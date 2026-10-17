"""Acceptance tests for deterministic impact analysis (analyzer.impact + analyzer.rules).

The declared cents-to-dollars change is enriched with the operation-level OpenAPI evidence
exactly as ``detect_changes`` adds it, then analysed against the contract-shaped demo
dependencies (tests/analyzer/fixtures/demo_dependencies.json). Nothing here touches the network
or executes consumer code.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from analyzer.impact import (
    NO_MATCH_WARNING,
    ImpactAnalyzer,
    analyze_impact,
    analyze_with_rules,
    build_risk,
    configure_default_analyzer,
    default_analyzer,
    match_dependencies,
    risk_category,
    validate_risk_evidence,
)
from analyzer.rules import (
    MAX_ACTION_CHARS,
    MAX_EXPLANATION_CHARS,
    RuleOutcome,
    UnitsContext,
    classify_dependency,
    units_context,
)
from discovery.contracts import (
    AnalysisResult,
    APIChange,
    Dependency,
    Endpoint,
    Evidence,
    RepositorySnapshot,
)
from discovery.ids import endpoint_id, evidence_id, risk_id, stable_id
from discovery.records import operation_pointer
from verification.scenarios.catalog import SCENARIO_URI_PREFIX, bound_scenario_ids
from verification.scenarios.plans import PlanConfig, build_verification_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
V1_SPEC = "examples/provider/demo_payments/openapi/demo-payments.v1.yaml"
DECLARED_CHANGE = REPO_ROOT / "examples/provider/demo_payments/changes/charge_amount_cents_to_dollars.json"
DEMO_DEPENDENCIES = REPO_ROOT / "tests/analyzer/fixtures/demo_dependencies.json"
STRICT_CONTRACTS = REPO_ROOT / "tests/analyzer/fixtures/strict_contracts"
PYTHON_SCENARIO = "demo-payments.charge-amount-units.display.python"
TYPESCRIPT_SCENARIO = "demo-payments.charge-amount-units.display.typescript"
NEGATIVE_CONTROLS = ("negative_refund_amount", "negative_stripe_service", "negative_local_amount")
SYNTHETIC_ENDPOINT = stable_id("test-endpoint", "GET", "/v1/widgets/{id}")
CHARGE_PATH = "/v1/charges/{id}"
REFUND_PATH = "/v1/refunds/{id}"
USD_CENTS = "USD amount in minor currency units (cents)"
USD_DOLLARS = "USD amount in major currency units (dollars)"


# Fixtures and builders ---------------------------------------------------------------------------


def enriched_change_document() -> dict:
    document = json.loads(DECLARED_CHANGE.read_text(encoding="utf-8"))
    digest = hashlib.sha256((REPO_ROOT / V1_SPEC).read_bytes()).hexdigest()
    pointer = operation_pointer("/v1/charges/{id}", "get")
    openapi = Evidence(
        id=evidence_id("openapi", digest, pointer),
        kind="openapi",
        visibility="provider_owned",
        source_uri=V1_SPEC,
        revision=f"sha256:{digest}",
        json_pointer=pointer,
    )
    document["evidence"].append(openapi.model_dump(mode="json"))
    return document


@pytest.fixture
def change() -> APIChange:
    return APIChange.model_validate(enriched_change_document())


@pytest.fixture
def deps() -> dict[str, Dependency]:
    documents = json.loads(DEMO_DEPENDENCIES.read_text(encoding="utf-8"))["dependencies"]
    return {handle: Dependency.model_validate(document) for handle, document in documents.items()}


def synthetic_dependency(
    name: str,
    *,
    operation: str,
    field_path: str = "response.amount",
    operand: object = None,
    destination: str | None = None,
    confidence: str = "direct",
    snapshot: str = "snapshot-a",
    enrollment: str = "enrollment-a",
    org: str = "org-a",
    endpoint_id: str = SYNTHETIC_ENDPOINT,
    line: int = 7,
) -> Dependency:
    file = f"app/{name}.py"
    return Dependency.model_validate(
        {
            "id": stable_id("test-dependency", name),
            "consumer_org_id": stable_id("test-org", org),
            "enrollment_id": stable_id("test-enrollment", enrollment),
            "repository_snapshot_id": stable_id("test-snapshot", snapshot),
            "service_id": "demo-payments",
            "endpoint_id": endpoint_id,
            "language": "python",
            "field_path": field_path,
            "origin": {"kind": "http_response", "library": "requests", "call": "requests.get"},
            "usage": {"operation": operation, "operand": operand, "destination": destination},
            "confidence": confidence,
            "evidence": [
                {
                    "id": stable_id("test-evidence", name),
                    "kind": "source",
                    "visibility": "consumer_private",
                    "source_uri": f"repository-snapshot://{snapshot}/{file}",
                    "revision": "0123abcd",
                    "file": file,
                    "line_start": line,
                    "line_end": line,
                    "json_pointer": None,
                    "excerpt": f"# usage {name}",
                }
            ],
        }
    )


def synthetic_change(
    kind: str,
    *,
    field_path: str = "response.amount",
    old: str = "",
    new: str = "",
    breaking: bool = False,
) -> APIChange:
    return APIChange.model_validate(
        {
            "id": stable_id("test-change", kind, field_path, old, new, breaking),
            "api_project_id": stable_id("test-project"),
            "service_id": "demo-payments",
            "endpoint_id": SYNTHETIC_ENDPOINT,
            "old_snapshot_id": stable_id("test-contract", "old"),
            "new_snapshot_id": stable_id("test-contract", "new"),
            "field_path": field_path,
            "kind": kind,
            "old_semantics": old,
            "new_semantics": new,
            "structural_breaking": breaking,
            "evidence": [
                {
                    "id": stable_id("test-change-evidence", kind, field_path),
                    "kind": "declared_change",
                    "visibility": "provider_owned",
                    "source_uri": "examples/provider/synthetic/change.json",
                    "revision": "synthetic@1",
                    "json_pointer": "",
                    "excerpt": f"{kind} on {field_path}",
                }
            ],
        }
    )


def risk_with(result: AnalysisResult, dependency: Dependency):
    matches = [risk for risk in result.risks if dependency.id in risk.dependency_ids]
    assert len(matches) == 1, f"expected exactly one risk for {dependency.id}"
    return matches[0]


def scenario_evidence(risk) -> list[Evidence]:
    return [item for item in risk.evidence if item.kind == "test"]


def everything(result: AnalysisResult) -> set[str]:
    ids = set(result.unresolved_dependency_ids)
    for risk in result.risks:
        ids.update(risk.dependency_ids)
    return ids


def in_snapshot_of(dependency: Dependency, anchor: Dependency) -> Dependency:
    """``dependency`` moved into ``anchor``'s organization, enrollment and repository snapshot."""
    owner = ("consumer_org_id", "enrollment_id", "repository_snapshot_id")
    return dependency.model_copy(update={field: getattr(anchor, field) for field in owner})


