"""Stage C: verification plans and the runner-facing staging protocol.

Risks are built by hand from the dependency fixtures (the analyzer is another module):
evidence = the dependency's source evidence plus the catalog's scenario pointer.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from discovery import text
from discovery.contracts import (
    SCHEMA_VERSION,
    Dependency,
    Evidence,
    RepositorySnapshot,
    Risk,
    VerificationPlan,
)
from discovery.ids import evidence_id, risk_id, stable_id, verification_plan_id
from verification.scenarios import catalog, staging
from verification.scenarios.plans import (
    TRUSTED_CONNECTIONS_ENV,
    AmbiguousScenario,
    NoExecutableScenario,
    PlanConfig,
    build_verification_plan,
    not_run_result,
)
from verification.scenarios.staging import (
    ENV_KEYS,
    PYTHON_RUNNER_ENV,
    Phase,
    harness_sha256,
    runner_argv_hint,
    stage_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEPENDENCIES = json.loads((FIXTURES / "demo_dependencies.json").read_text(encoding="utf-8"))["dependencies"]
CONSUMER_IDS = json.loads((FIXTURES / "consumer_fixture_ids.json").read_text(encoding="utf-8"))
CHANGE_ID = json.loads(
    (REPO_ROOT / "examples/provider/demo_payments/changes/charge_amount_cents_to_dollars.json").read_text(
        encoding="utf-8"
    )
)["id"]
PY_SCENARIO = "demo-payments.charge-amount-units.display.python"
TS_SCENARIO = "demo-payments.charge-amount-units.display.typescript"
OLD_FIXTURE = "demo-payments.charge.ch_demo.v1-cents"
NEW_FIXTURE = "demo-payments.charge.ch_demo.v2-dollars"
FETCHED_AT = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _dependency(handle: str) -> Dependency:
    return Dependency.model_validate(DEPENDENCIES[handle])


def _risk(handle: str, scenario_ids: tuple[str, ...] = (), *, category: str = "conversion") -> Risk:
    dependency = _dependency(handle)
    pointers = [
        catalog.scenario_pointer_evidence(catalog.get_scenario(scenario_id), dependency)
        for scenario_id in scenario_ids
    ]
    return Risk(
        id=risk_id(CHANGE_ID, dependency.enrollment_id, dependency.repository_snapshot_id, category),
        change_id=CHANGE_ID,
        consumer_org_id=dependency.consumer_org_id,
        enrollment_id=dependency.enrollment_id,
        repository_snapshot_id=dependency.repository_snapshot_id,
        dependency_ids=[dependency.id],
        severity="high",
        confidence="direct",
        explanation="Predicted: response.amount changes from cents to dollars while the consumer divides by 100.",
        suggested_action="Stop dividing response.amount by 100 once the proposed contract is live.",
        evidence=[*dependency.evidence, *pointers],
    )


def _snapshot(repository: str) -> RepositorySnapshot:
    ids = CONSUMER_IDS["repositories"][repository]
    return RepositorySnapshot(
        id=ids["repository_snapshot_id"],
        repository_connection_id=ids["repository_connection_id"],
        commit_sha=ids["commit_sha"],
        fetched_at=FETCHED_AT,
        content_hash="sha256:" + "0" * 64,
    )


def _trusting(*repositories: str) -> PlanConfig:
    return PlanConfig(
        frozenset(CONSUMER_IDS["repositories"][repo]["repository_connection_id"] for repo in repositories)
    )


def _python_plan(config: PlanConfig | None = None) -> VerificationPlan:
    config = _trusting("python_storefront") if config is None else config
    return build_verification_plan(_risk("python_display", (PY_SCENARIO,)), _snapshot("python_storefront"), config=config)


def _typescript_plan(config: PlanConfig | None = None) -> VerificationPlan:
    config = _trusting("ts_storefront") if config is None else config
    return build_verification_plan(_risk("typescript_display", (TS_SCENARIO,)), _snapshot("ts_storefront"), config=config)


# Plans ---------------------------------------------------------------------------


def test_python_display_plan_fields() -> None:
    risk = _risk("python_display", (PY_SCENARIO,))
    snapshot = _snapshot("python_storefront")
    plan = build_verification_plan(risk, snapshot, config=_trusting("python_storefront"))

    assert plan.scenario_id == PY_SCENARIO
    assert plan.runner_profile == "trusted_python"
    assert plan.test_files == [f".shockwave/scenarios/{PY_SCENARIO}/test_shockwave_display_invariant.py"]
    assert (plan.old_fixture_id, plan.new_fixture_id) == (OLD_FIXTURE, NEW_FIXTURE)
    assert plan.timeout_seconds == 60
    assert plan.expected_behavior == catalog.get_scenario(PY_SCENARIO).expected_behavior
    assert "$10.00" in plan.expected_behavior
    assert plan.id == verification_plan_id(risk.id, snapshot.id, PY_SCENARIO)
    assert (plan.risk_id, plan.repository_snapshot_id) == (risk.id, snapshot.id)


def test_typescript_display_plan_fields() -> None:
    plan = _typescript_plan()

    assert plan.scenario_id == TS_SCENARIO
    assert plan.runner_profile == "trusted_node"
    assert plan.test_files == [f".shockwave/scenarios/{TS_SCENARIO}/shockwave_display_invariant.test.mjs"]
    assert (plan.old_fixture_id, plan.new_fixture_id) == (OLD_FIXTURE, NEW_FIXTURE)
    assert plan.timeout_seconds == 60


def test_untrusted_connections_get_the_sandbox_profile() -> None:
    assert _python_plan(PlanConfig()).runner_profile == "sandbox_python"
    assert _python_plan(_trusting("ts_storefront")).runner_profile == "sandbox_python"
    assert _typescript_plan(PlanConfig()).runner_profile == "sandbox_node"
    assert _typescript_plan(_trusting("ts_storefront")).runner_profile == "trusted_node"


def test_plan_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = CONSUMER_IDS["repositories"]["python_storefront"]["repository_connection_id"]
    monkeypatch.setenv(TRUSTED_CONNECTIONS_ENV, f" {connection.upper()} , ,not-a-uuid")

    assert PlanConfig.from_env().trusted_repository_connection_ids == frozenset({connection, "not-a-uuid"})
    risk, snapshot = _risk("python_display", (PY_SCENARIO,)), _snapshot("python_storefront")
    assert build_verification_plan(risk, snapshot).runner_profile == "trusted_python"

    monkeypatch.delenv(TRUSTED_CONNECTIONS_ENV)
    assert PlanConfig.from_env() == PlanConfig()
    assert build_verification_plan(risk, snapshot).runner_profile == "sandbox_python"
    assert PlanConfig.from_env({TRUSTED_CONNECTIONS_ENV: connection}).is_trusted(snapshot.repository_connection_id)


def test_plans_are_deterministic_and_fully_serialized() -> None:
    first, second = _python_plan(), _python_plan()

    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert set(first.model_dump(mode="json")) == {
        "id",
        "risk_id",
        "repository_snapshot_id",
        "scenario_id",
        "runner_profile",
        "test_files",
        "old_fixture_id",
        "new_fixture_id",
        "expected_behavior",
        "timeout_seconds",
    }
    assert _python_plan(PlanConfig()).id == first.id  # the profile is configuration, not identity


@pytest.mark.parametrize(
    ("handle", "category"), [("python_storage", "storage"), ("python_fraud_threshold", "comparison")]
)
def test_storage_and_comparison_risks_have_no_executable_proof(handle: str, category: str) -> None:
    risk = _risk(handle, category=category)

    with pytest.raises(NoExecutableScenario, match="not_run") as excinfo:
        build_verification_plan(risk, _snapshot("python_storefront"), config=_trusting("python_storefront"))

    result = not_run_result(risk, str(excinfo.value))
    assert result.status == "not_run"
    assert (result.baseline, result.proposed) == (None, None)
    assert result.scenario_id == "unavailable"
    assert result.artifact_ids == []
    assert result.summary == str(excinfo.value)
    assert (result.risk_id, result.repository_snapshot_id) == (risk.id, risk.repository_snapshot_id)
    assert result.model_dump(mode="json")["baseline"] is None
    # The contract's schema_version is passed explicitly, never left to a model default.
    assert result.schema_version == SCHEMA_VERSION and "schema_version" in result.model_fields_set


def test_not_run_result_ids_are_deterministic() -> None:
    storage, threshold = _risk("python_storage", category="storage"), _risk("python_fraud_threshold", category="x")

    first = not_run_result(storage, "no executable scenario")
    assert str(uuid.UUID(first.id)) == first.id
    assert not_run_result(storage, "reworded reason").id == first.id
    assert not_run_result(threshold, "no executable scenario").id != first.id
    assert not_run_result(storage, "   ").summary  # a blank reason never yields an empty summary


def test_unknown_scenario_pointer_is_not_executable() -> None:
    dependency = _dependency("python_display")
    pointer = Evidence(
        id=evidence_id("scenario-pointer", "demo-payments.unknown", dependency.id),
        kind="test",
        visibility="consumer_private",
        source_uri="shockwave-scenario://demo-payments.unknown",
        revision=catalog.CATALOG_REVISION,
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=None,
        excerpt=None,
    )
    risk = _risk("python_display").model_copy(update={"evidence": [*dependency.evidence, pointer]})

    with pytest.raises(NoExecutableScenario, match="not in this catalog"):
        build_verification_plan(risk, _snapshot("python_storefront"), config=PlanConfig())


def test_two_bound_scenarios_are_ambiguous_and_degrade_to_not_run() -> None:
    """AmbiguousScenario is a NoExecutableScenario, so callers record not_run instead of aborting."""
    risk = _risk("python_display", (PY_SCENARIO, TS_SCENARIO))

    with pytest.raises(AmbiguousScenario, match="exactly one") as excinfo:
        build_verification_plan(risk, _snapshot("python_storefront"), config=PlanConfig())

    assert isinstance(excinfo.value, NoExecutableScenario) and isinstance(excinfo.value, ValueError)
    assert PY_SCENARIO in str(excinfo.value) and TS_SCENARIO in str(excinfo.value)
    result = not_run_result(risk, f"Not run: {excinfo.value}")
    assert (result.status, result.scenario_id) == ("not_run", "unavailable")
    assert (result.baseline, result.proposed) == (None, None)


def _pointer_of(risk: Risk) -> Evidence:
    (pointer,) = [item for item in risk.evidence if catalog.is_scenario_pointer(item)]
    return pointer


def _declared_pointer(scenario_id: str) -> Evidence:
    """A pointer as a provider could declare it on a change, as if copied into the Risk with the change's evidence."""
    return Evidence(
        id=evidence_id("declared-change", "evidence", scenario_id),
        kind="test",
        visibility="provider_owned",
        source_uri=f"{catalog.SCENARIO_URI_PREFIX}{scenario_id}",
        revision="provider-declared",
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=None,
        excerpt="Executable scenario available, not yet run.",
    )


