"""Runner-facing staging protocol for executable regression scenarios.

Sai's runner owns execution: the disposable checkout, sandbox, subprocess, logs and
artifacts. Inside that checkout it calls ``stage_scenario`` once per phase. Staging
writes the catalog's native harness and its ``binding.json`` (plus, for the Python
profiles, an empty ``pytest.ini``) under ``.shockwave/scenarios/<scenario_id>/``, writes a
fresh copy of the phase's provider response fixture into the runner's work directory
(never into the checkout), and returns the only environment variables the run needs.
The runner executes the profile's backend-owned argv (``runner_argv_hint`` documents the
mapping) with cwd set to the checkout and that environment merged into its own minimal,
secret-free environment, then passes the report found at ``SHOCKWAVE_REPORT_PATH`` (at
most ``MAX_REPORT_BYTES``) to ``verification.scenarios.interpret``.

Why it is defensive: the checkout is consumer-controlled content. A committed
``.shockwave`` symlink, or a pre-existing staged file that is a symlink, must not
redirect writes outside the checkout, so symlinked staging directories are refused and
staged files are always created fresh (exclusive create never follows a link). A stored
plan is trusted only after it is re-validated against the catalog, so a tampered plan
cannot swap fixtures, change the runner profile family, or point the runner at
arbitrary test files. The Python argv and environment keep the checkout's pytest
configuration out of the run (no conftest.py, no pytest.ini/pyproject.toml/setup.cfg/
tox.ini addopts or ``-p`` plugins, no auto-loaded plugins, and on Python >= 3.11 no
checkout module shadowing pytest). Code the run imports from the checkout still executes
in-process, so a report is consistency-checked, never authenticated (see interpret).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from discovery.contracts import VerificationPlan

# Re-exported: plans, interpret and the runner double import clip_text from here.
from discovery.text import clip_text
from verification.scenarios.catalog import (
    ASSETS_DIR,
    ScenarioSpec,
    UnknownScenario,
    fixture_path,
    get_scenario,
)

Phase = Literal["baseline", "proposed", "patched"]
PHASES: tuple[Phase, ...] = ("baseline", "proposed", "patched")

# Version of the scenario report and binding.json protocol, hard-coded by the harness assets.
# It is not the contract models' schema_version (discovery.contracts.SCHEMA_VERSION).
SCHEMA_VERSION = "shockwave.v1"
REPORT_KIND = "scenario_report"
BINDING_FILE = "binding.json"
PYTEST_CONFIG_FILE = "pytest.ini"
MAX_REPORT_BYTES = 64 * 1024

PYTHON_PROFILES = frozenset({"trusted_python", "sandbox_python"})
NODE_PROFILES = frozenset({"trusted_node", "sandbox_node"})

# The complete environment contract between staging and the harnesses.
ENV_KEYS = (
    "SHOCKWAVE_SCENARIO_ID",
    "SHOCKWAVE_SCENARIO_PHASE",
    "SHOCKWAVE_FIXTURE_ID",
    "SHOCKWAVE_FIXTURE_PATH",
    "SHOCKWAVE_REPORT_PATH",
    "SHOCKWAVE_REPO_ROOT",
)

# Keys of the scenario report both harnesses write to SHOCKWAVE_REPORT_PATH.
REPORT_FIELDS = (
    "schema_version",
    "kind",
    "scenario_id",
    "phase",
    "fixture_id",
    "assertion_id",
    "status",  # "passed" | "failed" (relevant comparison differed) | "error" (comparison never ran)
    "expected",
    "actual",
    "error_type",
    "message",
    "requested_urls",
    "harness_sha256",
    "binding_sha256",
)

# Runner hardening for the Python profiles, merged into the staged environment: pytest
# auto-loads no plugin from the consumer's environment, and ``python -m pytest`` does not
# put the checkout on sys.path (Python >= 3.11; older interpreters ignore the variable), so
# a checkout-root pytest.py or *.dist-info cannot replace pytest or register a plugin.
PYTHON_RUNNER_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONSAFEPATH": "1"}

# With ``-c`` this is the only configuration pytest reads (an empty [pytest] section).
_PYTEST_CONFIG = b"# Staged by Shockwave: the scenario run never reads the checkout's pytest configuration.\n[pytest]\n"


@dataclass(frozen=True)
class StagedRun:
    """Everything the runner needs to execute one staged phase. Paths are absolute."""

    scenario_id: str
    phase: str
    fixture_id: str
    test_files: tuple[str, ...]  # repository-relative; execute with cwd = repository root
    env: dict[str, str]  # ENV_KEYS (+ PYTHON_RUNNER_ENV for Python); merge into the runner's minimal environment
    fixture_path: Path
    report_path: Path
    staged_dir: Path


def validated_spec(plan: VerificationPlan) -> ScenarioSpec:
    """The catalog scenario a plan names; refuses (ValueError) plans that disagree with it."""
    try:
        spec = get_scenario(plan.scenario_id)
    except UnknownScenario:
        raise ValueError(f"refusing plan: unknown scenario {clip_text(plan.scenario_id, 120)!r}") from None
    if list(plan.test_files) != [spec.staged_test_file]:
        raise ValueError(
            f"refusing plan: test_files must be exactly [{spec.staged_test_file!r}] for {spec.scenario_id}"
        )
    if (plan.old_fixture_id, plan.new_fixture_id) != (spec.old_fixture_id, spec.new_fixture_id):
        raise ValueError(
            f"refusing plan: fixtures must be {spec.old_fixture_id} (baseline) and "
            f"{spec.new_fixture_id} (proposed/patched) for {spec.scenario_id}"
        )
    if plan.runner_profile not in (spec.trusted_profile, spec.sandbox_profile):
        raise ValueError(
            f"refusing plan: runner_profile {plan.runner_profile} cannot run {spec.scenario_id} "
            f"(expected {spec.trusted_profile} or {spec.sandbox_profile})"
        )
    return spec


def fixture_id_for_phase(plan: VerificationPlan, phase: str) -> str:
    """Baseline runs the current-contract fixture; proposed and patched run the proposed one."""
    _check_phase(phase)
    return plan.old_fixture_id if phase == "baseline" else plan.new_fixture_id


def harness_sha256(spec: ScenarioSpec) -> str:
    """SHA-256 of the catalog harness asset; the harness reports the hash of its own bytes."""
    return hashlib.sha256(_harness_bytes(spec)).hexdigest()


def binding_document(spec: ScenarioSpec) -> dict:
    """The ``binding.json`` staged next to the harness (deterministic for a catalog entry)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": spec.scenario_id,
        "assertion_id": spec.assertion_id,
        "expected_display": spec.expected_display,
        "binding": {
            "language": spec.binding.language,
            "module": spec.binding.module,
            "function": spec.binding.function,
            "args": list(spec.binding.args),
            "http_injection": spec.binding.http_injection,
        },
        "harness_sha256": harness_sha256(spec),
    }