def odd_divide(anchor: Dependency) -> Dependency:
    """A divide-by-1000 next to ``anchor`` (storefront/reports.py:7): no scenario is bound to it."""
    document = anchor.model_dump(mode="json")
    document["id"] = stable_id("test-dependency", "reports")
    document["usage"] = {"operation": "divide", "operand": 1000, "destination": None}
    location = {"file": "storefront/reports.py", "line_start": 7, "line_end": 7, "excerpt": "x / 1000"}
    document["evidence"] = [{**document["evidence"][0], **location, "id": stable_id("test-evidence", "reports")}]
    return Dependency.model_validate(document)


def smuggled_pointer() -> dict:
    """Evidence using the reserved scenario scheme, as a provider could declare it."""
    return {
        "id": stable_id("test-smuggled-pointer"),
        "kind": "test",
        "visibility": "provider_owned",
        "source_uri": f"{SCENARIO_URI_PREFIX}{PYTHON_SCENARIO}",
        "revision": "demo-payments@2.0.0-proposed",
        "file": None,
        "line_start": None,
        "line_end": None,
        "json_pointer": None,
        "excerpt": "Executable scenario available, not yet run",
    }


def endpoint_record(record_id: str, snapshot_id: str, path: str) -> Endpoint:
    return Endpoint.model_validate(
        {
            "id": record_id,
            "service_id": "demo-payments",
            "method": "GET",
            "path": path,
            "snapshot_id": snapshot_id,
            "operation_id": None,
            "request_schema": {},
            "response_schema": {},
            "evidence": [],
            "reconciliation": "not_compared",
            "confidence": "direct",
        }
    )


def reimported(change: APIChange, deps: dict[str, Dependency]) -> tuple[APIChange, list[Endpoint]]:
    """``change`` against a re-import of the identical v1 contract under a new snapshot id, plus the
    Endpoint records of both snapshots (the scan's catalog stays the original v1 one)."""
    snapshot = stable_id("test-contract", "v1-reimport")
    charge = endpoint_id(snapshot, "demo-payments", "GET", CHARGE_PATH)
    rekeyed = change.model_copy(update={"endpoint_id": charge, "old_snapshot_id": snapshot})
    endpoints = [
        endpoint_record(change.endpoint_id, change.old_snapshot_id, CHARGE_PATH),
        endpoint_record(deps["negative_refund_amount"].endpoint_id, change.old_snapshot_id, REFUND_PATH),
        endpoint_record(charge, snapshot, CHARGE_PATH),
        endpoint_record(endpoint_id(snapshot, "demo-payments", "GET", REFUND_PATH), snapshot, REFUND_PATH),
    ]
    return rekeyed, endpoints


def repository_snapshot(dependency: Dependency) -> RepositorySnapshot:
    return RepositorySnapshot(
        id=dependency.repository_snapshot_id,
        repository_connection_id=stable_id("test-connection"),
        commit_sha="0" * 40,
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
        content_hash="sha256:test",
    )


# The demo: cents-to-dollars on GET /v1/charges/{id} ----------------------------------------------------


@pytest.mark.parametrize(
    ("handle", "scenario", "location"),
    [
        ("python_display", PYTHON_SCENARIO, "storefront/pricing.py:18"),
        ("typescript_display", TYPESCRIPT_SCENARIO, "src/pricing.ts:20"),
    ],
)
def test_display_dependencies_yield_high_direct_conversion_risks(change, deps, handle, scenario, location):
    dependency = deps[handle]
    result = analyze_impact(change, list(deps.values()))
    risk = risk_with(result, dependency)

    assert risk.dependency_ids == [dependency.id]
    key = f"conversion:{scenario}"  # a scenario-bound conversion Risk is keyed by its scenario
    assert risk.id == risk_id(change.id, dependency.enrollment_id, dependency.repository_snapshot_id, key)
    assert risk_category(change, risk) == "conversion"
    assert (risk.severity, risk.confidence) == ("high", "direct")
    assert "$0.10" in risk.explanation and "$10.00" in risk.explanation
    assert location in risk.explanation and location in risk.suggested_action
    assert "100x too small" in risk.explanation
    for item in dependency.evidence:  # dependency evidence verbatim (same id, file, lines, excerpt)
        assert item in risk.evidence
    for item in change.evidence:
        assert item in risk.evidence
    pointers = scenario_evidence(risk)
    assert [pointer.source_uri for pointer in pointers] == [f"{SCENARIO_URI_PREFIX}{scenario}"]
    assert "not yet run" in (pointers[0].excerpt or "")


def test_display_risks_are_separate_per_repository_snapshot(change, deps):
    result = analyze_impact(change, list(deps.values()))
    python_risk = risk_with(result, deps["python_display"])
    typescript_risk = risk_with(result, deps["typescript_display"])
    assert python_risk.id != typescript_risk.id
    assert python_risk.repository_snapshot_id != typescript_risk.repository_snapshot_id


