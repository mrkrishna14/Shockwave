"""Acceptance matrix for Kanishk's slice, end to end through the contract functions.

discover_endpoints -> detect_changes -> analyze_impact -> build_verification_plan, then the
development runner double (trusted local fixtures only, not Sai's runner) ->
interpret_verification / interpret_patched_run, on the checked-in Demo Payments fixtures,
plus the pinned Stripe inventory. Sections 1-8 are the rows of the acceptance matrix. The
last section checks the demo CLI (``python -m analyzer.demo``) and that the checked-in
samples are exactly what it writes. Expected values come from the fixtures themselves (id
files, specs, consumer sources, the raw Stripe document), not from copied literals that
could drift. Nothing here touches the network.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from analyzer import demo
from analyzer.changes import detect_changes
from analyzer.demo import (
    LOCAL_DOUBLE_SAMPLE,
    SAMPLE_FILES,
    PatchNotApplicable,
    SimulatedPatch,
)
from analyzer.demo import main as demo_main
from analyzer.impact import (
    analyze_impact,
    match_dependencies,
    risk_category,
    validate_risk_evidence,
)
from analyzer.rules import RULES_LABEL
from discovery import discover, discover_endpoints
from discovery.contracts import (
    AnalysisResult,
    APIChange,
    Dependency,
    DiscoveryInput,
    DiscoveryResult,
    Endpoint,
    RepositorySnapshot,
    Risk,
    VerificationPlan,
    VerificationResult,
)
from discovery.ids import endpoint_id
from discovery.inventory import summarize_inventory
from discovery.pinned import STRIPE_PIN_PATH, load_pinned_document
from discovery.records import FetchedDocument, operation_pointer
from verification.scenarios.catalog import (
    get_scenario,
    load_fixture,
    scenario_ids_from_evidence,
    scenario_pointer_evidence,
)
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import (
    PhaseRun,
    interpret_patched_run,
    interpret_verification,
)
from verification.scenarios.plans import (
    AmbiguousScenario,
    NoExecutableScenario,
    PlanConfig,
    build_verification_plan,
    not_run_result,
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "provider" / "demo_payments"
DEMO_SOURCE = DEMO_DIR / "service"
SAMPLES_DIR = DEMO_DIR / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STRICT_CONTRACTS = FIXTURES / "strict_contracts"
CONSUMER_REPOS = FIXTURES / "consumer_doubles"
PROVIDER_IDS = _load(DEMO_DIR / "fixture_ids.json")
CONSUMER_IDS = _load(FIXTURES / "consumer_fixture_ids.json")
DEPENDENCY_DOCUMENTS = _load(FIXTURES / "demo_dependencies.json")["dependencies"]
DECLARED_DOCUMENT = _load(DEMO_DIR / "changes" / "charge_amount_cents_to_dollars.json")
PROJECT_ID = PROVIDER_IDS["api_project_id"]
STRIPE_REF = "stripe/openapi:latest/openapi.spec3.json"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
NODE = Path(sys.executable).parent / "node"
REGENERATE = (
    "regenerate with: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m analyzer.demo "
    "--execute-local --write-samples examples/provider/demo_payments/samples"
)

CHARGE = ("GET", "/v1/charges/{id}")
RECEIPT = ("GET", "/v1/charges/{id}/receipt")
REFUND = ("GET", "/v1/refunds/{id}")
HEALTH = ("GET", "/healthz")

# Display dependency -> (consumer repository, scenario, the targeted edit removing its /100).
DISPLAY = {
    "python_display": (
        "python_storefront",
        "demo-payments.charge-amount-units.display.python",
        ("storefront/pricing.py", "charge['amount'] / 100", "charge['amount']"),
    ),
    "typescript_display": (
        "ts_storefront",
        "demo-payments.charge-amount-units.display.typescript",
        ("src/pricing.ts", "(charge.amount / 100)", "(charge.amount)"),
    ),
}
PREDICTION_ONLY = {"python_fraud_threshold": "comparison", "python_storage": "storage"}
NEGATIVE_CONTROLS = ("negative_refund_amount", "negative_stripe_service", "negative_local_amount")
PASS_THROUGH = "python_export_pass_through"
CONTRACT_MODELS = (
    AnalysisResult,
    APIChange,
    DiscoveryResult,
    VerificationPlan,
    VerificationResult,
)

requires_runtimes = pytest.mark.skipif(
    not NODE.exists() or importlib.util.find_spec("requests") is None,
    reason=f"local execution needs Node.js >= 22.18 at {NODE} and the requests package",
)


# Helpers --------------------------------------------------------------------------------


def line_of(path: Path, needle: str) -> int:
    matches = [
        number
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if needle in line
    ]
    assert len(matches) == 1, f"{needle!r} must appear exactly once in {path.name}"
    return matches[0]


def discover_demo(contract: str, spec_name: str) -> DiscoveryResult:
    spec = DEMO_DIR / "openapi" / spec_name
    ref = spec.relative_to(REPO_ROOT).as_posix()
    request = DiscoveryInput(
        service_id=PROVIDER_IDS["service_id"],
        snapshot_id=PROVIDER_IDS["contract_snapshots"][contract],
        openapi_documents=[ref],
        source_snapshot_id=PROVIDER_IDS["source_snapshot_id"],
        frameworks=["fastapi"],
    )
    return discover_endpoints(request, {ref: FetchedDocument(spec.read_bytes(), source_uri=ref)}, DEMO_SOURCE)


def by_key(result: DiscoveryResult) -> dict[tuple[str, str], Endpoint]:
    return {(endpoint.method, endpoint.path): endpoint for endpoint in result.endpoints}


def risk_for(analysis: AnalysisResult, dependency: Dependency) -> Risk:
    (risk,) = [risk for risk in analysis.risks if dependency.id in risk.dependency_ids]
    return risk


def edit(relative: str, old: str, new: str) -> Callable[[Path], None]:
    def apply(checkout: Path) -> None:
        path = checkout / relative
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, f"the edit does not apply to {relative}"
        path.write_text(text.replace(old, new), encoding="utf-8")

    return apply


def assert_round_trips(output: Any) -> None:
    model = type(output)
    assert model in CONTRACT_MODELS, model
    assert model.model_validate_json(output.model_dump_json()) == output
    assert model.model_validate(json.loads(json.dumps(output.model_dump(mode="json")))) == output


def raw_operation_count(document: dict[str, Any]) -> int:
    return sum(
        1
        for path, item in document["paths"].items()
        if not path.startswith("x-")
        for method in item
        if method in HTTP_METHODS
    )


# Fixtures -------------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def no_network() -> Iterator[None]:
    """Module-scoped and autouse, so it is active before the module-scoped fixtures run."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the end-to-end pipeline must never open a network connection")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", refuse)
        patch.setattr(socket.socket, "connect_ex", refuse)
        patch.setattr(socket, "create_connection", refuse)
        patch.setattr(socket, "getaddrinfo", refuse)
        yield