# Pointers to the Python display scenario that no storage, comparison or TypeScript Risk owns.
FOREIGN_POINTERS = {
    "copied-from-another-risk": lambda: _pointer_of(_risk("python_display", (PY_SCENARIO,))),
    "minted-for-a-dependency-the-risk-does-not-hold": lambda: catalog.scenario_pointer_evidence(
        catalog.get_scenario(PY_SCENARIO),
        _dependency("python_display").model_copy(update={"id": stable_id("test", "absent-dependency")}),
    ),
    "smuggled-through-change-evidence": lambda: _declared_pointer(PY_SCENARIO),
}


@pytest.mark.parametrize("make_pointer", FOREIGN_POINTERS.values(), ids=list(FOREIGN_POINTERS))
@pytest.mark.parametrize(
    ("handle", "category"), [("python_storage", "storage"), ("python_fraud_threshold", "comparison")]
)
def test_foreign_scenario_pointers_never_make_storage_or_comparison_risks_executable(
    handle: str, category: str, make_pointer
) -> None:
    risk = _risk(handle, category=category)
    risk = risk.model_copy(update={"evidence": [*risk.evidence, make_pointer()]})

    with pytest.raises(NoExecutableScenario, match="not bound to the risk's own dependencies") as excinfo:
        build_verification_plan(risk, _snapshot("python_storefront"), config=_trusting("python_storefront"))

    assert not isinstance(excinfo.value, AmbiguousScenario)
    result = not_run_result(risk, f"Not run: {excinfo.value}")
    assert (result.status, result.scenario_id) == ("not_run", "unavailable")