def binding_bytes(spec: ScenarioSpec) -> bytes:
    return (json.dumps(binding_document(spec), indent=2, sort_keys=True) + "\n").encode("utf-8")


def binding_sha256(spec: ScenarioSpec) -> str:
    return hashlib.sha256(binding_bytes(spec)).hexdigest()


def stage_scenario(
    plan: VerificationPlan,
    repo_root: Path,
    phase: Phase,
    work_dir: Path,
) -> StagedRun:
    """Stage ``plan``'s scenario into the checkout at ``repo_root`` for one ``phase``.

    Fresh copies every run: the scenario directory is recreated, the fixture copy is
    rewritten and any stale report for this phase is deleted, so a report found after
    execution can only come from this run.
    """
    _check_phase(phase)
    spec = validated_spec(plan)
    root = _repository_root(repo_root)
    work = _work_directory(work_dir, root)
    python = plan.runner_profile in PYTHON_PROFILES

    staged_dir = _fresh_scenario_directory(root, spec)
    _write_new(staged_dir / spec.staged_test_name, _harness_bytes(spec))
    _write_new(staged_dir / BINDING_FILE, binding_bytes(spec))
    if python:
        _write_new(staged_dir / PYTEST_CONFIG_FILE, _PYTEST_CONFIG)

    fixture_id = fixture_id_for_phase(plan, phase)
    fixture_copy = work / f"{fixture_id}.json"
    _remove_file(fixture_copy)
    _write_new(fixture_copy, fixture_path(fixture_id).read_bytes())

    report_path = work / f"{phase}-report.json"
    _remove_file(report_path)

    env = {
        "SHOCKWAVE_SCENARIO_ID": spec.scenario_id,
        "SHOCKWAVE_SCENARIO_PHASE": phase,
        "SHOCKWAVE_FIXTURE_ID": fixture_id,
        "SHOCKWAVE_FIXTURE_PATH": str(fixture_copy),
        "SHOCKWAVE_REPORT_PATH": str(report_path),
        "SHOCKWAVE_REPO_ROOT": str(root),
    }
    if python:
        env.update(PYTHON_RUNNER_ENV)
    return StagedRun(
        scenario_id=spec.scenario_id,
        phase=phase,
        fixture_id=fixture_id,
        test_files=tuple(plan.test_files),
        env=env,
        fixture_path=fixture_copy,
        report_path=report_path,
        staged_dir=staged_dir,
    )