@pytest.fixture(scope="module")
def v1() -> DiscoveryResult:
    return discover_demo("v1", "demo-payments.v1.yaml")


@pytest.fixture(scope="module")
def v2() -> DiscoveryResult:
    return discover_demo("v2_proposed", "demo-payments.v2.yaml")


@pytest.fixture(scope="module")
def declared() -> APIChange:
    return APIChange.model_validate(DECLARED_DOCUMENT)


@pytest.fixture(scope="module")
def changes(v1: DiscoveryResult, v2: DiscoveryResult, declared: APIChange) -> list[APIChange]:
    return detect_changes(v1, v2, [declared], api_project_id=PROJECT_ID)


@pytest.fixture(scope="module")
def change(changes: list[APIChange]) -> APIChange:
    (only,) = changes
    return only


@pytest.fixture(scope="module")
def dependencies() -> dict[str, Dependency]:
    return {handle: Dependency.model_validate(document) for handle, document in DEPENDENCY_DOCUMENTS.items()}


@pytest.fixture(scope="module")
def analysis(v1: DiscoveryResult, change: APIChange, dependencies: dict[str, Dependency]) -> AnalysisResult:
    # The dependency doubles were keyed to the v1 catalog, which is also the change's old snapshot.
    return analyze_impact(change, list(dependencies.values()), endpoints=v1.endpoints)


@pytest.fixture(scope="module")
def snapshots() -> dict[str, RepositorySnapshot]:
    return {
        handle: RepositorySnapshot(
            id=ids["repository_snapshot_id"],
            repository_connection_id=ids["repository_connection_id"],
            commit_sha=ids["commit_sha"],
            fetched_at=datetime(2026, 10, 16, tzinfo=UTC),
            content_hash="sha256:" + "0" * 64,  # placeholder: plans never read it
        )
        for handle, ids in CONSUMER_IDS["repositories"].items()
    }