def test_fraud_threshold_is_a_separate_comparison_prediction(change, deps):
    result = analyze_impact(change, list(deps.values()))
    dependency = deps["python_fraud_threshold"]
    risk = risk_with(result, dependency)
    assert risk.id == risk_id(change.id, dependency.enrollment_id, dependency.repository_snapshot_id, "comparison")
    assert risk.id != risk_with(result, deps["python_display"]).id
    assert (risk.severity, risk.confidence) == ("high", "inferred")
    assert "storefront/fraud.py:10" in risk.explanation
    assert "$50,000.00" in risk.explanation and "Prediction" in risk.explanation
    assert "Missing evidence" in risk.explanation
    assert scenario_evidence(risk) == []  # no executable proof exists for a threshold comparison


def test_storage_sink_is_a_potential_risk_not_a_confirmed_corruption(change, deps):
    result = analyze_impact(change, list(deps.values()))
    dependency = deps["python_storage"]
    risk = risk_with(result, dependency)
    assert risk.id == risk_id(change.id, dependency.enrollment_id, dependency.repository_snapshot_id, "storage")
    assert (risk.severity, risk.confidence) == ("medium", "inferred")
    assert "not confirmed" in risk.explanation
    assert "mixed units" in risk.explanation
    assert "storefront/ledger.py:10-13" in risk.explanation
    assert scenario_evidence(risk) == []


def test_whole_response_pass_through_is_only_unresolved(change, deps):
    result = analyze_impact(change, list(deps.values()))
    export_id = deps["python_export_pass_through"].id
    assert export_id in result.unresolved_dependency_ids
    assert all(export_id not in risk.dependency_ids for risk in result.risks)
    assert any("enclosing object" in warning for warning in result.warnings)


def test_negative_controls_appear_nowhere(change, deps):
    result = analyze_impact(change, list(deps.values()))
    serialized = json.dumps(result.model_dump(mode="json"))
    for handle in NEGATIVE_CONTROLS:
        dependency = deps[handle]
        assert dependency.id not in everything(result)
        for item in dependency.evidence:
            assert item.id not in serialized
    assert "stripe" not in serialized.lower()


def test_match_dependencies_partitions_the_demo(change, deps):
    analyzable, unresolved = match_dependencies(change, list(deps.values()))
    expected = ("python_display", "typescript_display", "python_fraud_threshold", "python_storage")
    assert {dependency.id for dependency in analyzable} == {deps[handle].id for handle in expected}
    assert [dependency.id for dependency in unresolved] == [deps["python_export_pass_through"].id]
    assert [dependency.id for dependency in analyzable] == sorted(dependency.id for dependency in analyzable)


def test_demo_result_is_rules_mode_valid_and_serializable(change, deps):
    dependencies = list(deps.values())
    result = analyze_impact(change, dependencies)
    assert result.analysis_mode == "rules"
    assert result.schema_version == "shockwave.v1"
    assert result.change_id == change.id
    assert len(result.risks) == 4
    assert result.unresolved_dependency_ids == [deps["python_export_pass_through"].id]
    assert validate_risk_evidence(result, change, dependencies) == []
    assert any(warning.startswith("predictions only") for warning in result.warnings)
    assert AnalysisResult.model_validate(json.loads(json.dumps(result.model_dump(mode="json")))) == result


def test_output_is_deterministic_and_inputs_are_not_modified(change, deps):
    dependencies = list(deps.values())
    before = (change.model_dump(mode="json"), [dependency.model_dump(mode="json") for dependency in dependencies])
    first = analyze_impact(change, dependencies).model_dump(mode="json")
    shuffled = dependencies[:]
    random.Random(7).shuffle(shuffled)
    second = analyze_impact(change, shuffled).model_dump(mode="json")
    assert first == second
    after = (change.model_dump(mode="json"), [dependency.model_dump(mode="json") for dependency in dependencies])
    assert before == after


def test_risk_evidence_is_a_copy_not_an_alias(change, deps):
    result = analyze_impact(change, list(deps.values()))
    risk = risk_with(result, deps["python_display"])
    risk.evidence[0].excerpt = "mutated by a caller"
    assert deps["python_display"].evidence[0].excerpt != "mutated by a caller"


def test_explanations_are_bounded_and_labeled_as_predictions(change, deps):
    result = analyze_impact(change, list(deps.values()))
    for risk in result.risks:
        assert len(risk.explanation) <= MAX_EXPLANATION_CHARS
        assert len(risk.suggested_action) <= MAX_ACTION_CHARS
        assert "not executed" in risk.explanation
        lowered = risk.explanation.lower()
        assert "verified" not in lowered and "tests passed" not in lowered


# Evidence validation ----------------------------------------------------------------------------------


def test_validate_risk_evidence_flags_invented_or_foreign_references(change, deps):
    dependencies = list(deps.values())
    result = analyze_impact(change, dependencies)
    display = deps["python_display"]
    index = result.risks.index(risk_with(result, display))

    def tampered(mutate) -> list[str]:
        copy = result.model_copy(deep=True)
        mutate(copy.risks[index])
        return validate_risk_evidence(copy, change, dependencies)

    invented = Evidence(
        id=stable_id("invented"),
        kind="source",
        visibility="consumer_private",
        source_uri="repository-snapshot://x/storefront/pricing.py",
        revision="0",
        file="storefront/pricing.py",
        line_start=99,
        line_end=99,
    )
    assert any("not a dependency location verbatim" in p for p in tampered(lambda risk: risk.evidence.append(invented)))
    moved = display.evidence[0].model_copy(update={"line_start": 3, "line_end": 3})
    assert tampered(lambda risk: risk.evidence.__setitem__(0, moved))
    for foreign in (stable_id("unknown-dependency"), deps["negative_stripe_service"].id):
        problems = tampered(lambda risk, foreign=foreign: risk.dependency_ids.append(foreign))
        assert any("not a matched input dependency" in problem for problem in problems)
    docs = Evidence(
        id=stable_id("docs"), kind="docs", visibility="public", source_uri="https://docs.example/x", revision="1"
    )
    assert any("neither dependency, change nor" in p for p in tampered(lambda risk: risk.evidence.append(docs)))
    assert any("omits evidence" in p for p in tampered(lambda risk: risk.evidence.pop(0)))
    storage = deps["python_storage"].id
    assert any("is also in risk" in p for p in tampered(lambda risk: risk.dependency_ids.append(storage)))
    other_snapshot = deps["typescript_display"].id
    assert any("another organization" in p for p in tampered(lambda risk: risk.dependency_ids.append(other_snapshot)))
    wrong_change = result.model_copy(update={"change_id": stable_id("other-change")})
    assert validate_risk_evidence(wrong_change, change, dependencies)


