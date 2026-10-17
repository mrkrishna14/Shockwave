"""Shared paths, labels, JSON output, subprocess, git and log helpers for the demo CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_DIR = REPO_ROOT / "demo"
LOCK_PATH = DEMO_DIR / "stripe-sources.lock.json"
CONSUMER_MANIFEST_PATH = DEMO_DIR / "consumer-repos.manifest.json"
CHANGE_MANIFEST_PATH = DEMO_DIR / "scenarios" / "stripe-payment-intent-amount-usd-major-units.manifest.json"
IDS_PATH = DEMO_DIR / "expected" / "demo-ids.json"
DEPENDENCY_FIXTURE_PATH = DEMO_DIR / "expected" / "dependencies.fixture.json"
MIGRATIONS_PATH = DEMO_DIR / "migrations" / "migrations.json"
EXPECTED_RESULTS_DIR = DEMO_DIR / "expected" / "results"
FIXTURE_SETS_DIR = DEMO_DIR / "stripe-fixtures"
DEFAULT_WORK_DIR = DEMO_DIR / ".work"
DEFAULT_REPORTS_DIR = DEMO_DIR / "reports"
DEFAULT_CACHE_DIR = DEMO_DIR / ".cache"
VENV_BIN = REPO_ROOT / ".venv" / "bin"
DEFAULT_NODE = VENV_BIN / "node"
DEFAULT_NPM = VENV_BIN / "npm"

# Labels printed by the commands and written into the persisted documents.
SIMULATED_LABEL = (
    "SIMULATED change: hypothetical, not announced by Stripe. Stripe is not a Shockwave customer and has "
    "not endorsed Shockwave."
)
FIXTURE_SERVER_LABEL = (
    "local fixture server: a simulation of Stripe responses from demo/stripe-fixtures (not Stripe, not "
    "Stripe production source, not another provider)"
)
RUNNER_LABEL = "LOCAL RUNNER DOUBLE — trusted local fixtures; plain subprocess, not a sandbox, not Sai's runner"
RUNNER_RESULT_PREFIX = (
    "LOCAL RUNNER DOUBLE (trusted local fixtures; plain subprocess, not a sandbox; not Sai's runner; demo run, "
    "duration_ms normalized to 0):"
)
CONSUMER_SUITE_LABEL = (
    "the consumer's own test command, run locally as a plain subprocess on a copy of the prepared repository "
    "(trusted local fixtures; not a sandbox, not Sai's runner)"
)
ANALYSIS_LABEL = "deterministic rules (analysis_mode=rules): no LLM, no model inference, nothing executed"
SCAN_FIXTURE_LABEL = "fixture (Noirit scanner not integrated)"
PROVIDER_SOURCE_STATEMENT = (
    "provider source access: unavailable (Stripe's server implementation is not accessible; SDKs, stripe-mock "
    "and samples are not provider source)"
)
ISOLATION_LABEL = (
    "Local single-machine demo: three fictional consumer organizations share one machine and one process. "
    "This does not prove cross-company identity isolation, which the backend (Ishan) must enforce."
)
TIMING_NOTE = (
    "duration_ms is normalized to 0 in results/ (measured durations vary between runs and interpretation never "
    "reads them); measured durations are kept in reports/runs/ only"
)
SNAPSHOT_FETCHED_AT = "2026-10-17T12:00:00Z"  # fixed: equals the prepared commits' author/committer date

MAX_LOG_TAIL_BYTES = 4096
COMMAND_TIMEOUT_SECONDS = 180

_KEY_PATTERN = re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z_]+|\bwhsec_[0-9A-Za-z]+")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s'\"]+")


class DemoError(Exception):
    """A step failed or a precondition is missing; the message says what to do."""


@dataclass(frozen=True)
class Layout:
    """Where generated state goes (overridable so tests run end to end in temporary directories)."""

    work_dir: Path = DEFAULT_WORK_DIR
    reports_dir: Path = DEFAULT_REPORTS_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR
    node: str = str(DEFAULT_NODE)
    offline: bool = False
    extra_redactions: tuple[str, ...] = field(default=())

    @property
    def repos_dir(self) -> Path:
        return self.work_dir / "repos"

    @property
    def snapshots_path(self) -> Path:
        return self.work_dir / "snapshots.json"

    @property
    def scratch_dir(self) -> Path:
        """Runner-double and consumer-suite copies (deleted after each run)."""
        return self.work_dir / "runs"

    @property
    def results_dir(self) -> Path:
        return self.reports_dir / "results"

    @property
    def raw_runs_dir(self) -> Path:
        return self.reports_dir / "runs"

    def repo(self, name: str) -> Path:
        return self.repos_dir / name

    def result_path(self, relative: str) -> Path:
        return self.results_dir / relative


# JSON ---------------------------------------------------------------------------------


def render_json(document: object) -> str:
    """Deterministic JSON text: sorted keys, 2-space indent, UTF-8, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_json(document).encode("utf-8"))
    return path


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DemoError(f"missing {display_path(path)}") from None
    except ValueError as error:
        raise DemoError(f"{display_path(path)} is not valid JSON: {error}") from None