@pytest.fixture(scope="module")
def trusted(snapshots: dict[str, RepositorySnapshot]) -> PlanConfig:
    return PlanConfig(frozenset(snapshot.repository_connection_id for snapshot in snapshots.values()))


@pytest.fixture(scope="module")
def plans(
    analysis: AnalysisResult,
    dependencies: dict[str, Dependency],
    snapshots: dict[str, RepositorySnapshot],
    trusted: PlanConfig,
) -> dict[str, VerificationPlan]:
    return {
        handle: build_verification_plan(risk_for(analysis, dependencies[handle]), snapshots[repository], config=trusted)
        for handle, (repository, _, _) in DISPLAY.items()
    }


@pytest.fixture(scope="module")
def not_run_results(
    analysis: AnalysisResult,
    dependencies: dict[str, Dependency],
    snapshots: dict[str, RepositorySnapshot],
    trusted: PlanConfig,
) -> dict[str, VerificationResult]:
    results = {}
    for handle in PREDICTION_ONLY:
        risk = risk_for(analysis, dependencies[handle])
        with pytest.raises(NoExecutableScenario) as missing:
            build_verification_plan(risk, snapshots["python_storefront"], config=trusted)
        results[handle] = not_run_result(risk, f"Not run: {missing.value}")
    return results


@pytest.fixture(scope="module")
def executions(
    plans: dict[str, VerificationPlan], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, dict[str, PhaseRun]]:
    runs: dict[str, dict[str, PhaseRun]] = {}
    for handle, plan in plans.items():
        repository, _, targeted_edit = DISPLAY[handle]
        consumer, work = CONSUMER_REPOS / repository, tmp_path_factory.mktemp(handle)
        runs[handle] = {
            phase: run_phase_locally(
                plan,
                consumer,
                phase,
                work_dir=work,
                node_executable=str(NODE),
                patch=edit(*targeted_edit) if phase == "patched" else None,
            )
            for phase in ("baseline", "proposed", "patched")
        }
    return runs


@pytest.fixture(scope="module")
def stripe_operation_count() -> int:
    return raw_operation_count(json.loads(load_pinned_document(STRIPE_PIN_PATH).content))


# 1. Discovery: documented vs implemented vs not found in scan ------------------------------


def test_1_demo_payments_reconciliation(v1: DiscoveryResult, v2: DiscoveryResult) -> None:
    expected = {
        CHARGE: "documented_and_found",
        RECEIPT: "implementation_only",
        REFUND: "documented_not_found_in_scan",
        HEALTH: "not_compared",
    }
    for result in (v1, v2):
        assert {key: endpoint.reconciliation for key, endpoint in by_key(result).items()} == expected

    charge, receipt, refund = (by_key(v1)[key] for key in (CHARGE, RECEIPT, REFUND))
    charges_py = DEMO_SOURCE / "demo_payments" / "charges.py"
    refunds_py = DEMO_SOURCE / "demo_payments" / "refunds.py"
    v1_snapshot = PROVIDER_IDS["contract_snapshots"]["v1"]
    assert str(charge.id) == DECLARED_DOCUMENT["endpoint_id"] == endpoint_id(v1_snapshot, "demo-payments", *CHARGE)
    assert sorted(item.kind for item in charge.evidence) == ["openapi", "source"]
    receipt_line = line_of(charges_py, '@router.get("/{id}/receipt")')
    assert [(item.kind, item.file, item.line_start) for item in receipt.evidence] == [
        ("source", "demo_payments/charges.py", receipt_line)
    ]
    assert [item.kind for item in refund.evidence] == ["openapi"]
    prefix_line = line_of(refunds_py, "router = APIRouter(")
    assert v1.coverage.unsupported_patterns == [f"fastapi:dynamic-prefix demo_payments/refunds.py:{prefix_line}"]
    assert any("not found in scan is not proof of absence" in warning for warning in v1.warnings)


# 2. Changes: schema diff alone vs explicit declared evidence ------------------------------


def test_2_schema_diff_alone_reports_no_change(v1: DiscoveryResult, v2: DiscoveryResult) -> None:
    assert detect_changes(v1, v2, [], api_project_id=PROJECT_ID) == []


