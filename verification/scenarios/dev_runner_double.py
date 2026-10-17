"""Development stand-in for Sai's verification runner: trusted local fixtures only.

THIS IS NOT A SANDBOX. It runs the consumer's code as a plain local subprocess with a
timeout, a minimal secret-free environment, process-group cleanup and only the last
64 KiB of output kept (in memory, never written to disk), and without network isolation
(the harnesses' network guards are best-effort in-process interception), which is
acceptable only for repository fixtures this team wrote (for example
``tests/analyzer/fixtures/consumer_doubles``). It therefore refuses ``sandbox_*``
profiles with an infrastructure error (interpreted as inconclusive) instead of
pretending to isolate anything. Sai's runner (``verification/runner``) is the real
executor; this double exists so the native harnesses, the staging protocol and the
interpretation can be proven end to end with real native test runs before it lands.

Per phase it copies the consumer repository to ``work_dir/checkout-<phase>`` (the source
is never mutated: a source at or inside that checkout, which every run replaces, is
refused), optionally applies ``patch`` to the copy (simulating Sai's migration), stages
the scenario, runs exactly the argv ``staging.runner_argv_hint`` documents (with this
machine's interpreter) with cwd = checkout, and returns the ExecutionResult plus the raw
report bytes (bounded), exactly what the real runner hands to
``verification.scenarios.interpret``, and the bounded output tail (logs only).
``run_control_phase_locally`` does the same for a control scenario and one fixture
variant (``work_dir/checkout-control-<variant>``, ``staging.stage_control`` and
``staging.control_argv_hint``); controls have no VerificationPlan.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from discovery.contracts import ExecutionResult, VerificationPlan
from verification.scenarios.catalog import UnknownScenario, get_control
from verification.scenarios.interpret import PhaseRun
from verification.scenarios.staging import (
    CONTROL_VARIANTS,
    MAX_REPORT_BYTES,
    NODE_PROFILES,
    PHASES,
    PYTHON_PROFILES,
    ControlVariant,
    Phase,
    StagedRun,
    clip_text,
    control_argv_hint,
    runner_argv_hint,
    stage_control,
    stage_scenario,
)

TRUSTED_PROFILES = frozenset({"trusted_python", "trusted_node"})
MAX_OUTPUT_BYTES = 64 * 1024
_DRAIN_GRACE_SECONDS = 5

_PYTEST_SUMMARY = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_PYTEST_COUNT = re.compile(r"\b(\d+) (?:passed|failed|errors?|xfailed|xpassed)\b")
_NODE_TESTS = re.compile(r"^\s*(?:#|\u2139) tests (\d+)\s*$", re.MULTILINE)  # TAP or spec reporter
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)


def run_phase_locally(
    plan: VerificationPlan,
    consumer_repo: Path,
    phase: str,
    *,
    work_dir: Path,
    node_executable: str | None = None,
    timeout_seconds: int | None = None,
    patch: Callable[[Path], None] | None = None,
) -> PhaseRun:
    """Execute one phase of ``plan`` against a copy of a trusted local consumer repository."""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}; got {phase!r}")
    return _run_locally(
        profile=plan.runner_profile,
        consumer_repo=consumer_repo,
        checkout_name=f"checkout-{phase}",
        work_dir=work_dir,
        stage=lambda checkout, work: stage_scenario(plan, checkout, cast(Phase, phase), work),
        argv_hint=lambda: runner_argv_hint(plan),
        timeout=plan.timeout_seconds if timeout_seconds is None else timeout_seconds,
        node_executable=node_executable,
        patch=patch,
    )


def run_control_phase_locally(
    scenario_id: str,
    consumer_repo: Path,
    variant: str,
    *,
    work_dir: Path,
    node_executable: str | None = None,
    timeout_seconds: int | None = None,
    patch: Callable[[Path], None] | None = None,
) -> PhaseRun:
    """Execute a control scenario against one fixture ``variant`` (baseline or proposed) on a trusted local copy.

    Uses the control's trusted profile: this double only runs trusted local fixtures.
    """
    if variant not in CONTROL_VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(CONTROL_VARIANTS)}; got {variant!r}")
    try:
        spec = get_control(scenario_id)
    except UnknownScenario:
        raise ValueError(f"not a control scenario: {clip_text(scenario_id, 120)!r}") from None
    return _run_locally(
        profile=spec.trusted_profile,
        consumer_repo=consumer_repo,
        checkout_name=f"checkout-control-{variant}",
        work_dir=work_dir,
        stage=lambda checkout, work: stage_control(scenario_id, checkout, cast(ControlVariant, variant), work),
        argv_hint=lambda: control_argv_hint(scenario_id),
        timeout=spec.timeout_seconds if timeout_seconds is None else timeout_seconds,
        node_executable=node_executable,
        patch=patch,
    )


def _run_locally(
    *,
    profile: str,
    consumer_repo: Path,
    checkout_name: str,
    work_dir: Path,
    stage: Callable[[Path, Path], StagedRun],
    argv_hint: Callable[[], list[str]],
    timeout: int,
    node_executable: str | None,
    patch: Callable[[Path], None] | None,
) -> PhaseRun:
    source = Path(consumer_repo).resolve()
    if not source.is_dir():
        raise ValueError(f"consumer_repo must be an existing directory: {source}")
    work = Path(work_dir).resolve()
    if _within(work, source):
        raise ValueError("work_dir must be outside the consumer repository")
    checkout = work / checkout_name
    if not checkout.is_symlink() and _within(source, checkout):  # a symlink there is unlinked, never followed
        raise ValueError(
            f"consumer_repo must not be work_dir/{checkout_name} or inside it: every run replaces that directory"
        )
    if profile not in TRUSTED_PROFILES:
        return _infrastructure_error(
            f"{profile} requires a sandbox; the development runner double executes only "
            "trusted local fixtures and refuses to run it"
        )

    interpreter = _interpreter(profile, node_executable)
    if interpreter is None:
        return _infrastructure_error("node executable not found; cannot run the node scenario")

    work.mkdir(parents=True, exist_ok=True)
    _fresh_copy(source, checkout)
    if patch is not None:
        patch(checkout)
    try:
        staged = stage(checkout, work)
    except ValueError as exc:
        return _infrastructure_error(f"staging refused: {clip_text(exc, 300)}")

    argv = [interpreter, *argv_hint()[1:]]  # the documented argv, as Sai's runner runs it
    exit_code, timed_out, duration_ms, output, start_error = _execute(
        argv, checkout, _minimal_env(work, argv[0], staged), timeout
    )
    if start_error is not None:
        return _infrastructure_error(start_error)
    return PhaseRun(
        execution=ExecutionResult(
            exit_code=exit_code,
            timed_out=timed_out,
            infrastructure_error=None,
            artifact_ids=[],
            duration_ms=duration_ms,
            tests_run=_tests_run(profile, output),
        ),
        report=_read_report(staged.report_path),
        output_tail=output,
    )


def _interpreter(profile: str, node_executable: str | None) -> str | None:
    """The profile's interpreter (this Python, or node) as an absolute path; None when node is missing.

    Absolute, because the run's cwd is the checkout: a relative ``--node .venv/bin/node``
    would otherwise be resolved against the checkout and fail to start.
    """
    if profile in PYTHON_PROFILES:
        return sys.executable
    found = shutil.which(node_executable or "node")
    return None if found is None else os.path.abspath(found)


def _within(path: Path, directory: Path) -> bool:
    """Whether ``path`` is ``directory`` or inside it, also by file identity.

    Resolved paths can still spell one directory differently (a case-insensitive volume,
    a bind mount), so every existing ancestor-or-self of ``path`` is compared by stat too.
    """
    if path == directory or path.is_relative_to(directory):
        return True
    try:
        target = os.stat(directory)
    except OSError:
        return False  # nothing there yet
    for candidate in (path, *path.parents):
        try:
            if os.path.samestat(os.stat(candidate), target):
                return True
        except OSError:
            continue  # not created yet
    return False


def _minimal_env(work: Path, executable: str, staged: StagedRun) -> dict[str, str]:
    """Only what the interpreters need plus the staging contract; nothing inherited."""
    entries = [str(Path(executable).parent), str(Path(sys.executable).parent), *os.defpath.split(os.pathsep)]
    path = os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))
    env = {"PATH": path, "HOME": str(work), "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1"}
    env.update(staged.env)
    return env


class _BoundedTail(threading.Thread):
    """Drains the runner's output pipe, keeping only the last MAX_OUTPUT_BYTES in memory."""

    def __init__(self, stream) -> None:
        super().__init__(name="dev-runner-output", daemon=True)
        self._stream = stream
        self._tail = bytearray()

    def run(self) -> None:
        with self._stream:
            while chunk := self._stream.read1(MAX_OUTPUT_BYTES):
                self._tail += chunk
                del self._tail[: max(len(self._tail) - MAX_OUTPUT_BYTES, 0)]

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")