OWN_CATEGORY_ONLY = "a scenario proves only risks of its own category"


def test_scenario_pointers_are_only_valid_on_risks_of_their_scenarios_category(change, deps):
    dependencies = list(deps.values())
    display = deps["python_display"]
    result = analyze_impact(change, dependencies)
    pointer = scenario_evidence(risk_with(result, display))[0]

    comparison = build_risk(
        change,
        [display],
        category="comparison",
        severity="high",
        confidence="inferred",
        explanation="mislabelled on purpose",
        suggested_action="none",
    )
    assert scenario_evidence(comparison) == []  # build_risk adds only pointers of the risk's own category
    comparison.evidence.append(pointer)
    forged = AnalysisResult(
        schema_version="shockwave.v1",
        change_id=change.id, risks=[comparison], unresolved_dependency_ids=[], warnings=[], analysis_mode="rules"
    )
    assert any(OWN_CATEGORY_ONLY in problem for problem in validate_risk_evidence(forged, change, dependencies))

    fraud_risk = risk_with(result, deps["python_fraud_threshold"]).model_copy(deep=True)
    fraud_risk.evidence.append(pointer)
    stolen = AnalysisResult(
        schema_version="shockwave.v1",
        change_id=change.id, risks=[fraud_risk], unresolved_dependency_ids=[], warnings=[], analysis_mode="rules"
    )
    assert any(OWN_CATEGORY_ONLY in problem for problem in validate_risk_evidence(stolen, change, dependencies))

    storage_risk = risk_with(result, deps["python_storage"]).model_copy(deep=True)
    storage_risk.evidence.append(pointer)
    stored = AnalysisResult(
        schema_version="shockwave.v1",
        change_id=change.id, risks=[storage_risk], unresolved_dependency_ids=[], warnings=[], analysis_mode="rules"
    )
    problems = validate_risk_evidence(stored, change, dependencies)
    assert any("only conversion and comparison risks have executable proof" in problem for problem in problems)

    typescript_risk = risk_with(result, deps["typescript_display"]).model_copy(deep=True)
    typescript_risk.evidence.append(pointer)  # the Python display risk's pointer on the TypeScript one
    copied = AnalysisResult(
        schema_version="shockwave.v1",
        change_id=change.id, risks=[typescript_risk], unresolved_dependency_ids=[], warnings=[], analysis_mode="rules"
    )
    assert any("not applicable" in problem for problem in validate_risk_evidence(copied, change, dependencies))


def test_inferred_display_dependency_gets_no_scenario_pointer(change, deps):
    inferred = deps["python_display"].model_copy(update={"confidence": "inferred"})
    result = analyze_impact(change, [inferred])
    risk = risk_with(result, inferred)
    assert (risk.severity, risk.confidence) == ("high", "inferred")
    assert scenario_evidence(risk) == []
    assert validate_risk_evidence(result, change, [inferred]) == []


def test_reserved_scenario_scheme_in_change_evidence_never_reaches_a_risk(deps):
    document = enriched_change_document()
    document["evidence"].append(smuggled_pointer())
    smuggling = APIChange.model_validate(document)
    dependencies = list(deps.values())
    result = analyze_impact(smuggling, dependencies)

    for handle in ("python_fraud_threshold", "python_storage"):
        assert scenario_evidence(risk_with(result, deps[handle])) == []
    for handle, scenario in (("python_display", PYTHON_SCENARIO), ("typescript_display", TYPESCRIPT_SCENARIO)):
        risk = risk_with(result, deps[handle])
        assert [item.source_uri for item in scenario_evidence(risk)] == [f"{SCENARIO_URI_PREFIX}{scenario}"]
        assert bound_scenario_ids(risk.evidence, risk.dependency_ids) == [scenario]
    assert smuggled_pointer()["id"] not in json.dumps(result.model_dump(mode="json"))
    assert any("1 change or dependency evidence item uses the reserved" in w for w in result.warnings)
    assert validate_risk_evidence(result, smuggling, dependencies) == []


def test_reserved_scenario_scheme_is_flagged_wherever_it_is_copied(deps):
    document = enriched_change_document()
    document["evidence"].append(smuggled_pointer())
    smuggling = APIChange.model_validate(document)
    dependencies = list(deps.values())
    result = analyze_impact(smuggling, dependencies)
    pointer = Evidence.model_validate(smuggled_pointer())

    def forged(handle: str) -> list[str]:
        copy = result.model_copy(deep=True)
        copy.risks[result.risks.index(risk_with(result, deps[handle]))].evidence.append(pointer)
        return validate_risk_evidence(copy, smuggling, dependencies)

    # Verbatim change evidence, yet no proof: storage never has a scenario, and the smuggled
    # pointer names a conversion scenario, which cannot prove a comparison risk.
    assert any("only conversion and comparison risks" in problem for problem in forged("python_storage"))
    assert any(OWN_CATEGORY_ONLY in problem for problem in forged("python_fraud_threshold"))
    assert any("not applicable" in problem for problem in forged("typescript_display"))
    assert any("not applicable" in problem for problem in forged("python_display"))  # not the catalog's own