def test_2_declared_evidence_yields_one_units_change_with_the_operation_evidence(
    v1: DiscoveryResult, changes: list[APIChange], declared: APIChange
) -> None:
    charge = by_key(v1)[CHARGE]
    assert [(c.id, c.kind, c.field_path, c.endpoint_id, c.service_id, c.structural_breaking) for c in changes] == [
        (declared.id, "units_changed", "response.amount", charge.id, "demo-payments", False)
    ]
    (change,) = changes
    assert change.evidence[: len(declared.evidence)] == declared.evidence
    operation = [item for item in change.evidence if item.kind == "openapi"]
    assert operation == [item for item in charge.evidence if item.kind == "openapi"]
    assert [item.json_pointer for item in operation] == [operation_pointer("/v1/charges/{id}", "get")]


# 3. Impact: what matches, what does not, what stays unknown -------------------------------


def test_3_display_dependencies_become_conversion_risks_with_scenario_pointers(
    analysis: AnalysisResult, change: APIChange, dependencies: dict[str, Dependency]
) -> None:
    for handle, (repository, scenario, _) in DISPLAY.items():
        dependency = dependencies[handle]
        risk = risk_for(analysis, dependency)
        assert risk.dependency_ids == [dependency.id]
        assert (risk_category(change, risk), risk.severity, risk.confidence) == ("conversion", "high", "direct")
        assert str(risk.repository_snapshot_id) == CONSUMER_IDS["repositories"][repository]["repository_snapshot_id"]
        assert scenario_ids_from_evidence(list(risk.evidence)) == [scenario]
        assert "the result becomes $0.10 instead of $10.00" in risk.explanation


def test_3_negative_controls_match_nothing(
    analysis: AnalysisResult, change: APIChange, dependencies: dict[str, Dependency]
) -> None:
    referenced = {*analysis.unresolved_dependency_ids, *(item for risk in analysis.risks for item in risk.dependency_ids)}
    for handle in NEGATIVE_CONTROLS:
        dependency = dependencies[handle]
        assert match_dependencies(change, [dependency]) == ([], [])
        assert dependency.id not in referenced
    # The fictional change is never attributed to Stripe.
    assert dependencies["negative_stripe_service"].service_id == "stripe" != change.service_id
    assert "stripe" not in analysis.model_dump_json().lower()


def test_3_storage_and_comparison_are_predictions_without_scenario_pointers(
    analysis: AnalysisResult, change: APIChange, dependencies: dict[str, Dependency]
) -> None:
    for handle, category in PREDICTION_ONLY.items():
        risk = risk_for(analysis, dependencies[handle])
        assert (risk_category(change, risk), risk.confidence) == (category, "inferred")
        assert [item for item in risk.evidence if item.kind == "test"] == []
        assert RULES_LABEL in risk.explanation
    assert "not confirmed" in risk_for(analysis, dependencies["python_storage"]).explanation


def test_3_pass_through_is_unresolved_and_everything_is_a_prediction(
    analysis: AnalysisResult, dependencies: dict[str, Dependency]
) -> None:
    assert analysis.analysis_mode == "rules"
    assert analysis.unresolved_dependency_ids == [dependencies[PASS_THROUGH].id]
    assert len(analysis.risks) == len(DISPLAY) + len(PREDICTION_ONLY)
    assert any("unknown, not safe" in warning for warning in analysis.warnings)
    assert any(warning.startswith("predictions only: no tests have run") for warning in analysis.warnings)
    for risk in analysis.risks:
        assert RULES_LABEL in risk.explanation
        assert "verified" not in risk.explanation.lower()


# 4. Risk evidence references real dependencies and locations ------------------------------


def test_4_risk_evidence_references_are_real(
    v1: DiscoveryResult, analysis: AnalysisResult, change: APIChange, dependencies: dict[str, Dependency]
) -> None:
    assert validate_risk_evidence(analysis, change, list(dependencies.values()), endpoints=v1.endpoints) == []
    by_id = {str(dependency.id): dependency for dependency in dependencies.values()}
    repositories = {ids["repository_snapshot_id"]: handle for handle, ids in CONSUMER_IDS["repositories"].items()}
    for risk in analysis.risks:
        for dependency_id in risk.dependency_ids:
            for item in by_id[str(dependency_id)].evidence:
                assert item in risk.evidence  # verbatim
                source = CONSUMER_REPOS / repositories[str(risk.repository_snapshot_id)] / item.file
                lines = source.read_text(encoding="utf-8").splitlines()
                assert 1 <= item.line_start <= item.line_end <= len(lines)
                assert "amount" in "\n".join(lines[item.line_start - 1 : item.line_end])