@pytest.mark.parametrize("make_pointer", FOREIGN_POINTERS.values(), ids=list(FOREIGN_POINTERS))
def test_a_foreign_pointer_beside_the_risks_own_pointer_is_ignored(make_pointer) -> None:
    """A Python pointer on the TypeScript display Risk neither replaces its scenario nor makes it ambiguous."""
    risk = _risk("typescript_display", (TS_SCENARIO,))
    risk = risk.model_copy(update={"evidence": [*risk.evidence, make_pointer()]})

    plan = build_verification_plan(risk, _snapshot("ts_storefront"), config=_trusting("ts_storefront"))

    assert (plan.scenario_id, plan.runner_profile) == (TS_SCENARIO, "trusted_node")


def test_a_reserved_uri_on_non_test_evidence_is_no_pointer() -> None:
    declared = _declared_pointer(PY_SCENARIO).model_copy(update={"kind": "declared_change"})
    risk = _risk("python_storage", category="storage")
    risk = risk.model_copy(update={"evidence": [*risk.evidence, declared]})

    with pytest.raises(NoExecutableScenario, match="comparison-only, storage-only or unbound"):
        build_verification_plan(risk, _snapshot("python_storefront"), config=_trusting("python_storefront"))


def test_snapshot_must_be_the_one_the_risk_came_from() -> None:
    with pytest.raises(ValueError, match="derived from repository snapshot"):
        build_verification_plan(_risk("python_display", (PY_SCENARIO,)), _snapshot("ts_storefront"))