def read_result(layout: Layout, relative: str, *, produced_by: str) -> dict:
    path = layout.result_path(relative)
    if not path.is_file():
        raise DemoError(f"missing {display_path(path, layout)}: run `demo.py {produced_by}` first")
    return read_json(path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repo_relative(path: Path) -> str:
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()


def display_path(path: Path, layout: Layout | None = None) -> str:
    """A path for reports: ``$WORK_DIR/...``/``$REPORTS_DIR/...`` or repository-relative, never absolute."""
    resolved = Path(path).resolve()
    if layout is not None:
        for root, token in ((layout.work_dir, "$WORK_DIR"), (layout.reports_dir, "$REPORTS_DIR")):
            root = Path(root).resolve()
            if resolved == root or resolved.is_relative_to(root):
                relative = resolved.relative_to(root).as_posix()
                return token if relative == "." else f"{token}/{relative}"
    if resolved.is_relative_to(REPO_ROOT):
        return resolved.relative_to(REPO_ROOT).as_posix()
    return resolved.name


def display_argv(argv: Sequence[str], layout: Layout | None = None) -> list[str]:
    """argv for persisted records: the interpreter by its bare name, other paths made non-absolute."""
    shown = []
    for index, item in enumerate(argv):
        if index == 0:
            shown.append(Path(item).name)
        elif os.path.isabs(item):
            shown.append(display_path(Path(item), layout))
        else:
            shown.append(item)
    return shown


# Logs ---------------------------------------------------------------------------------


def redact(text: str, layout: Layout | None = None) -> tuple[str, int]:
    """Text with anything that looks like a Stripe key or bearer token, and local paths, removed."""
    count = 0

    def _sub(pattern: re.Pattern[str], replacement: str, value: str) -> str:
        nonlocal count
        value, replaced = pattern.subn(replacement, value)
        count += replaced
        return value

    text = _sub(_BEARER_PATTERN, "Bearer [REDACTED]", text)
    text = _sub(_KEY_PATTERN, "[REDACTED_STRIPE_KEY]", text)
    for secret in layout.extra_redactions if layout is not None else ():
        if secret and secret in text:
            count += text.count(secret)
            text = text.replace(secret, "[REDACTED]")
    roots: list[tuple[str, str]] = []
    if layout is not None:
        for root, token in ((layout.work_dir, "$WORK_DIR"), (layout.reports_dir, "$REPORTS_DIR")):
            for spelling in {str(root), str(Path(root).resolve())}:
                roots.append((spelling, token))
    for spelling in {str(REPO_ROOT), str(REPO_ROOT.resolve())}:
        roots.append((spelling, "$REPO"))
    roots.append((str(Path.home()), "~"))
    for spelling, token in sorted(roots, key=lambda item: -len(item[0])):
        if spelling and spelling not in ("/", "."):
            text = text.replace(spelling, token)
    return text, count


def log_tail(output: str | None, layout: Layout | None = None, limit: int = MAX_LOG_TAIL_BYTES) -> dict:
    """A bounded (<= ``limit`` bytes), redacted tail of a run's output, for reports/runs only."""
    text, redactions = redact(output or "", layout)
    data = text.encode("utf-8")
    truncated = len(data) > limit
    if truncated:
        data = data[-limit:]
    return {
        "bytes_kept": len(data),
        "bytes_total": len(text.encode("utf-8")),
        "truncated": truncated,
        "redactions": redactions,
        "text": data.decode("utf-8", errors="ignore"),
        "note": f"last {limit} bytes at most; Stripe-key-like strings, bearer tokens and local paths are redacted",
    }


# Subprocesses ---------------------------------------------------------------------------


def base_env(extra: Mapping[str, str] | None = None, *, home: Path | None = None) -> dict[str, str]:
    """A minimal, credential-free environment: no STRIPE_* variables, PYTHONPATH or NODE_OPTIONS leak in."""
    env = {
        "PATH": os.pathsep.join((str(VENV_BIN), "/usr/bin", "/bin")),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "NO_COLOR": "1",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home) if home is not None else os.environ.get("HOME", "/"),
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    env.update(extra or {})
    return env


def run_process(
    argv: Sequence[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (output captured as text, merged stderr); OSError and timeouts become DemoError."""
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else base_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DemoError(f"{Path(argv[0]).name} timed out after {timeout}s") from None
    except OSError as error:
        raise DemoError(f"could not start {Path(argv[0]).name}: {error.strerror or error}") from None


_GIT: str | None = None


def git_executable() -> str:
    """The first git that actually runs here (PATH may list an x86-only /usr/local/bin/git first)."""
    global _GIT
    if _GIT is not None:
        return _GIT
    candidates = ("/usr/bin/git", shutil.which("git"), "/opt/homebrew/bin/git", "/usr/local/bin/git")
    for candidate in dict.fromkeys(item for item in candidates if item):
        try:
            probe = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, timeout=60, check=False, env=base_env()
            )
        except OSError:
            continue
        if probe.returncode == 0:
            _GIT = candidate
            return candidate
    raise DemoError("no runnable git executable found (needed for prepare, scan and migrations)")


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Shockwave Demo",
    "GIT_AUTHOR_EMAIL": "demo@shockwave.invalid",
    "GIT_COMMITTER_NAME": "Shockwave Demo",
    "GIT_COMMITTER_EMAIL": "demo@shockwave.invalid",
    "GIT_AUTHOR_DATE": SNAPSHOT_FETCHED_AT,
    "GIT_COMMITTER_DATE": SNAPSHOT_FETCHED_AT,
}


def git(args: Sequence[str], cwd: Path, *, check: bool = True, timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    """Run git hermetically (no global/system config, fixed identity); returns stdout."""
    argv = [git_executable(), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args]
    completed = run_process(argv, cwd, env=base_env(GIT_IDENTITY), timeout=timeout)
    if check and completed.returncode != 0:
        raise DemoError(f"git {' '.join(args[:3])} failed in {cwd.name}: {completed.stdout.strip()[-600:]}")
    return completed.stdout


def git_blob(repo: Path, commit: str, file: str, *, timeout: int = 60) -> bytes | None:
    """The bytes of ``file`` at ``commit`` (None when the commit has no such file)."""
    argv = [git_executable(), "cat-file", "blob", f"{commit}:{file}"]
    try:
        completed = subprocess.run(
            argv, cwd=repo, env=base_env({"GIT_TERMINAL_PROMPT": "0"}), capture_output=True, timeout=timeout, check=False
        )
    except OSError as error:
        raise DemoError(f"could not start git: {error.strerror or error}") from None
    return completed.stdout if completed.returncode == 0 else None


# Copies ---------------------------------------------------------------------------------

COPY_IGNORE = (
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
    "*.sqlite",
    ".venv",
    ".shockwave",
)


def remove_tree(path: Path, *, inside: Path) -> None:
    """Delete ``path`` (file, link or directory), refusing anything outside ``inside``."""
    path, inside = Path(path), Path(inside).resolve()
    if not (path.is_symlink() or path.exists()):
        return
    parent = path.parent.resolve()
    if not (parent == inside or parent.is_relative_to(inside)):
        raise DemoError(f"refusing to delete {path}: it is outside {inside}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def scratch_copy(source: Path, destination: Path, *, inside: Path) -> Path:
    """Copy a prepared repository (with .git) for a run, linking node_modules instead of copying it."""
    remove_tree(destination, inside=inside)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    installed = source / "node_modules"
    if installed.is_dir():
        (destination / "node_modules").symlink_to(installed.resolve(), target_is_directory=True)
    return destination


# Consumer suites ------------------------------------------------------------------------

_PYTEST_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b")
_PYTEST_SUMMARY = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_NODE_COUNT = re.compile(r"^\s*(?:#|ℹ) (tests|pass|fail) (\d+)\s*$", re.MULTILINE)
_NODE_FAILED = re.compile(r"^✖ (.+?)(?: \(\d+(?:\.\d+)?m?s\))?$", re.MULTILINE)
_TAP_FAILED = re.compile(r"^not ok \d+ - (.+)$", re.MULTILINE)
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)


@dataclass(frozen=True)
class SuiteCommand:
    argv: list[str]  # executable argv
    shown: list[str]  # argv as persisted (interpreter by bare name)
    declared: str  # the consumer manifest's test command
    kind: str  # "node" | "pytest"


def own_suite_command(entry: Mapping, checkout: Path, node: str) -> SuiteCommand:
    """The consumer's own test command (demo/consumer-repos.manifest.json ``test``) as an argv.

    ``npm test`` runs the package.json ``test`` script directly with this Node (no npm, no
    network); ``python -m pytest`` runs under this Python with ``-q -p no:cacheprovider`` so no
    cache is written into the copy.
    """
    declared = str(entry.get("test", ""))
    if declared == "npm test":
        import shlex

        package = json.loads((checkout / "package.json").read_text(encoding="utf-8"))
        script = str(package.get("scripts", {}).get("test", ""))
        parts = shlex.split(script)
        if not parts or parts[0] != "node":
            raise DemoError(f"{entry.get('name')}: package.json test script {script!r} does not start with node")
        return SuiteCommand([node, *parts[1:]], ["node", *parts[1:]], f"npm test ({script})", "node")
    if declared == "python -m pytest":
        tail = ["-m", "pytest", "-q", "-p", "no:cacheprovider"]
        return SuiteCommand([sys.executable, *tail], ["python", *tail], declared, "pytest")
    raise DemoError(f"{entry.get('name')}: unsupported test command {declared!r}")


def suite_counts(kind: str, output: str) -> dict:
    """tests/passed/failed and failing test names parsed from a node --test or pytest -q run."""
    if kind == "node":
        counts = {name: int(value) for name, value in _NODE_COUNT.findall(output)}
        failing = []
        marker = output.find("✖ failing tests:")
        if marker >= 0:
            failing = _NODE_FAILED.findall(output[marker + len("✖ failing tests:") :])
        failing = failing or _TAP_FAILED.findall(output)
        tests, passed, failed = counts.get("tests", 0), counts.get("pass", 0), counts.get("fail", 0)
    else:
        tests = passed = failed = 0
        for line in reversed(output.splitlines()):
            if _PYTEST_SUMMARY.search(line) and (_PYTEST_COUNT.search(line) or "no tests ran" in line):
                for value, label in _PYTEST_COUNT.findall(line):
                    if label == "passed":
                        passed += int(value)
                    elif label in ("failed", "error", "errors"):
                        failed += int(value)
                    tests += int(value) if label != "skipped" else 0
                break
        failing = _PYTEST_FAILED.findall(output)
    return {
        "tests": tests,
        "passed": passed,
        "failed": failed,
        "failing_tests": sorted(dict.fromkeys(item.strip() for item in failing))[:20],
    }


def count(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {plural or noun + 's'}"


def short_scenario(scenario_id: str) -> str:
    """``stripe.payment-intent-amount-units.checkout-display.typescript`` -> ``checkout-display``."""
    parts = scenario_id.split(".")
    return parts[2] if len(parts) >= 4 else hashlib.sha256(scenario_id.encode()).hexdigest()[:12]