# 5. Plans for executable risks; not_run for the rest --------------------------------------


def test_5_display_risks_get_plans_on_trusted_fixture_repositories(
    analysis: AnalysisResult,
    dependencies: dict[str, Dependency],
    snapshots: dict[str, RepositorySnapshot],
    plans: dict[str, VerificationPlan],
) -> None:
    profiles = {"python_storefront": "trusted_python", "ts_storefront": "trusted_node"}
    for handle, (repository, scenario, _) in DISPLAY.items():
        plan, risk = plans[handle], risk_for(analysis, dependencies[handle])
        assert (plan.risk_id, plan.repository_snapshot_id, plan.scenario_id) == (
            risk.id,
            risk.repository_snapshot_id,
            scenario,
        )
        assert plan.runner_profile == profiles[repository]
        old, new = load_fixture(plan.old_fixture_id), load_fixture(plan.new_fixture_id)
        assert (old["contract"], old["response"]["body"]["amount"]) == ("current", 1000)
        assert (new["contract"], new["response"]["body"]["amount"]) == ("proposed", 10)
        assert "$10.00" in plan.expected_behavior
        # Without explicit trust the same risk would need a sandbox.
        untrusted = build_verification_plan(risk, snapshots[repository], config=PlanConfig())
        assert untrusted.runner_profile.startswith("sandbox_")


def test_5_storage_and_comparison_risks_have_no_plan_and_stay_not_run(
    analysis: AnalysisResult, not_run_results: dict[str, VerificationResult], dependencies: dict[str, Dependency]
) -> None:
    for handle in PREDICTION_ONLY:
        result = not_run_results[handle]
        assert (result.status, result.baseline, result.proposed) == ("not_run", None, None)
        assert result.risk_id == risk_for(analysis, dependencies[handle]).id
        assert "no executable scenario" in result.summary


# 6. Native execution on the local runner double -------------------------------------------


@requires_runtimes
def test_6_local_double_verifies_both_display_regressions(
    plans: dict[str, VerificationPlan], executions: dict[str, dict[str, PhaseRun]]
) -> None:
    for handle, plan in plans.items():
        baseline, proposed = executions[handle]["baseline"], executions[handle]["proposed"]
        result = interpret_verification(plan, baseline, proposed)
        assert result.status == "verified_regression", result.summary
        assert "expected $10.00, actual $0.10" in result.summary
        assert (result.baseline, result.proposed) == (baseline.execution, proposed.execution)
        assert baseline.execution.exit_code == 0 and proposed.execution.exit_code not in (0, None)
        assert_round_trips(result)


@requires_runtimes
def test_6_patched_display_code_is_validated_with_the_same_expectation(
    plans: dict[str, VerificationPlan], executions: dict[str, dict[str, PhaseRun]]
) -> None:
    for handle, plan in plans.items():
        outcome = interpret_patched_run(plan, executions[handle]["patched"])
        assert outcome.status == "validated", outcome.reason
        assert "expected $10.00, actual $10.00" in outcome.reason
        repository, _, (relative, old, _) = DISPLAY[handle]
        assert old in (CONSUMER_REPOS / repository / relative).read_text(encoding="utf-8")  # copy only


def test_6_nothing_is_verified_without_execution(
    plans: dict[str, VerificationPlan], not_run_results: dict[str, VerificationResult]
) -> None:
    for plan in plans.values():
        assert interpret_verification(plan, None, None).status == "not_run"
    assert {result.status for result in not_run_results.values()} == {"not_run"}


# 7. Every output is a valid contract model and survives JSON ------------------------------


def test_7_every_output_validates_against_the_contracts_and_round_trips(
    v1: DiscoveryResult,
    v2: DiscoveryResult,
    changes: list[APIChange],
    analysis: AnalysisResult,
    plans: dict[str, VerificationPlan],
    not_run_results: dict[str, VerificationResult],
) -> None:
    for output in (v1, v2, *changes, analysis, *plans.values(), *not_run_results.values()):
        assert_round_trips(output)


# 8. Stripe inventory is computed from the pinned document ---------------------------------