# Staging ------------------------------------------------------------------------


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return checkout, tmp_path / "work"


def test_stage_writes_harness_binding_fixture_and_documented_env(tmp_path: Path) -> None:
    checkout, work = _dirs(tmp_path)
    plan = _python_plan()
    spec = catalog.get_scenario(PY_SCENARIO)

    staged = stage_scenario(plan, checkout, "baseline", work)

    root = checkout.resolve()
    harness = root / spec.staged_test_file
    assert staged.staged_dir == root / spec.staged_dir
    assert harness.read_bytes() == (catalog.ASSETS_DIR / spec.harness_asset).read_bytes()
    assert json.loads((staged.staged_dir / "binding.json").read_text(encoding="utf-8")) == {
        "schema_version": "shockwave.v1",
        "scenario_id": PY_SCENARIO,
        "assertion_id": "displayed_price_usd",
        "expected_display": "$10.00",
        "binding": {
            "language": "python",
            "module": "storefront.pricing",
            "function": "display_charge_price",
            "args": ["ch_demo"],
            "http_injection": "python_requests_get",
        },
        "harness_sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
    }
    # The run's only pytest configuration: an empty [pytest] section, written fresh like binding.json.
    config = configparser.ConfigParser()
    config.read_string((staged.staged_dir / "pytest.ini").read_text(encoding="utf-8"))
    assert config.sections() == ["pytest"] and config.items("pytest") == []
    assert set(ENV_KEYS) == {
        "SHOCKWAVE_SCENARIO_ID",
        "SHOCKWAVE_SCENARIO_PHASE",
        "SHOCKWAVE_FIXTURE_ID",
        "SHOCKWAVE_FIXTURE_PATH",
        "SHOCKWAVE_REPORT_PATH",
        "SHOCKWAVE_REPO_ROOT",
    }
    assert set(staged.env) == {*ENV_KEYS, *PYTHON_RUNNER_ENV}
    assert {key: staged.env[key] for key in PYTHON_RUNNER_ENV} == {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONSAFEPATH": "1",
    }
    assert staged.env["SHOCKWAVE_SCENARIO_ID"] == PY_SCENARIO
    assert staged.env["SHOCKWAVE_SCENARIO_PHASE"] == "baseline"
    assert staged.env["SHOCKWAVE_FIXTURE_ID"] == OLD_FIXTURE
    for key in ("SHOCKWAVE_FIXTURE_PATH", "SHOCKWAVE_REPORT_PATH", "SHOCKWAVE_REPO_ROOT"):
        assert Path(staged.env[key]).is_absolute()
    assert staged.env["SHOCKWAVE_REPO_ROOT"] == str(root)
    assert staged.fixture_path == work.resolve() / f"{OLD_FIXTURE}.json"
    assert json.loads(staged.fixture_path.read_text(encoding="utf-8")) == catalog.load_fixture(OLD_FIXTURE)
    assert staged.report_path == work.resolve() / "baseline-report.json"
    assert not staged.report_path.exists()
    assert staged.test_files == (spec.staged_test_file,)
    assert (staged.scenario_id, staged.phase, staged.fixture_id) == (PY_SCENARIO, "baseline", OLD_FIXTURE)