def test_reserved_scenario_scheme_in_dependency_evidence_is_not_copied(change, deps):
    storage = deps["python_storage"].model_copy(deep=True)
    storage.evidence.append(Evidence.model_validate(smuggled_pointer()))
    result = analyze_impact(change, [storage])
    assert scenario_evidence(risk_with(result, storage)) == []
    assert any("reserved shockwave-scenario://" in warning for warning in result.warnings)
    assert validate_risk_evidence(result, change, [storage]) == []


def test_declared_change_backed_by_provider_source_evidence_validates(deps):
    document = enriched_change_document()
    provider_source = {
        "id": stable_id("test-provider-source"),
        "kind": "source",
        "visibility": "provider_owned",
        "source_uri": "examples/provider/demo_payments/service/demo_payments/store.py",
        "revision": "demo-payments@2.0.0-proposed",
        "file": "demo_payments/store.py",
        "line_start": 29,
        "line_end": 29,
        "json_pointer": None,
        "excerpt": "major = amount_minor / 100",
    }
    openapi = [item for item in document["evidence"] if item["kind"] == "openapi"]
    dependencies = list(deps.values())
    for evidence in ([*document["evidence"], provider_source], [provider_source, *openapi]):
        backed = APIChange.model_validate({**document, "evidence": evidence})
        result = analyze_impact(backed, dependencies)
        assert len(result.risks) == 4
        for risk in result.risks:  # the change's evidence is shared, verbatim, with each consumer's Risk
            assert Evidence.model_validate(provider_source) in risk.evidence
        assert validate_risk_evidence(result, backed, dependencies) == []
        moved = result.model_copy(deep=True)
        moved.risks[0].evidence.append(Evidence.model_validate({**provider_source, "line_start": 30, "line_end": 30}))
        assert any("not a dependency location verbatim" in p for p in validate_risk_evidence(moved, backed, dependencies))


# Units rules beyond the demo ------------------------------------------------------------------------


def units_change(old: str = USD_CENTS, new: str = USD_DOLLARS) -> APIChange:
    return synthetic_change("units_changed", old=old, new=new)


def test_units_context_reads_direction_currency_and_factor(change):
    assert units_context(change) == UnitsContext(old_unit="minor", new_unit="major", currency="usd", factor=100)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("EUR amount in cents", "EUR amount in whole units", "EUR"),
        ("amount in cents", "amount in dollars", "single currency"),
        ("USD amount in cents", "USD amount in cents, rounded", "direction"),
        ("USD amount", "USD amount, new rounding", "direction"),
    ],
)
def test_unknown_currency_or_direction_leaves_dependencies_unresolved(old, new, expected):
    change = units_change(old, new)
    dependencies = [
        synthetic_dependency("divide", operation="divide", operand=100),
        synthetic_dependency("compare", operation="compare", operand=500, snapshot="snapshot-b"),
    ]
    result = analyze_with_rules(change, dependencies)
    assert result.risks == []
    assert result.unresolved_dependency_ids == sorted(dependency.id for dependency in dependencies)
    assert any(expected in warning and "unresolved" in warning for warning in result.warnings)


def test_other_arithmetic_is_an_uncertain_conversion():
    change = units_change()
    by_thousand = synthetic_dependency("thousand", operation="divide", operand=1000)
    by_name = synthetic_dependency("named", operation="divide", operand="SCALE", snapshot="snapshot-b")
    times_hundred = synthetic_dependency("times", operation="multiply", operand=100, snapshot="snapshot-c")
    result = analyze_with_rules(change, [by_thousand, by_name, times_hundred])
    for dependency in (by_thousand, by_name, times_hundred):
        risk = risk_with(result, dependency)
        assert (risk.severity, risk.confidence) == ("medium", "inferred")
        assert "uncertain" in risk.explanation
    assert "SCALE" in risk_with(result, by_name).explanation


def test_major_to_minor_change_flags_multiplication_as_too_large():
    change = units_change(old=USD_DOLLARS, new=USD_CENTS)
    multiply = synthetic_dependency("to_cents", operation="multiply", operand=100)
    compare = synthetic_dependency("limit", operation="compare", operand=500, snapshot="snapshot-b")
    result = analyze_with_rules(change, [multiply, compare])
    conversion = risk_with(result, multiply)
    assert (conversion.severity, conversion.confidence) == ("high", "direct")
    assert "100x too large" in conversion.explanation and "$1,000.00" in conversion.explanation
    threshold = risk_with(result, compare)
    assert "$5.00" in threshold.explanation and "100x lower" in threshold.explanation


def test_comparison_with_symbolic_threshold_still_predicts_a_shift():
    result = analyze_with_rules(units_change(), [synthetic_dependency("limit", operation="compare", operand="LIMIT")])
    risk = result.risks[0]
    assert (risk.severity, risk.confidence) == ("high", "inferred")
    assert "shift by 100x" in risk.explanation


def test_units_change_leaves_opaque_usages_and_unresolved_dependencies_unresolved():
    change = units_change()
    dependencies = [
        synthetic_dependency("read", operation="read"),
        synthetic_dependency("forward", operation="pass_through", snapshot="snapshot-b"),
        synthetic_dependency("unknown", operation="unknown", snapshot="snapshot-c"),
        synthetic_dependency(
            "unclear", operation="divide", operand=100, confidence="unresolved", snapshot="snapshot-d"
        ),
    ]
    result = analyze_with_rules(change, dependencies)
    assert result.risks == []
    assert result.unresolved_dependency_ids == sorted(dependency.id for dependency in dependencies)
    assert all(classify_dependency(change, dependency) is None for dependency in dependencies)