def test_8_stripe_inventory_count_equals_the_raw_operation_count(stripe_operation_count: int) -> None:
    pin = _load(STRIPE_PIN_PATH)
    fetched = load_pinned_document(STRIPE_PIN_PATH)
    raw = json.loads(fetched.content)
    stripe = PROVIDER_IDS["stripe"]
    request = DiscoveryInput(
        service_id=stripe["service_id"],
        snapshot_id=stripe["contract_snapshot_id"],
        openapi_documents=[STRIPE_REF],
        source_snapshot_id=None,
        frameworks=[],
    )
    outcome = discover(request, {STRIPE_REF: fetched}, None)
    summary = summarize_inventory(outcome)

    assert stripe_operation_count == raw_operation_count(raw) > 0
    assert summary["endpoint_count"] == summary["documented_operation_count"] == stripe_operation_count
    (document,) = summary["documents"]
    assert document["operation_count"] == stripe_operation_count
    assert (document["sha256"], document["visibility"]) == (pin["content"]["sha256"], "public")
    assert document["info_version"] == raw["info"]["version"]
    assert {endpoint.service_id for endpoint in outcome.result.endpoints} == {"stripe"}
    assert DECLARED_DOCUMENT["endpoint_id"] not in {endpoint.id for endpoint in outcome.result.endpoints}


# Demo CLI and the checked-in samples -------------------------------------------------------


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[int, str, Path]:
    directory = tmp_path_factory.mktemp("cli-samples")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = demo_main(["--execute-local", "--write-samples", str(directory)])
    return code, stdout.getvalue(), directory


@requires_runtimes
def test_cli_report_labels_every_state(cli_run: tuple[int, str, Path], analysis: AnalysisResult) -> None:
    code, report, _ = cli_run
    assert code == 0, report
    for label in (
        "public contract discovery; not Stripe production source",
        "schema diff alone: 0 changes",
        "with declared evidence: units_changed, structural_breaking=false",
        "analysis_mode=rules: deterministic rules, not model inference",
        "evidence references (validate_risk_evidence): 0 problems",
        "[6] Verification - LOCAL RUNNER DOUBLE - trusted local fixtures, not a sandbox, not Sai's runner",
        "documented_and_found",
        "implementation_only",
        "documented_not_found_in_scan",
    ):
        assert label in report
    assert report.count("    PREDICTED  risk ") == len(analysis.risks)
    assert report.count("result: verified_regression") == len(DISPLAY)
    assert report.count("simulated edit): validated") == len(DISPLAY)
    assert report.count("    NOT_RUN  risk ") == len(PREDICTION_ONLY)


def test_cli_without_execution_claims_no_verification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / LOCAL_DOUBLE_SAMPLE
    stale.write_text("left over from an earlier run\n", encoding="utf-8")

    code = demo_main(["--write-samples", str(tmp_path)])

    report = capsys.readouterr().out
    assert code == 0, report
    assert "[6] Verification - not executed" in report
    for claim in ("verified_regression", "validated", "LOCAL RUNNER DOUBLE"):
        assert claim not in report
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([*SAMPLE_FILES, LOCAL_DOUBLE_SAMPLE])
    assert stale.read_text(encoding="utf-8") == "left over from an earlier run\n"  # never touched
    for name in SAMPLE_FILES:
        assert (tmp_path / name).read_bytes() == (SAMPLES_DIR / name).read_bytes(), f"{name} is stale; {REGENERATE}"


@requires_runtimes
def test_checked_in_samples_are_exactly_what_the_cli_writes(cli_run: tuple[int, str, Path]) -> None:
    _, _, directory = cli_run
    names = sorted([*SAMPLE_FILES, LOCAL_DOUBLE_SAMPLE])
    assert sorted(path.name for path in directory.iterdir()) == names
    assert sorted(path.name for path in SAMPLES_DIR.iterdir()) == names
    for name in names:
        assert (directory / name).read_bytes() == (SAMPLES_DIR / name).read_bytes(), f"{name} is stale; {REGENERATE}"