@pytest.mark.parametrize(
    ("phase", "fixture_id", "amount"),
    [("baseline", OLD_FIXTURE, 1000), ("proposed", NEW_FIXTURE, 10), ("patched", NEW_FIXTURE, 10)],
)
def test_stage_selects_the_phase_fixture(tmp_path: Path, phase: Phase, fixture_id: str, amount: int) -> None:
    checkout, work = _dirs(tmp_path)

    staged = stage_scenario(_typescript_plan(), checkout, phase, work)

    assert staged.fixture_id == fixture_id
    assert json.loads(staged.fixture_path.read_text(encoding="utf-8"))["response"]["body"]["amount"] == amount
    assert staged.report_path.name == f"{phase}-report.json"
    assert set(staged.env) == set(ENV_KEYS)  # the pytest hardening applies to Python profiles only
    assert not (staged.staged_dir / "pytest.ini").exists()


def test_stage_makes_fresh_copies_every_run(tmp_path: Path) -> None:
    checkout, work = _dirs(tmp_path)
    plan = _python_plan()
    first = stage_scenario(plan, checkout, "proposed", work)
    harness = checkout / plan.test_files[0]
    harness.write_text("tampered", encoding="utf-8")
    (first.staged_dir / "conftest.py").write_text("stale", encoding="utf-8")
    first.report_path.write_text('{"status": "passed"}', encoding="utf-8")
    first.fixture_path.write_text("{}", encoding="utf-8")

    second = stage_scenario(plan, checkout, "proposed", work)

    assert harness.read_bytes() == (catalog.ASSETS_DIR / "python_display_harness.py").read_bytes()
    assert not (second.staged_dir / "conftest.py").exists()
    assert not second.report_path.exists()
    assert json.loads(second.fixture_path.read_text(encoding="utf-8")) == catalog.load_fixture(NEW_FIXTURE)


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_symlinked_staging_directory_is_refused(tmp_path: Path, depth: int) -> None:
    checkout, work = _dirs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    parts = (".shockwave", "scenarios", PY_SCENARIO)
    if depth > 1:
        checkout.joinpath(*parts[: depth - 1]).mkdir(parents=True)
    checkout.joinpath(*parts[:depth]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        stage_scenario(_python_plan(), checkout, "baseline", work)
    assert list(outside.iterdir()) == []


def test_staging_component_that_is_a_file_is_refused(tmp_path: Path) -> None:
    checkout, work = _dirs(tmp_path)
    (checkout / ".shockwave").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        stage_scenario(_python_plan(), checkout, "baseline", work)


def test_preexisting_symlinked_staged_file_is_replaced_not_followed(tmp_path: Path) -> None:
    checkout, work = _dirs(tmp_path)
    spec = catalog.get_scenario(PY_SCENARIO)
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    staged_dir = checkout / spec.staged_dir
    staged_dir.mkdir(parents=True)
    for name in ("binding.json", spec.staged_test_name, "pytest.ini"):
        (staged_dir / name).symlink_to(victim)

    stage_scenario(_python_plan(), checkout, "baseline", work)

    assert victim.read_text(encoding="utf-8") == "original"
    for name in ("binding.json", spec.staged_test_name, "pytest.ini"):
        assert not (staged_dir / name).is_symlink()


_SPEC = catalog.get_scenario(PY_SCENARIO)


@pytest.mark.parametrize(
    "update",
    [
        {"scenario_id": "demo-payments.unknown"},
        {"test_files": ["conftest.py"]},
        {"test_files": [_SPEC.staged_test_file, "tests/test_other.py"]},
        {"old_fixture_id": NEW_FIXTURE, "new_fixture_id": OLD_FIXTURE},
        {"runner_profile": "trusted_node"},
    ],
    ids=["unknown-scenario", "foreign-test-file", "extra-test-file", "swapped-fixtures", "wrong-profile"],
)
def test_tampered_plans_are_refused(tmp_path: Path, update: dict) -> None:
    checkout, work = _dirs(tmp_path)
    tampered = _python_plan().model_copy(update=update)

    with pytest.raises(ValueError, match="refusing plan"):
        stage_scenario(tampered, checkout, "baseline", work)
    with pytest.raises(ValueError, match="refusing plan"):
        runner_argv_hint(tampered)
    assert not (checkout / ".shockwave").exists()


def test_repo_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    for repo_root in (tmp_path / "missing", a_file):
        with pytest.raises(ValueError, match="existing directory"):
            stage_scenario(_python_plan(), repo_root, "baseline", tmp_path / "work")


def test_work_dir_must_be_outside_the_checkout(tmp_path: Path) -> None:
    checkout, _ = _dirs(tmp_path)

    with pytest.raises(ValueError, match="outside the repository checkout"):
        stage_scenario(_python_plan(), checkout, "baseline", checkout / "work")
    assert not (checkout / "work").exists()


def test_unknown_phase_is_refused(tmp_path: Path) -> None:
    checkout, work = _dirs(tmp_path)
    escaping_phase = cast(Phase, "../escape")

    with pytest.raises(ValueError, match="phase must be one of"):
        stage_scenario(_python_plan(), checkout, escaping_phase, work)


def test_runner_argv_hint_maps_profiles_to_native_runners() -> None:
    python_dir = f".shockwave/scenarios/{PY_SCENARIO}"
    python_file = f"{python_dir}/test_shockwave_display_invariant.py"
    node_file = f".shockwave/scenarios/{TS_SCENARIO}/shockwave_display_invariant.test.mjs"

    # Hermetic: no conftest.py, and the staged pytest.ini/scenario directory as the only config and root.
    expected_python = [
        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest",
        "-c", f"{python_dir}/pytest.ini", "--rootdir", python_dir, python_file,
    ]
    assert runner_argv_hint(_python_plan()) == expected_python
    assert runner_argv_hint(_python_plan(PlanConfig())) == expected_python
    assert runner_argv_hint(_typescript_plan()) == ["node", "--test", node_file]


def test_staging_clip_text_is_the_shared_untrusted_text_renderer() -> None:
    noisy = f"price{chr(0xFEFF)}\n\t{chr(0x202E)}srallod {chr(0x85)}{chr(0x2028)}" + "x" * 300

    assert staging.clip_text(noisy, 40) == text.clip_text(noisy, 40)
    assert staging.clip_text(noisy) == text.clip_text(noisy)
    assert staging.clip_text(f"a{chr(0xFEFF)}b") == "a b"  # the byte-order mark is unsafe too
    assert staging.clip_text("x" * 10, 2) == "xx"  # never longer than the limit


def test_harness_sha256_is_the_asset_digest() -> None:
    for spec in catalog.SCENARIOS.values():
        asset = (catalog.ASSETS_DIR / spec.harness_asset).read_bytes()
        assert harness_sha256(spec) == hashlib.sha256(asset).hexdigest()