def runner_argv_hint(plan: VerificationPlan) -> list[str]:
    """Documentation of the profile -> argv mapping Sai's runner owns (run with cwd = checkout).

    Interpreter paths are the runner's choice; the arguments are not, and the run's
    environment includes ``StagedRun.env``. ``-p no:cacheprovider`` keeps pytest from
    writing ``.pytest_cache`` into the checkout. ``--noconftest`` loads no conftest.py, and
    ``-c``/``--rootdir`` make the staged ``pytest.ini`` and scenario directory pytest's only
    configuration and root, so the checkout's pytest.ini, pyproject.toml, setup.cfg and
    tox.ini (``addopts``, ``-p`` plugins) never reach the run. The node test runner reads
    no configuration from the checkout.
    """
    spec = validated_spec(plan)
    if plan.runner_profile in PYTHON_PROFILES:
        return [
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest",
            "-c", f"{spec.staged_dir}/{PYTEST_CONFIG_FILE}", "--rootdir", spec.staged_dir,
            *plan.test_files,
        ]
    if plan.runner_profile in NODE_PROFILES:
        return ["node", "--test", *plan.test_files]
    raise ValueError(f"unknown runner profile {plan.runner_profile!r}")


def _check_phase(phase: str) -> None:
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}; got {clip_text(phase, 40)!r}")


def _harness_bytes(spec: ScenarioSpec) -> bytes:
    return (ASSETS_DIR / spec.harness_asset).read_bytes()


def _repository_root(repo_root: Path) -> Path:
    candidate = Path(repo_root)
    if not candidate.is_dir():
        raise ValueError(f"repo_root must be an existing directory: {candidate}")
    return candidate.resolve(strict=True)


def _work_directory(work_dir: Path, root: Path) -> Path:
    # Fixture copies and reports live outside the code under test.
    work = Path(work_dir).resolve()
    if work == root or work.is_relative_to(root):
        raise ValueError("work_dir must be outside the repository checkout being verified")
    work.mkdir(parents=True, exist_ok=True)
    return work.resolve(strict=True)


def _fresh_scenario_directory(root: Path, spec: ScenarioSpec) -> Path:
    parts = PurePosixPath(spec.staged_dir).parts
    for depth in range(1, len(parts) + 1):
        _check_staging_component(root, parts[:depth])
    staged = root.joinpath(*parts)
    if staged.exists():
        shutil.rmtree(staged)  # a real directory (checked above); rmtree never follows links
    for depth in range(1, len(parts) + 1):
        root.joinpath(*parts[:depth]).mkdir(exist_ok=depth < len(parts))
        _check_staging_component(root, parts[:depth])
    return staged


def _check_staging_component(root: Path, parts: tuple[str, ...]) -> None:
    path = root.joinpath(*parts)
    label = "/".join(parts)
    if path.is_symlink():
        raise ValueError(f"refusing to stage: {label} is a symlink in the checkout")
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"refusing to stage: {label} exists and is not a directory")
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"refusing to stage: {label} resolves outside the repository checkout")


def _write_new(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:  # exclusive create: fails instead of following a symlink
        handle.write(data)


def _remove_file(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        raise ValueError(f"refusing to stage: {path.name} in the work directory is a directory")
    path.unlink(missing_ok=True)  # removes a symlink itself, never its target