def test_dependencies_in_one_snapshot_and_category_form_one_risk():
    change = units_change()
    exact = synthetic_dependency("exact", operation="divide", operand=100, line=3)
    odd = synthetic_dependency("odd", operation="divide", operand=1000, confidence="inferred", line=9)
    result = analyze_with_rules(change, [odd, exact])
    assert len(result.risks) == 1
    risk = result.risks[0]
    assert risk.dependency_ids == sorted([exact.id, odd.id])
    assert (risk.severity, risk.confidence) == ("high", "inferred")
    assert "app/exact.py:3" in risk.explanation and "app/odd.py:9 (divide by 1000)" in risk.explanation
    assert validate_risk_evidence(result, change, [odd, exact]) == []


def test_python_and_typescript_display_code_in_one_repository_get_one_plannable_risk_each(change, deps):
    python = deps["python_display"]
    typescript = in_snapshot_of(deps["typescript_display"], python)
    result = analyze_impact(change, [python, typescript])
    assert len(result.risks) == 2
    for dependency, scenario in ((python, PYTHON_SCENARIO), (typescript, TYPESCRIPT_SCENARIO)):
        risk = risk_with(result, dependency)
        assert risk.dependency_ids == [dependency.id]
        key = f"conversion:{scenario}"
        assert risk.id == risk_id(change.id, python.enrollment_id, python.repository_snapshot_id, key)
        assert risk_category(change, risk) == "conversion"
        assert bound_scenario_ids(risk.evidence, risk.dependency_ids) == [scenario]
        plan = build_verification_plan(risk, repository_snapshot(python), config=PlanConfig())
        assert (plan.risk_id, plan.scenario_id) == (risk.id, scenario)
    assert validate_risk_evidence(result, change, [python, typescript]) == []


def test_a_scenario_bound_risk_holds_only_the_dependencies_its_scenario_runs(change, deps):
    python = deps["python_display"]
    odd = odd_divide(python)
    result = analyze_impact(change, [odd, python])
    bound, unbound = risk_with(result, python), risk_with(result, odd)
    assert (bound.dependency_ids, unbound.dependency_ids) == ([python.id], [odd.id])
    owner = (change.id, python.enrollment_id, python.repository_snapshot_id)
    assert bound.id == risk_id(*owner, f"conversion:{PYTHON_SCENARIO}")
    assert unbound.id == risk_id(*owner, "conversion")
    assert [risk_category(change, risk) for risk in (bound, unbound)] == ["conversion", "conversion"]
    assert "reports.py" not in bound.explanation and "storefront/pricing.py:18" not in unbound.explanation
    assert scenario_evidence(unbound) == [] and (unbound.severity, unbound.confidence) == ("medium", "inferred")
    assert validate_risk_evidence(result, change, [odd, python]) == []

    merged = result.model_copy(deep=True)  # the old category-level grouping: one verdict for both
    merged.risks.remove(unbound)
    target = merged.risks[merged.risks.index(bound)]
    target.dependency_ids.append(odd.id)
    target.evidence.extend(odd.evidence)
    problems = validate_risk_evidence(merged, change, [odd, python])
    assert any(f"{odd.id} is not bound to scenario {PYTHON_SCENARIO}" in problem for problem in problems)
    with pytest.raises(ValueError, match="exactly one executable scenario"):
        build_risk(
            change,
            [python, odd],
            category="conversion",
            severity="high",
            confidence="inferred",
            explanation="mixed on purpose",
            suggested_action="none",
        )


def test_risk_category_recovers_every_category_and_rejects_foreign_ids(change, deps):
    result = analyze_impact(change, list(deps.values()))
    categories = sorted(risk_category(change, risk) or "none" for risk in result.risks)
    assert categories == ["comparison", "conversion", "conversion", "storage"]
    foreign = result.risks[0].model_copy(update={"id": stable_id("not-a-risk-id")})
    assert risk_category(change, foreign) is None


def test_enrollments_and_organizations_are_never_mixed():
    change = units_change()
    first = synthetic_dependency("first", operation="divide", operand=100, enrollment="enrollment-a", org="org-a")
    second = synthetic_dependency("second", operation="divide", operand=100, enrollment="enrollment-b", org="org-b")
    result = analyze_with_rules(change, [first, second])
    assert len(result.risks) == 2
    assert {risk.enrollment_id for risk in result.risks} == {first.enrollment_id, second.enrollment_id}
    assert all(len(risk.dependency_ids) == 1 for risk in result.risks)


def test_contradictory_inputs_are_rejected():
    change = units_change()
    dependency = synthetic_dependency("dup", operation="divide", operand=100)
    analyzable, _ = match_dependencies(change, [dependency, dependency.model_copy(deep=True)])
    assert [item.id for item in analyzable] == [dependency.id]
    with pytest.raises(ValueError, match="conflicting dependencies"):
        match_dependencies(change, [dependency, dependency.model_copy(update={"confidence": "inferred"})])
    other_org = synthetic_dependency("other", operation="read", org="org-b")
    with pytest.raises(ValueError, match="different consumer organizations"):
        match_dependencies(change, [dependency, other_org])


def test_non_api_fields_other_endpoints_and_unrelated_fields_do_not_match():
    change = units_change()
    dependencies = [
        synthetic_dependency("local", operation="divide", operand=100, field_path="amount"),
        synthetic_dependency("elsewhere", operation="divide", operand=100, endpoint_id=stable_id("other-endpoint")),
        synthetic_dependency("currency", operation="read", field_path="response.currency"),
    ]
    result = analyze_with_rules(change, dependencies)
    assert result.risks == [] and result.unresolved_dependency_ids == []
    assert NO_MATCH_WARNING in result.warnings


# Endpoint identity across contract snapshots ----------------------------------------------------------