def test_samples_are_contract_shaped_deterministic_and_free_of_local_paths(
    v1: DiscoveryResult,
    change: APIChange,
    analysis: AnalysisResult,
    plans: dict[str, VerificationPlan],
    not_run_results: dict[str, VerificationResult],
    stripe_operation_count: int,
) -> None:
    documents = {}
    for name in [*SAMPLE_FILES, LOCAL_DOUBLE_SAMPLE]:
        text = (SAMPLES_DIR / name).read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"  # sorted, 2-space
        for local in (str(REPO_ROOT), str(Path.home()), tempfile.gettempdir(), "/private/"):
            assert local not in text, f"{name} leaks a local path"
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text), f"{name} contains a timestamp"
        if name != LOCAL_DOUBLE_SAMPLE:
            assert "verified_regression" not in text and '"validated"' not in text
        documents[name] = json.loads(text)

    assert documents["stripe_inventory_summary.json"]["endpoint_count"] == stripe_operation_count
    assert DiscoveryResult.model_validate(documents["demo_payments_discovery_v1.json"]) == v1
    changes_document = documents["demo_payments_changes.json"]
    assert changes_document["schema_diff_only"] == []
    assert [APIChange.model_validate(item) for item in changes_document["with_declared_change"]] == [change]
    assert AnalysisResult.model_validate(documents["demo_payments_analysis.json"]) == analysis
    planned = documents["demo_payments_verification_plans.json"]
    assert {VerificationPlan.model_validate(item).id for item in planned["plans"]} == {p.id for p in plans.values()}
    assert [VerificationResult.model_validate(item) for item in planned["not_run"]] == sorted(
        not_run_results.values(), key=lambda result: [risk.id for risk in analysis.risks].index(result.risk_id)
    )

    local = documents[LOCAL_DOUBLE_SAMPLE]
    assert local["runner"] == (
        "dev_runner_double (local, trusted fixtures only; plain subprocess, not a sandbox; not Sai's runner)"
    )
    assert "duration_ms is normalized to 0" in local["timing"]
    results = [VerificationResult.model_validate(item) for item in local["results"]]
    assert {result.risk_id for result in results} == {plan.risk_id for plan in plans.values()}
    for result in results:
        assert result.status == "verified_regression" and "actual $0.10" in result.summary
        assert result.summary.startswith(demo.LOCAL_DOUBLE_RESULT_PREFIX)  # the label travels with the record
        assert result.baseline.duration_ms == result.proposed.duration_ms == 0
    assert [entry["status"] for entry in local["patched"]] == ["validated"] * len(DISPLAY)
    assert {entry["patch"]["file"] for entry in local["patched"]} == {edit[0] for _, _, edit in DISPLAY.values()}


def test_simulated_patch_removes_exactly_one_conversion_or_refuses(tmp_path: Path) -> None:
    source = tmp_path / "pricing.py"
    source.write_text("x = 1\nreturn f\"${charge['amount'] / 100:.2f}\"\n", encoding="utf-8")
    SimulatedPatch("pricing.py", 2, 2, "100").apply(tmp_path)
    assert source.read_text(encoding="utf-8") == "x = 1\nreturn f\"${charge['amount']:.2f}\"\n"

    for text in ("total // 100\n", "total / 1000\n", "a / 100 + b / 100\n", "total * 100\n"):
        source.write_text(text, encoding="utf-8")
        with pytest.raises(PatchNotApplicable):
            SimulatedPatch("pricing.py", 1, 1, "100").apply(tmp_path)
        assert source.read_text(encoding="utf-8") == text  # a refused edit changes nothing
    with pytest.raises(PatchNotApplicable):
        SimulatedPatch("pricing.py", 5, 5, "100").apply(tmp_path)


# Demo honesty and integration regressions ------------------------------------------------


PROVIDER_PROBE = """
import importlib, sys
from fastapi.testclient import TestClient
app = importlib.import_module("demo_payments.main").app
client = TestClient(app)  # in-process ASGI transport; no socket is opened
print(client.get("/v1/charges/ch_demo").json()["amount"], client.get("/v1/refunds/re_demo").json()["amount"])
"""