def _execute(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: int
) -> tuple[int | None, bool, int, str, str | None]:
    """Run to completion or timeout: (exit_code, timed_out, duration_ms, output_tail, start_error)."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so a timeout kills grandchildren too
        )
    except OSError as exc:
        return None, False, 0, "", f"could not start {Path(argv[0]).name}: {clip_text(exc.strerror or exc)}"
    tail = _BoundedTail(process.stdout)
    tail.start()
    try:
        exit_code: int | None = process.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(process)  # the leader is not reaped yet, so the group id is still ours
        process.wait()
        exit_code, timed_out = None, True
    except BaseException:
        _kill_group(process)
        process.wait()
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    tail.join(_DRAIN_GRACE_SECONDS)
    if tail.is_alive():  # a straggler in the group still holds the pipe open
        _kill_group(process)
        tail.join(_DRAIN_GRACE_SECONDS)
    return exit_code, timed_out, duration_ms, tail.text(), None


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tests_run(profile: str, output: str) -> int:
    if profile in NODE_PROFILES:
        counts = _NODE_TESTS.findall(output)
        return int(counts[-1]) if counts else 0
    for line in reversed(output.splitlines()):
        if _PYTEST_SUMMARY.search(line) and ("no tests ran" in line or _PYTEST_COUNT.search(line)):
            return sum(int(count) for count in _PYTEST_COUNT.findall(line))
    ran = _UNITTEST_RAN.findall(output)
    return int(ran[-1]) if ran else 0


def _fresh_copy(source: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )


def _read_report(path: Path) -> bytes | None:
    """The report bytes, read bounded, without following symlinks or blocking on a FIFO."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read(MAX_REPORT_BYTES + 1)  # one extra byte lets interpretation reject oversize


def _infrastructure_error(message: str) -> PhaseRun:
    return PhaseRun(
        execution=ExecutionResult(
            exit_code=None,
            timed_out=False,
            infrastructure_error=message,
            artifact_ids=[],
            duration_ms=0,
            tests_run=0,
        ),
        report=None,
    )