def test_dependencies_keyed_to_another_contract_snapshot_match_through_endpoints(change, deps):
    rekeyed, endpoints = reimported(change, deps)
    dependencies = list(deps.values())
    baseline = analyze_impact(change, dependencies)

    blind = analyze_impact(rekeyed, dependencies)  # record ids alone cannot tell it is the same endpoint
    assert blind.risks == [] and blind.unresolved_dependency_ids == []
    same_service = {dependency.id for dependency in dependencies if dependency.service_id == "demo-payments"}
    hints = [warning for warning in blind.warnings if "pass endpoints" in warning]
    assert len(hints) == 1 and hints[0].startswith(f"{len(same_service)} supplied dependencies of this service name")
    assert NO_MATCH_WARNING in blind.warnings

    result = analyze_impact(rekeyed, dependencies, endpoints=endpoints)
    assert [risk.dependency_ids for risk in result.risks] == [risk.dependency_ids for risk in baseline.risks]
    assert result.unresolved_dependency_ids == baseline.unresolved_dependency_ids
    assert sorted(item.source_uri for risk in result.risks for item in scenario_evidence(risk)) == [
        f"{SCENARIO_URI_PREFIX}{PYTHON_SCENARIO}",
        f"{SCENARIO_URI_PREFIX}{TYPESCRIPT_SCENARIO}",
    ]
    assert deps["negative_refund_amount"].id not in everything(result)  # identified: another endpoint
    assert not any("pass endpoints" in warning for warning in result.warnings)
    assert validate_risk_evidence(result, rekeyed, dependencies, endpoints=endpoints) == []
    assert validate_risk_evidence(result, rekeyed, dependencies)  # without them nothing matches
    assert match_dependencies(rekeyed, dependencies, endpoints=endpoints) == match_dependencies(change, dependencies)


def test_endpoint_hint_counts_only_records_the_call_cannot_identify(change, deps):
    rekeyed, endpoints = reimported(change, deps)
    refund = [deps["negative_refund_amount"]]
    identified = analyze_impact(rekeyed, refund, endpoints=endpoints)
    assert identified.warnings[-1] == NO_MATCH_WARNING
    unknown = analyze_impact(change, refund)
    assert any(warning.startswith("1 supplied dependency of this service names") for warning in unknown.warnings)
    stripe = analyze_impact(change, [deps["negative_stripe_service"]])  # another service: never a hint
    assert stripe.warnings[-1] == NO_MATCH_WARNING


# Structural and behavioral kinds ----------------------------------------------------------------------


def test_field_removed_affects_every_usage_directly():
    change = synthetic_change("field_removed", old="number", new="removed", breaking=True)
    read = synthetic_dependency("read", operation="read", snapshot="s1")
    divide = synthetic_dependency("divide", operation="divide", operand=100, snapshot="s2")
    compare = synthetic_dependency("compare", operation="compare", operand=5, confidence="inferred", snapshot="s3")
    store = synthetic_dependency("store", operation="store", destination="db.amount", snapshot="s4")
    nested = synthetic_dependency("nested", operation="unknown", field_path="response.amount.value", snapshot="s5")
    enclosing = synthetic_dependency("enclosing", operation="pass_through", field_path="response", snapshot="s6")
    dependencies = [read, divide, compare, store, nested, enclosing]
    result = analyze_with_rules(change, dependencies)

    expected = {
        read.id: ("read", "high", "direct"),
        divide.id: ("conversion", "high", "direct"),
        compare.id: ("comparison", "high", "inferred"),
        store.id: ("storage", "high", "direct"),
        nested.id: ("read", "high", "direct"),
    }
    for dependency in (read, divide, compare, store, nested):
        risk = risk_with(result, dependency)
        category, severity, confidence = expected[dependency.id]
        assert risk.id == risk_id(change.id, dependency.enrollment_id, dependency.repository_snapshot_id, category)
        assert (risk.severity, risk.confidence) == (severity, confidence)
        assert "removes" in risk.explanation
    assert result.unresolved_dependency_ids == [enclosing.id]
    assert validate_risk_evidence(result, change, dependencies) == []


@pytest.mark.parametrize(
    ("breaking", "operation", "expected"),
    [
        (True, "divide", ("high", "inferred")),
        (True, "compare", ("high", "inferred")),
        (True, "read", ("medium", "inferred")),
        (True, "store", ("medium", "inferred")),
        (False, "divide", ("low", "inferred")),
        (False, "read", ("low", "inferred")),
    ],
)
def test_type_changed_severity_follows_breakage_and_usage(breaking, operation, expected):
    change = synthetic_change("type_changed", old="number", new="string", breaking=breaking)
    dependency = synthetic_dependency("typed", operation=operation, operand=100)
    outcome = classify_dependency(change, dependency)
    assert isinstance(outcome, RuleOutcome)
    assert (outcome.severity, outcome.confidence) == expected
    result = analyze_with_rules(change, [dependency])
    assert (result.risks[0].severity, result.risks[0].confidence) == expected


@pytest.mark.parametrize(
    ("breaking", "operation", "expected"),
    [
        (True, "compare", ("comparison", "medium")),
        (True, "read", ("read", "low")),
        (False, "compare", ("comparison", "low")),
    ],
)
def test_enum_changed_severity_follows_breakage_and_usage(breaking, operation, expected):
    change = synthetic_change(
        "enum_changed", field_path="response.status", old="succeeded|failed", new="paid|failed", breaking=breaking
    )
    dependency = synthetic_dependency("status", operation=operation, operand="succeeded", field_path="response.status")
    outcome = classify_dependency(change, dependency)
    assert isinstance(outcome, RuleOutcome)
    assert (outcome.category, outcome.severity, outcome.confidence) == (*expected, "inferred")