@pytest.mark.skipif(importlib.util.find_spec("httpx") is None, reason="fastapi.testclient needs httpx")
def test_fictional_provider_changes_only_the_declared_charge_amount() -> None:
    """The refund display is a negative control only if the provider keeps refunds in cents."""
    amounts = {}
    for contract in ("v1", "v2"):
        env = {**os.environ, "DEMO_PAYMENTS_CONTRACT": contract, "PYTHONDONTWRITEBYTECODE": "1"}
        env["PYTHONPATH"] = str(DEMO_SOURCE)
        completed = subprocess.run(
            [sys.executable, "-c", PROVIDER_PROBE],
            check=False,
            cwd=DEMO_SOURCE,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        amounts[contract] = completed.stdout.split()
    assert amounts == {"v1": ["1000", "250"], "v2": ["10", "250"]}


def test_demo_matches_dependencies_through_the_v1_endpoint_records(
    monkeypatch: pytest.MonkeyPatch, v1: DiscoveryResult
) -> None:
    seen: dict[str, object] = {}

    def spy(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            seen[name] = kwargs.get("endpoints")
            return function(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(demo, "analyze_impact", spy("analyze", analyze_impact))
    monkeypatch.setattr(demo, "validate_risk_evidence", spy("validate", validate_risk_evidence))
    run = demo.run_demo()
    assert seen["analyze"] == seen["validate"] == list(v1.endpoints)
    report = demo.render_report(run)
    # A different endpoint record is named by its identity, not just called "different".
    assert "storefront/refunds.py:10: a different endpoint (GET /v1/refunds/{id})" in report


def test_report_prints_every_suggested_action_and_the_patch_caveat(analysis: AnalysisResult) -> None:
    run = demo.run_demo()
    report = demo.render_report(run)
    assert report.count("      suggested action: ") == len(analysis.risks)
    assert "validated" not in report  # nothing ran, so nothing is called validated
    assert "pinned to the proposed contract" in demo.PATCH_NOTE
    assert "coordinated with the provider's cutover" in demo.PATCH_NOTE


def test_local_double_labels_are_explicit_and_keep_result_ids() -> None:
    for label in (demo.LOCAL_DOUBLE_LABEL, demo.LOCAL_DOUBLE_RUNNER, demo.LOCAL_DOUBLE_RESULT_PREFIX):
        assert "not a sandbox" in label and "not Sai's runner" in label
    plan_result = VerificationResult(
        schema_version="shockwave.v1",
        id="00000000-0000-5000-8000-000000000001",
        risk_id="00000000-0000-5000-8000-000000000002",
        repository_snapshot_id="00000000-0000-5000-8000-000000000003",
        status="not_run",
        baseline=None,
        proposed=None,
        scenario_id="demo-payments.charge-amount-units.display.python",
        summary="Not run.",
        artifact_ids=[],
    )
    labeled = demo.labeled_local_result(plan_result)
    assert labeled.summary == f"{demo.LOCAL_DOUBLE_RESULT_PREFIX} Not run."
    assert labeled.model_copy(update={"summary": plan_result.summary}) == plan_result  # only the summary changes


def test_fixture_mode_is_labeled_a_replayed_fixture_not_a_recording() -> None:
    label = demo.ANALYSIS_MODE_LABELS["fixture"]
    assert "not live inference" in label and "fixture" in label
    assert "recorded" not in label


def test_a_risk_bound_to_two_scenarios_is_planned_as_not_run(
    analysis: AnalysisResult, change: APIChange, dependencies: dict[str, Dependency]
) -> None:
    python_risk = risk_for(analysis, dependencies["python_display"])
    typescript = dependencies["typescript_display"]
    mixed = python_risk.model_copy(
        update={
            "dependency_ids": [*python_risk.dependency_ids, typescript.id],
            "evidence": [
                *python_risk.evidence,
                scenario_pointer_evidence(get_scenario(DISPLAY["typescript_display"][1]), typescript),
            ],
        }
    )
    only_mixed = analysis.model_copy(update={"risks": [mixed]})
    repositories = demo.consumer_repositories(CONSUMER_IDS)
    plans, not_run = demo.plan_verifications(only_mixed, repositories, demo.trusted_fixture_config(repositories))
    assert plans == []
    assert [result.status for result in not_run] == ["not_run"]
    with pytest.raises(AmbiguousScenario):
        build_verification_plan(mixed, next(iter(repositories.values())).snapshot)
    run = demo.run_demo()
    report = demo.render_report(
        demo.DemoRun(**{**run.__dict__, "analysis": only_mixed, "plans": plans, "not_run": not_run})
    )
    assert "2 executable scenarios are bound, so no single plan proves it" in " ".join(report.split())


def test_demo_runs_against_uuid_typed_strict_contracts() -> None:
    env = {key: value for key, value in os.environ.items() if key != "SHOCKWAVE_CONTRACTS"}
    env.update(PYTHONPATH=os.pathsep.join([str(REPO_ROOT), str(STRICT_CONTRACTS)]), PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [sys.executable, "-m", "analyzer.demo"],
        check=False,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert "contracts: shared.schemas" in completed.stdout
    assert "evidence references (validate_risk_evidence): 0 problems" in completed.stdout
    assert completed.stdout.count(" conversion high/direct ") == len(DISPLAY)