def test_endpoint_removal_affects_every_dependency_on_the_endpoint():
    # "*" is a removal only when structurally breaking (detect_changes sets that when the endpoint is gone).
    change = synthetic_change(
        "behavior_changed", field_path="*", old="endpoint available", new="endpoint removed", breaking=True
    )
    amount = synthetic_dependency("amount", operation="divide", operand=100, snapshot="s1")
    status = synthetic_dependency(
        "status", operation="read", field_path="response.status", confidence="inferred", snapshot="s2"
    )
    whole = synthetic_dependency("whole", operation="pass_through", field_path="response", snapshot="s3")
    unclear = synthetic_dependency("unclear", operation="read", confidence="unresolved", snapshot="s4")
    other_endpoint = stable_id("other-endpoint")
    elsewhere = synthetic_dependency("elsewhere", operation="read", endpoint_id=other_endpoint, snapshot="s5")
    dependencies = [amount, status, whole, unclear, elsewhere]
    result = analyze_with_rules(change, dependencies)

    assert (risk_with(result, amount).severity, risk_with(result, amount).confidence) == ("high", "direct")
    assert (risk_with(result, status).severity, risk_with(result, status).confidence) == ("high", "inferred")
    assert (risk_with(result, whole).severity, risk_with(result, whole).confidence) == ("high", "direct")
    conversion_id = risk_id(change.id, amount.enrollment_id, amount.repository_snapshot_id, "conversion")
    assert risk_with(result, amount).id == conversion_id
    assert risk_with(result, whole).id == risk_id(change.id, whole.enrollment_id, whole.repository_snapshot_id, "read")
    assert result.unresolved_dependency_ids == [unclear.id]
    assert elsewhere.id not in everything(result)


def test_field_behavior_change_is_medium_and_opaque_usages_stay_unresolved():
    change = synthetic_change("behavior_changed", old="rounded half-up", new="rounded half-even")
    read = synthetic_dependency("read", operation="read", snapshot="s1")
    compare = synthetic_dependency("compare", operation="compare", operand=5, snapshot="s2")
    store = synthetic_dependency("store", operation="store", snapshot="s3")
    forward = synthetic_dependency("forward", operation="pass_through", snapshot="s4")
    result = analyze_with_rules(change, [read, compare, store, forward])
    for dependency in (read, compare, store):
        risk = risk_with(result, dependency)
        assert (risk.severity, risk.confidence) == ("medium", "inferred")
    assert result.unresolved_dependency_ids == [forward.id]


# Analyzer selection ------------------------------------------------------------------------------------


def test_default_analyzer_is_rules_even_with_a_model_env_var(monkeypatch, change, deps):
    monkeypatch.setenv("SHOCKWAVE_ANALYZER_MODEL", "claude-opus-5-5")
    assert default_analyzer().mode == "rules"
    assert analyze_impact(change, list(deps.values())).analysis_mode == "rules"


def test_configure_default_analyzer_validates_and_replaces():
    original = default_analyzer()
    replacement = ImpactAnalyzer("rules")
    try:
        configure_default_analyzer(replacement)
        assert default_analyzer() is replacement
        with pytest.raises(TypeError):
            configure_default_analyzer("model")  # type: ignore[arg-type]
    finally:
        configure_default_analyzer(original)


def test_impact_analyzer_rejects_inconsistent_configuration():
    with pytest.raises(ValueError, match="requires a ModelImpactAnalyzer"):
        ImpactAnalyzer("model")
    with pytest.raises(ValueError, match="mode must be"):
        ImpactAnalyzer("fixture")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only used in model mode"):
        ImpactAnalyzer("rules", model_analyzer=object())  # type: ignore[arg-type]


# Canonical contracts with uuid.UUID ids -------------------------------------------------------------------

STRICT_DEMO_INPUTS = """
import hashlib, json, uuid
from pathlib import Path
import discovery.contracts as contracts
from discovery.contracts import APIChange, Dependency
from discovery.records import operation_pointer
from discovery.ids import evidence_id

assert contracts.CONTRACTS_SOURCE == "shared.schemas", contracts.CONTRACTS_SOURCE
spec = "examples/provider/demo_payments/openapi/demo-payments.v1.yaml"
digest = hashlib.sha256(Path(spec).read_bytes()).hexdigest()
pointer = operation_pointer("/v1/charges/{id}", "get")
document = json.loads(Path("examples/provider/demo_payments/changes/charge_amount_cents_to_dollars.json").read_text())
document["evidence"].append({
    "id": evidence_id("openapi", digest, pointer), "kind": "openapi", "visibility": "provider_owned",
    "source_uri": spec, "revision": "sha256:" + digest, "file": None, "line_start": None, "line_end": None,
    "json_pointer": pointer, "excerpt": None,
})
change = APIChange.model_validate(document)
documents = json.loads(Path("tests/analyzer/fixtures/demo_dependencies.json").read_text())["dependencies"]
deps = {handle: Dependency.model_validate(item) for handle, item in documents.items()}
assert isinstance(change.id, uuid.UUID) and isinstance(deps["python_display"].id, uuid.UUID)
"""


def run_with_strict_contracts(script: str) -> subprocess.CompletedProcess:
    """Run ``script`` with tests/analyzer/fixtures/strict_contracts standing in for shared.schemas."""
    env = {key: value for key, value in os.environ.items() if key != "SHOCKWAVE_CONTRACTS"}
    env.update(PYTHONPATH=os.pathsep.join([str(REPO_ROOT), str(STRICT_CONTRACTS)]), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-c", STRICT_DEMO_INPUTS + script],
        check=False,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_analysis_and_validation_work_with_uuid_typed_contract_ids():
    script = (
        "from analyzer.impact import analyze_impact, risk_category, validate_risk_evidence\n"
        "result = analyze_impact(change, list(deps.values()))\n"
        "print('risks', len(result.risks), 'unresolved', len(result.unresolved_dependency_ids))\n"
        "print('problems', validate_risk_evidence(result, change, list(deps.values())))\n"
        "print('categories', sorted(risk_category(change, risk) for risk in result.risks))\n"
    )
    completed = run_with_strict_contracts(script)
    assert completed.returncode == 0, completed.stderr
    assert "risks 4 unresolved 1" in completed.stdout
    assert "problems []" in completed.stdout
    assert "categories ['comparison', 'conversion', 'conversion', 'storage']" in completed.stdout
