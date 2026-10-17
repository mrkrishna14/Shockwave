"""Stripe scenarios: catalog, impact pointers, plans, case interpretation and REAL SDK runs.

The change is the clearly labeled SIMULATED Stripe change (USD PaymentIntent ``amount`` from
minor to major units; not announced by Stripe). The APIChange and Dependency inputs are
built here, contract-shaped, standing in for the scan fixture another agent writes (they are
fixtures, not a completed scan). The real runs copy ``demo/templates/*`` (team-created demo
consumers, not Stripe customers) into temporary directories and execute the official
stripe-node / stripe-python SDKs against the harness's in-process loopback fixture server on
the LOCAL RUNNER DOUBLE (trusted local fixtures; plain subprocess; not a sandbox; not Sai's
runner). No request leaves the machine: the fixture server is on 127.0.0.1 and every guard
probe below targets loopback only.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import runpy
import shutil
import sys
import types
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from analyzer.impact import (
    ImpactAnalyzer,
    analyze_impact,
    build_risk,
    risk_category,
    validate_risk_evidence,
)
from analyzer.model_adapter import ModelImpactAnalyzer, ModelReply
from discovery.contracts import (
    AnalysisResult,
    APIChange,
    Dependency,
    Evidence,
    ExecutionResult,
    RepositorySnapshot,
    Risk,
    VerificationPlan,
)
from discovery.ids import change_id, endpoint_id, evidence_id, risk_id, stable_id
from discovery.records import operation_pointer
from verification.scenarios import catalog, controls, staging
from verification.scenarios.catalog import (
    STRIPE_CHECKOUT_SCENARIO,
    STRIPE_FIXTURE_BASELINE,
    STRIPE_FIXTURE_PROPOSED,
    STRIPE_FRAUD_SCENARIO,
    STRIPE_REPORTING_CONTROL,
    bound_scenario_ids,
    canonical_json,
    find_applicable_scenario,
    scenario_pointer_evidence,
)
from verification.scenarios.controls import interpret_control, run_control_locally
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import (
    PINNED_PATCH_CAVEAT,
    PhaseRun,
    classify_phase,
    interpret_patched_run,
    interpret_verification,
)
from verification.scenarios.outcomes import (
    CASE_OUTCOME_TABLE,
    CASE_PHASE_EVIDENCE,
    OutcomeRow,
    render_case_outcome_table_markdown,
)
from verification.scenarios.plans import (
    NoExecutableScenario,
    PlanConfig,
    build_verification_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "demo" / "templates"
NODE = Path(sys.executable).parent / "node"
API_VERSION = "2026-09-30.endive"
OFFLINE_KEY = "sk_test_shockwave_offline"
PI_PATH = "/v1/payment_intents/{intent}"

requires_node = pytest.mark.skipif(not NODE.exists(), reason=f"Node.js >= 22.18 expected at {NODE}")
requires_checkout_sdk = pytest.mark.skipif(
    not (TEMPLATES / "checkout" / "node_modules" / "stripe" / "package.json").exists(),
    reason="demo/templates/checkout/node_modules is missing (run npm ci in the template first)",
)
requires_stripe_python = pytest.mark.skipif(
    importlib.util.find_spec("stripe") is None, reason="the Python consumers import the stripe SDK"
)

# Identifiers of the contract-shaped inputs (fixtures for this test, not scanner output).
API_PROJECT = stable_id("test-stripe", "api-project")
BASELINE_SNAPSHOT = stable_id("test-stripe", "contract-snapshot", "baseline")
PROPOSED_SNAPSHOT = stable_id("test-stripe", "contract-snapshot", "proposed-simulated")
ENDPOINT = endpoint_id(BASELINE_SNAPSHOT, "stripe", "GET", PI_PATH)
CONSUMERS = {
    name: {
        "org": stable_id("test-stripe", "consumer-org", name),
        "enrollment": stable_id("test-stripe", "enrollment", name),
        "snapshot": stable_id("test-stripe", "repository-snapshot", name),
        "connection": stable_id("test-stripe", "repository-connection", name),
    }
    for name in ("checkout", "fraud", "reporting")
}
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OLD_SEMANTICS = "USD amount in minor currency units (cents): 1000 represents $10.00"
NEW_SEMANTICS = "USD amount in major currency units (dollars): 10 represents $10.00"


# Contract-shaped inputs --------------------------------------------------------------------------


def stripe_change(*, old: str = OLD_SEMANTICS, new: str = NEW_SEMANTICS, openapi: bool = True) -> APIChange:
    pointer = operation_pointer(PI_PATH, "get")
    evidence = [
        Evidence(
            id=evidence_id("declared_change", "sim-stripe-pi-amount-usd-major-units"),
            kind="declared_change",
            visibility="provider_owned",
            source_uri="demo/scenarios/stripe-payment-intent-amount-usd-major-units.manifest.json",
            revision="sim-stripe-pi-amount-usd-major-units",
            excerpt="SIMULATED change, not announced by Stripe: USD PaymentIntent amount in major units.",
        )
    ]
    if openapi:
        evidence.append(
            Evidence(
                id=evidence_id("openapi", "stripe-pin", pointer),
                kind="openapi",
                visibility="public",
                source_uri="stripe/openapi:latest/openapi.spec3.json",
                revision="969d8c308ea65b6dbdef4b6b4a71904027a010b8",
                json_pointer=pointer,
            )
        )
    return APIChange(
        id=change_id(BASELINE_SNAPSHOT, PROPOSED_SNAPSHOT, "stripe", "GET", PI_PATH, "response.amount", "units_changed"),
        api_project_id=API_PROJECT,
        service_id="stripe",
        endpoint_id=ENDPOINT,
        old_snapshot_id=BASELINE_SNAPSHOT,
        new_snapshot_id=PROPOSED_SNAPSHOT,
        field_path="response.amount",
        kind="units_changed",
        old_semantics=old,
        new_semantics=new,
        structural_breaking=False,
        evidence=evidence,
    )


def dependency(
    name: str,
    consumer: str,
    *,
    language: str,
    field_path: str,
    operation: str,
    operand: object = None,
    destination: str | None = None,
    file: str,
    line: int,
    excerpt: str,
    call: str,
    confidence: str = "direct",
) -> Dependency:
    ids = CONSUMERS[consumer]
    return Dependency.model_validate(
        {
            "id": stable_id("test-stripe", "dependency", name),
            "consumer_org_id": ids["org"],
            "enrollment_id": ids["enrollment"],
            "repository_snapshot_id": ids["snapshot"],
            "service_id": "stripe",
            "endpoint_id": ENDPOINT,
            "language": language,
            "field_path": field_path,
            "origin": {"kind": "sdk_response", "library": "stripe", "call": call},
            "usage": {"operation": operation, "operand": operand, "destination": destination},
            "confidence": confidence,
            "evidence": [
                {
                    "id": stable_id("test-stripe", "evidence", name),
                    "kind": "source",
                    "visibility": "consumer_private",
                    "source_uri": f"repository-snapshot://{ids['snapshot']}/{file}",
                    "revision": COMMIT,
                    "file": file,
                    "line_start": line,
                    "line_end": line,
                    "json_pointer": None,
                    "excerpt": excerpt,
                }
            ],
        }
    )


NODE_CALL = "stripe.paymentIntents.retrieve"
PY_CALL = "stripe.PaymentIntent.retrieve"
CLIENT_CALL = "client.v1.payment_intents.retrieve"


def stripe_dependencies() -> dict[str, Dependency]:
    return {
        "checkout_display": dependency(
            "checkout_display", "checkout", language="typescript", field_path="response.amount", operation="divide",
            operand=100, file="src/orderSummary.ts", line=22, excerpt="const totalDollars = paymentIntent.amount / 100;",
            call=NODE_CALL,
        ),
        "checkout_tax_local": dependency(  # negative control: basis points, not a Stripe field
            "checkout_tax_local", "checkout", language="typescript", field_path="basisPoints", operation="divide",
            operand=100, file="src/tax.ts", line=6, excerpt="return basisPoints / 100;", call="local",
        ),
        "checkout_reward_local": dependency(  # negative control: a local object's amount
            "checkout_reward_local", "checkout", language="typescript", field_path="reward.amount",
            operation="divide", operand=100, file="src/rewards.ts", line=11, excerpt="(reward.amount / 100)",
            call="local",
        ),
        "fraud_threshold": dependency(
            "fraud_threshold", "fraud", language="python", field_path="response.amount", operation="compare",
            operand=500000, file="fraud_service/review.py", line=24, excerpt="if payment_intent.amount > 500000:",
            call=PY_CALL,
        ),
        "fraud_velocity_local": dependency(  # negative control: a count named amount
            "fraud_velocity_local", "fraud", language="python", field_path="limit.amount", operation="compare",
            operand=None, file="fraud_service/velocity.py", line=18, excerpt="return payments_in_window > limit.amount",
            call="local",
        ),
        "reporting_summary_currency": dependency(  # the control's path: reads currency, not amount
            "reporting_summary_currency", "reporting", language="python", field_path="response.currency",
            operation="read", file="reporting_app/summary.py", line=21,
            excerpt="counts[(payment_intent.currency, payment_intent.status)] += 1", call=CLIENT_CALL,
        ),
        "reporting_ledger_store": dependency(
            "reporting_ledger_store", "reporting", language="python", field_path="response.amount",
            operation="store", destination="sqlite payments.amount_minor", file="reporting_app/ledger.py", line=34,
            excerpt="(payment_intent.id, payment_intent.currency, payment_intent.amount),", call=CLIENT_CALL,
        ),
    }


def snapshot_of(consumer: str) -> RepositorySnapshot:
    ids = CONSUMERS[consumer]
    return RepositorySnapshot(
        id=ids["snapshot"],
        repository_connection_id=ids["connection"],
        commit_sha=COMMIT,
        fetched_at=datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        content_hash="sha256:" + "0" * 64,
    )


TRUSTED = PlanConfig(frozenset(ids["connection"] for ids in CONSUMERS.values()))


def risk_with(result: AnalysisResult, dep: Dependency) -> Risk:
    (risk,) = [risk for risk in result.risks if str(dep.id) in {str(item) for item in risk.dependency_ids}]
    return risk


@pytest.fixture(scope="module")
def change() -> APIChange:
    return stripe_change()


@pytest.fixture(scope="module")
def deps() -> dict[str, Dependency]:
    return stripe_dependencies()


@pytest.fixture(scope="module")
def analysis(change, deps) -> AnalysisResult:
    return analyze_impact(change, list(deps.values()))


@pytest.fixture(scope="module")
def checkout_plan(analysis, deps) -> VerificationPlan:
    return build_verification_plan(risk_with(analysis, deps["checkout_display"]), snapshot_of("checkout"), config=TRUSTED)


@pytest.fixture(scope="module")
def fraud_plan(analysis, deps) -> VerificationPlan:
    return build_verification_plan(risk_with(analysis, deps["fraud_threshold"]), snapshot_of("fraud"), config=TRUSTED)


# Catalog -----------------------------------------------------------------------------------------


def test_stripe_catalog_entries_use_the_fixed_identifiers() -> None:
    checkout = catalog.get_scenario(STRIPE_CHECKOUT_SCENARIO)
    fraud = catalog.get_scenario(STRIPE_FRAUD_SCENARIO)
    control = catalog.get_scenario(STRIPE_REPORTING_CONTROL)

    assert STRIPE_CHECKOUT_SCENARIO == "stripe.payment-intent-amount-units.checkout-display.typescript"
    assert STRIPE_FRAUD_SCENARIO == "stripe.payment-intent-amount-units.fraud-threshold.python"
    assert STRIPE_REPORTING_CONTROL == "stripe.payment-intent.reporting-summary.control.python"
    assert (checkout.kind, checkout.category, checkout.usage_operation, checkout.usage_operand) == (
        "risk", "conversion", "divide", 100,
    )
    assert (fraud.kind, fraud.category, fraud.usage_operation, fraud.usage_operand) == (
        "risk", "comparison", "compare", 500000,
    )
    assert (control.kind, control.category, control.usage_operation) == ("control", None, None)
    assert catalog.CONTROL_SCENARIO_IDS == (STRIPE_REPORTING_CONTROL,)
    for spec in (checkout, fraud, control):
        assert (spec.service_id, spec.method, spec.path, spec.field_path) == ("stripe", "GET", PI_PATH, "response.amount")
        assert (spec.old_fixture_id, spec.new_fixture_id) == (STRIPE_FIXTURE_BASELINE, STRIPE_FIXTURE_PROPOSED)
        assert spec.binding.http_injection == "stripe_sdk_env"
        assert (spec.binding.api_key_env, spec.binding.api_base_env) == ("STRIPE_SECRET_KEY", "STRIPE_API_BASE")
        assert len(f"Executable scenario available, not yet run: {spec.expected_behavior}") <= 1000  # Evidence bound
    assert (checkout.binding.language, checkout.binding.consumer_file, checkout.binding.function) == (
        "typescript", "src/orderSummary.ts", "getOrderSummary",
    )
    assert (fraud.binding.language, fraud.binding.consumer_file, fraud.binding.module, fraud.binding.function) == (
        "python", "fraud_service/review.py", "fraud_service.review", "review_payment",
    )
    assert (control.binding.consumer_file, control.binding.module, control.binding.function) == (
        "reporting_app/summary.py", "reporting_app.summary", "summarize_payment_intents",
    )
    assert [(case.case_id, case.relevant, case.expected) for case in checkout.cases] == [
        ("displayed_total_usd", True, "$10.00"),
        ("tax_note_unchanged", False, "Includes 8.25% sales tax"),
    ]
    assert [(case.case_id, case.relevant, case.args, case.expected) for case in fraud.cases] == [
        ("below_threshold_not_flagged", False, ["pi_demo_4000usd"], False),
        ("at_threshold_not_flagged", False, ["pi_demo_5000usd"], False),
        ("above_threshold_flagged", True, ["pi_demo_6000usd"], True),
    ]
    (summary,) = control.cases
    assert summary.relevant is False and summary.result_path == ()
    assert summary.expected == {"eur": {"requires_capture": 1}, "usd": {"processing": 1, "succeeded": 3}}
    assert summary.args == [list(catalog.STRIPE_DEMO_PAYMENT_INTENTS)]
    assert catalog.case_request_paths(control, summary) == tuple(
        f"/v1/payment_intents/{pi}" for pi in catalog.STRIPE_DEMO_PAYMENT_INTENTS
    )


def test_stripe_fixture_sets_resolve_to_the_canonical_demo_files() -> None:
    for fixture_id, variant in ((STRIPE_FIXTURE_BASELINE, "baseline"), (STRIPE_FIXTURE_PROPOSED, "proposed")):
        path = catalog.fixture_path(fixture_id)
        assert path == REPO_ROOT / "demo" / "stripe-fixtures" / variant / "payment_intents.json"
        document = catalog.load_fixture(fixture_id)
        assert document["fixture_set_id"] == fixture_id and document["variant"] == variant
        assert document["provider"] == "Stripe" and document["api_version"] == API_VERSION
        assert catalog.fixture_api_version(fixture_id) == API_VERSION
        assert catalog.fixture_sha256(fixture_id) == hashlib.sha256(path.read_bytes()).hexdigest()
    baseline = catalog.load_fixture(STRIPE_FIXTURE_BASELINE)["responses"]
    proposed = catalog.load_fixture(STRIPE_FIXTURE_PROPOSED)["responses"]
    assert (baseline["pi_demo_10usd"]["amount"], proposed["pi_demo_10usd"]["amount"]) == (1000, 10)
    assert (baseline["pi_demo_6000usd"]["amount"], proposed["pi_demo_6000usd"]["amount"]) == (600000, 6000)
    assert baseline["pi_demo_25eur"] == proposed["pi_demo_25eur"]  # outside the USD scope
    assert "SIMULATED" in catalog.load_fixture(STRIPE_FIXTURE_PROPOSED)["label"]


# Demo Payments stays byte-identical: harness assets, bindings and pointer revisions.
DEMO_PAYMENTS_ASSET_SHA256 = {
    "python_display_harness.py": "01f09f665e8ceae22618d5f8b20e41bb1a48feb1f567a64339eee1d91a34621f",
    "node_display_harness.mjs": "08f60e60d98c4b9ac3ab30ef4556d77f60f1924ff1ccae448868a8c48563bfd0",
}


def test_demo_payments_scenarios_are_unchanged() -> None:
    for scenario_id in (
        "demo-payments.charge-amount-units.display.python",
        "demo-payments.charge-amount-units.display.typescript",
    ):
        spec = catalog.get_scenario(scenario_id)
        assert hashlib.sha256((catalog.ASSETS_DIR / spec.harness_asset).read_bytes()).hexdigest() == (
            DEMO_PAYMENTS_ASSET_SHA256[spec.harness_asset]
        )
        assert spec.cases == () and spec.expected_display == "$10.00" and spec.category == "conversion"
        assert set(staging.binding_document(spec)) == {
            "schema_version", "scenario_id", "assertion_id", "expected_display", "binding", "harness_sha256",
        }
        assert spec.revision == "shockwave-scenarios@2026-10-16.1"  # pointers in the checked-in samples


def test_case_binding_document_lists_the_cases(tmp_path: Path) -> None:
    spec = catalog.get_scenario(STRIPE_FRAUD_SCENARIO)
    document = staging.binding_document(spec)

    assert document["report_format"] == staging.CASES_REPORT_FORMAT == "shockwave.cases.v1"
    assert document["operation"] == {"method": "GET", "path": PI_PATH}
    assert document["binding"] == {
        "language": "python",
        "module": "fraud_service.review",
        "function": "review_payment",
        "http_injection": "stripe_sdk_env",
        "api_key_env": "STRIPE_SECRET_KEY",
        "api_base_env": "STRIPE_API_BASE",
    }
    assert document["cases"][2] == {
        "case_id": "above_threshold_flagged",
        "relevant": True,
        "args": ["pi_demo_6000usd"],
        "result_path": ["needs_manual_review"],
        "expected": True,
        "resource_ids": ["pi_demo_6000usd"],
    }
    assert document["harness_sha256"] == staging.harness_sha256(spec)
    assert staging.binding_bytes(spec) == staging.binding_bytes(spec)  # deterministic
    for asset in ("python_stripe_harness.py", "node_stripe_harness.mjs"):
        text = (catalog.ASSETS_DIR / asset).read_text(encoding="utf-8")
        assert OFFLINE_KEY in text and "sk_live" not in text


def test_controls_are_staged_per_variant_without_a_plan(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    control = catalog.get_scenario(STRIPE_REPORTING_CONTROL)

    staged = staging.stage_control(STRIPE_REPORTING_CONTROL, checkout, "proposed", tmp_path / "work")

    assert staged.phase == "proposed" and staged.fixture_id == STRIPE_FIXTURE_PROPOSED
    assert staged.report_path.name == "control-proposed-report.json"
    assert staged.fixture_path.read_bytes() == catalog.fixture_path(STRIPE_FIXTURE_PROPOSED).read_bytes()
    assert (staged.staged_dir / "pytest.ini").exists() and (staged.staged_dir / "binding.json").exists()
    assert set(staged.env) == {*staging.ENV_KEYS, *staging.PYTHON_RUNNER_ENV}
    assert staging.control_argv_hint(STRIPE_REPORTING_CONTROL) == [
        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest",
        "-c", f"{control.staged_dir}/pytest.ini", "--rootdir", control.staged_dir, control.staged_test_file,
    ]
    for refused in (STRIPE_FRAUD_SCENARIO, "demo-payments.unknown"):
        with pytest.raises(ValueError, match="not a control scenario"):
            staging.stage_control(refused, checkout, "baseline", tmp_path / "work")
    with pytest.raises(ValueError, match="variant must be one of"):
        staging.stage_control(STRIPE_REPORTING_CONTROL, checkout, "patched", tmp_path / "work")  # type: ignore[arg-type]


# Impact: scenario pointers for conversion and comparison risks ------------------------------------


def test_find_applicable_scenario_binds_the_checkout_and_fraud_usages(change, deps) -> None:
    assert find_applicable_scenario(change, deps["checkout_display"]).scenario_id == STRIPE_CHECKOUT_SCENARIO
    assert find_applicable_scenario(change, deps["fraud_threshold"]).scenario_id == STRIPE_FRAUD_SCENARIO
    for handle in ("reporting_ledger_store", "reporting_summary_currency", "checkout_tax_local", "fraud_velocity_local"):
        assert find_applicable_scenario(change, deps[handle]) is None


def _variant(dep: Dependency, **updates: object) -> Dependency:
    document = dep.model_dump(mode="json")
    usage = updates.pop("usage", None)
    file = updates.pop("file", None)
    document.update(updates)
    if usage is not None:
        document["usage"] = {**document["usage"], **usage}
    if file is not None:
        document["evidence"][0]["file"] = file
    return Dependency.model_validate(document)


@pytest.mark.parametrize(
    "updates",
    [
        {"usage": {"operand": 5000}},  # another threshold
        {"usage": {"operation": "divide", "operand": 500000}},  # another usage
        {"language": "javascript"},  # another language
        {"file": "fraud_service/other.py"},  # not the bound consumer file
        {"confidence": "unresolved"},  # origin not established at all
        {"service_id": "demo-payments"},  # another provider
    ],
    ids=["operand", "operation", "language", "consumer-file", "unresolved", "service"],
)
def test_the_fraud_scenario_needs_the_exact_bound_usage(change, deps, updates) -> None:
    assert find_applicable_scenario(change, _variant(deps["fraud_threshold"], **updates)) is None


@pytest.mark.parametrize(
    "variant",
    [
        {"old": "EUR amount in minor currency units (cents)", "new": "EUR amount in major currency units"},
        {"old": NEW_SEMANTICS, "new": OLD_SEMANTICS},  # the other direction
        {"openapi": False},  # no operation evidence for GET /v1/payment_intents/{intent}
    ],
    ids=["currency", "direction", "no-openapi-evidence"],
)
def test_the_change_must_match_the_scenario(deps, variant) -> None:
    change = stripe_change(**variant)
    for handle in ("checkout_display", "fraud_threshold"):
        assert find_applicable_scenario(change, deps[handle]) is None


def test_controls_never_match_and_never_get_pointers(change, deps) -> None:
    control = catalog.get_scenario(STRIPE_REPORTING_CONTROL)
    lookalike = _variant(
        deps["fraud_threshold"],
        consumer_org_id=CONSUMERS["reporting"]["org"],
        enrollment_id=CONSUMERS["reporting"]["enrollment"],
        repository_snapshot_id=CONSUMERS["reporting"]["snapshot"],
        file=control.binding.consumer_file,
    )
    assert find_applicable_scenario(change, lookalike) is None
    with pytest.raises(ValueError, match="controls are never bound to a Risk"):
        scenario_pointer_evidence(control, lookalike)

    forged = Evidence(
        id=catalog.scenario_pointer_id(STRIPE_REPORTING_CONTROL, lookalike.id),
        kind="test",
        visibility="consumer_private",
        source_uri=f"{catalog.SCENARIO_URI_PREFIX}{STRIPE_REPORTING_CONTROL}",
        revision=catalog.CATALOG_REVISION,
    )
    risk = Risk(
        id=risk_id(change.id, lookalike.enrollment_id, lookalike.repository_snapshot_id, "comparison"),
        change_id=change.id,
        consumer_org_id=lookalike.consumer_org_id,
        enrollment_id=lookalike.enrollment_id,
        repository_snapshot_id=lookalike.repository_snapshot_id,
        dependency_ids=[lookalike.id],
        severity="high",
        confidence="inferred",
        explanation="forged",
        suggested_action="none",
        evidence=[*lookalike.evidence, forged],
    )
    with pytest.raises(NoExecutableScenario, match="is a control"):
        build_verification_plan(risk, snapshot_of("reporting"), config=TRUSTED)
    result = AnalysisResult(
        schema_version="shockwave.v1", change_id=change.id, risks=[risk], unresolved_dependency_ids=[],
        warnings=[], analysis_mode="rules",
    )
    problems = validate_risk_evidence(result, change, [lookalike])
    assert any("points to a control scenario" in problem for problem in problems)


def test_analyze_impact_mints_conversion_and_comparison_pointers(change, deps, analysis) -> None:
    checkout = risk_with(analysis, deps["checkout_display"])
    fraud = risk_with(analysis, deps["fraud_threshold"])
    ledger = risk_with(analysis, deps["reporting_ledger_store"])

    assert analysis.analysis_mode == "rules"
    assert len(analysis.risks) == 3
    assert validate_risk_evidence(analysis, change, list(deps.values())) == []
    expected_keys = {
        checkout.id: f"conversion:{STRIPE_CHECKOUT_SCENARIO}",
        fraud.id: f"comparison:{STRIPE_FRAUD_SCENARIO}",
        ledger.id: "storage",
    }
    for risk in (checkout, fraud, ledger):
        assert risk.id == risk_id(change.id, risk.enrollment_id, risk.repository_snapshot_id, expected_keys[risk.id])
    assert (risk_category(change, checkout), risk_category(change, fraud), risk_category(change, ledger)) == (
        "conversion", "comparison", "storage",
    )
    assert bound_scenario_ids(checkout.evidence, checkout.dependency_ids) == [STRIPE_CHECKOUT_SCENARIO]
    assert bound_scenario_ids(fraud.evidence, fraud.dependency_ids) == [STRIPE_FRAUD_SCENARIO]
    assert bound_scenario_ids(ledger.evidence, ledger.dependency_ids) == []
    (pointer,) = [item for item in fraud.evidence if item.kind == "test"]
    assert pointer == scenario_pointer_evidence(catalog.get_scenario(STRIPE_FRAUD_SCENARIO), deps["fraud_threshold"])
    assert pointer.revision == catalog.STRIPE_REVISION and "not yet run" in (pointer.excerpt or "")
    # The comparison prediction says a scenario exists and no longer lists it as missing evidence.
    assert (fraud.severity, fraud.confidence) == ("high", "inferred")
    assert "An executable scenario exists for this usage; it has not run." in fraud.explanation
    assert "an executable comparison scenario" not in fraud.explanation
    assert "intended unit of the threshold" in fraud.explanation
    assert "(500000 -> 5000)" in fraud.suggested_action
    assert (checkout.severity, checkout.confidence) == ("high", "direct")
    assert "$0.10 instead of $10.00" in checkout.explanation
    # Negative controls (local amounts, basis points) and the currency read are not matched at all.
    matched = {str(item) for risk in analysis.risks for item in risk.dependency_ids}
    matched |= {str(item) for item in analysis.unresolved_dependency_ids}
    for handle in ("checkout_tax_local", "checkout_reward_local", "fraud_velocity_local", "reporting_summary_currency"):
        assert str(deps[handle].id) not in matched


def test_a_pointer_whose_scenario_category_mismatches_the_risk_is_rejected(change, deps, analysis) -> None:
    fraud_dep, checkout_dep = deps["fraud_threshold"], deps["checkout_display"]
    own_category = "a scenario proves only risks of its own category"

    fraud = risk_with(analysis, fraud_dep).model_copy(deep=True)
    fraud.evidence.append(scenario_pointer_evidence(catalog.get_scenario(STRIPE_CHECKOUT_SCENARIO), checkout_dep))
    stolen = analysis.model_copy(update={"risks": [fraud]})
    problems = validate_risk_evidence(stolen, change, list(deps.values()))
    assert any(f"points to a conversion scenario, but the risk's category is comparison ({own_category})" in p for p in problems)

    as_conversion = build_risk(
        change, [fraud_dep], category="conversion", severity="high", confidence="inferred",
        explanation="mislabelled on purpose", suggested_action="none",
    )
    assert [item for item in as_conversion.evidence if item.kind == "test"] == []  # only its own category's pointers
    as_conversion.evidence.append(scenario_pointer_evidence(catalog.get_scenario(STRIPE_FRAUD_SCENARIO), fraud_dep))
    forged = analysis.model_copy(update={"risks": [as_conversion]})
    problems = validate_risk_evidence(forged, change, list(deps.values()))
    assert any(f"points to a comparison scenario, but the risk's category is conversion ({own_category})" in p for p in problems)


class _ScriptedClient:
    model_id = "scripted-model"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: dict) -> ModelReply:
        self.calls += 1
        return ModelReply(text=self.text, model=self.model_id, stop_reason="end_turn", live=True)


def test_model_mode_keys_and_points_comparison_risks_like_rules_mode(change, deps, analysis) -> None:
    fraud = deps["fraud_threshold"]
    reply = {
        "assessments": [
            {
                "dependency_ids": [str(fraud.id)],
                "category": "comparison",
                "severity": "high",
                "confidence": "direct",
                "explanation": "fraud_service/review.py:24 compares response.amount with 500000; in dollars the "
                "threshold would sit 100x higher.",
                "suggested_action": "Rescale the threshold to 5000 for the proposed contract.",
                "missing_evidence": ["intended unit of the threshold"],
            }
        ],
        "unresolved_dependency_ids": [],
    }
    client = _ScriptedClient(json.dumps(reply))
    model = ImpactAnalyzer("model", model_analyzer=ModelImpactAnalyzer(client, model_processing_allowed=lambda _: True))

    result = model.analyze(change, [fraud])

    (risk,) = result.risks
    assert result.analysis_mode == "model" and client.calls == 1
    assert risk.id == risk_with(analysis, fraud).id  # keyed comparison:<scenario>, exactly as in rules mode
    assert bound_scenario_ids(risk.evidence, risk.dependency_ids) == [STRIPE_FRAUD_SCENARIO]
    assert risk.confidence == "inferred"  # capped by the rules' comparison outcome
    assert validate_risk_evidence(result, change, [fraud]) == []


def test_plans_for_the_stripe_risks(checkout_plan, fraud_plan, analysis, deps) -> None:
    for plan, scenario_id, profile in (
        (checkout_plan, STRIPE_CHECKOUT_SCENARIO, "trusted_node"),
        (fraud_plan, STRIPE_FRAUD_SCENARIO, "trusted_python"),
    ):
        spec = catalog.get_scenario(scenario_id)
        assert (plan.scenario_id, plan.runner_profile, plan.timeout_seconds) == (scenario_id, profile, 60)
        assert plan.test_files == [spec.staged_test_file]
        assert (plan.old_fixture_id, plan.new_fixture_id) == (STRIPE_FIXTURE_BASELINE, STRIPE_FIXTURE_PROPOSED)
        assert plan.expected_behavior == spec.expected_behavior
    untrusted = build_verification_plan(risk_with(analysis, deps["fraud_threshold"]), snapshot_of("fraud"), config=PlanConfig())
    assert untrusted.runner_profile == "sandbox_python" and untrusted.id == fraud_plan.id
    with pytest.raises(NoExecutableScenario, match="storage-only"):
        build_verification_plan(risk_with(analysis, deps["reporting_ledger_store"]), snapshot_of("reporting"), config=TRUSTED)


def test_a_plan_naming_a_control_is_refused() -> None:
    control = catalog.get_scenario(STRIPE_REPORTING_CONTROL)
    plan = VerificationPlan(
        id=stable_id("test-stripe", "control-plan"),
        risk_id=stable_id("test-stripe", "control-risk"),
        repository_snapshot_id=CONSUMERS["reporting"]["snapshot"],
        scenario_id=control.scenario_id,
        runner_profile=control.trusted_profile,
        test_files=[control.staged_test_file],
        old_fixture_id=control.old_fixture_id,
        new_fixture_id=control.new_fixture_id,
        expected_behavior=control.expected_behavior,
        timeout_seconds=60,
    )
    with pytest.raises(ValueError, match="refusing plan: .* is a control scenario"):
        staging.validated_spec(plan)
    assert classify_phase(plan, "baseline", _run(_report(control, "baseline"))).outcome == "inconclusive"


# Case interpretation through every CASE_OUTCOME_TABLE row (synthetic evidence) -----------------------

FRAUD = catalog.get_scenario(STRIPE_FRAUD_SCENARIO)
CONTROL = catalog.get_scenario(STRIPE_REPORTING_CONTROL)
SYNTHETIC_PLAN = VerificationPlan(
    id=stable_id("test-stripe", "synthetic-plan"),
    risk_id=stable_id("test-stripe", "synthetic-risk"),
    repository_snapshot_id=CONSUMERS["fraud"]["snapshot"],
    scenario_id=FRAUD.scenario_id,
    runner_profile="sandbox_python",
    test_files=[FRAUD.staged_test_file],
    old_fixture_id=FRAUD.old_fixture_id,
    new_fixture_id=FRAUD.new_fixture_id,
    expected_behavior=FRAUD.expected_behavior,
    timeout_seconds=60,
)
_UNSET = object()


def _request(resource_id: str, **overrides: object) -> dict:
    return {
        "method": "GET",
        "path": f"/v1/payment_intents/{resource_id}",
        "stripe_version": API_VERSION,
        "authorization": "test_mode_bearer",
        **overrides,
    }


def _entry(case: catalog.ScenarioCase, actual: object = _UNSET, **overrides: object) -> dict:
    value = case.expected if actual is _UNSET else actual
    entry = {
        "case_id": case.case_id,
        "relevant": case.relevant,
        "expected": case.expected,
        "actual": value,
        "status": "passed" if canonical_json(value) == case.expected_json else "failed",
        "error_type": None,
        "message": "synthetic",
        "requests": [_request(resource_id) for resource_id in case.resource_ids],
    }
    entry.update(overrides)
    return entry


def _fixture_for(spec: catalog.ScenarioSpec, phase: str) -> str:
    return spec.old_fixture_id if phase == "baseline" else spec.new_fixture_id


def _report(spec: catalog.ScenarioSpec, phase: str, *, cases: dict[str, dict] | None = None, **overrides: object) -> dict:
    """A case report for ``spec``; ``cases`` replaces entries by case id."""
    entries = [(cases or {}).get(case.case_id) or _entry(case) for case in spec.cases]
    fixture_id = _fixture_for(spec, phase)
    report = {
        "schema_version": "shockwave.v1",
        "kind": "scenario_report",
        "report_format": "shockwave.cases.v1",
        "scenario_id": spec.scenario_id,
        "scenario_kind": spec.kind,
        "phase": phase,
        "fixture_id": fixture_id,
        "fixture_sha256": catalog.fixture_sha256(fixture_id),
        "status": "passed" if all(entry["status"] == "passed" for entry in entries) else "failed",
        "error_type": None,
        "message": "synthetic",
        "cases": entries,
        "unattributed_requests": [],
        "requests_dropped": 0,
        "harness_sha256": staging.harness_sha256(spec),
        "binding_sha256": staging.binding_sha256(spec),
    }
    report.update(overrides)
    return report


def _execution(*, exit_code: int | None = 0, tests_run: int = 1, timed_out: bool = False, error: str | None = None):
    return ExecutionResult(
        exit_code=exit_code, timed_out=timed_out, infrastructure_error=error, artifact_ids=[], duration_ms=0,
        tests_run=tests_run,
    )


def _run(report: object, *, exit_code: int | None = None, **execution: object) -> PhaseRun:
    if exit_code is None and isinstance(report, dict):
        exit_code = 0 if report.get("status") == "passed" else 1
    return PhaseRun(_execution(exit_code=exit_code, **execution), report)  # type: ignore[arg-type]


def _relevant(spec: catalog.ScenarioSpec) -> catalog.ScenarioCase:
    return spec.relevant_case or spec.cases[0]


def _guard(spec: catalog.ScenarioSpec) -> catalog.ScenarioCase:
    return spec.guard_cases[-1] if spec.guard_cases else spec.cases[0]


def _wrong_value(case: catalog.ScenarioCase) -> object:
    expected = case.expected
    if isinstance(expected, bool):
        return not expected
    if isinstance(expected, dict):
        return {"usd": {"succeeded": 4}}
    return "$0.10"


def _with_case(spec, phase, case, **entry_overrides) -> dict:
    return _report(spec, phase, cases={case.case_id: _entry(case, **entry_overrides)})


CaseBuilder = Callable[[catalog.ScenarioSpec, str], PhaseRun | None]
CASE_BUILDERS: dict[str, CaseBuilder] = {
    "not_executed": lambda spec, phase: None,
    "pass": lambda spec, phase: _run(_report(spec, phase)),
    "relevant_fail": lambda spec, phase: _run(_with_case(spec, phase, _relevant(spec), actual=_wrong_value(_relevant(spec)))),
    "guard_fail": lambda spec, phase: _run(_with_case(spec, phase, _guard(spec), actual=_wrong_value(_guard(spec)))),
    "relevant_and_guard_fail": lambda spec, phase: _run(
        _report(
            spec,
            phase,
            cases={
                _relevant(spec).case_id: _entry(_relevant(spec), _wrong_value(_relevant(spec))),
                _guard(spec).case_id: _entry(_guard(spec), _wrong_value(_guard(spec))),
            },
        )
    ),
    "case_error": lambda spec, phase: _run(
        _with_case(
            spec, phase, spec.cases[0], actual=None, status="error", error_type="InvalidRequestError",
            message="InvalidRequestError: No such payment_intent: 'pi_missing'",
        )
    ),
    "harness_error": lambda spec, phase: _run(
        _report(
            spec, phase, status="error", cases=None, error_type="ModuleNotFoundError",
            message="ModuleNotFoundError: No module named 'stripe'",
        )
        | {"cases": []},
        exit_code=1,
    ),
    "infrastructure_error": lambda spec, phase: _run(None, exit_code=None, tests_run=0, error="no sandbox available"),
    "timeout": lambda spec, phase: PhaseRun(_execution(exit_code=None, tests_run=0, timed_out=True), None),
    "zero_tests": lambda spec, phase: PhaseRun(_execution(exit_code=0, tests_run=0), None),
    "missing_report": lambda spec, phase: PhaseRun(_execution(exit_code=1), None),
    "unparseable_report": lambda spec, phase: PhaseRun(_execution(exit_code=1), b"{not json"),
    "oversized_report": lambda spec, phase: PhaseRun(_execution(exit_code=1), _report(spec, phase, message="x" * 70000)),
    "tampered_expectation": lambda spec, phase: _run(
        _with_case(
            spec, phase, _relevant(spec), expected=_wrong_value(_relevant(spec)), actual=_wrong_value(_relevant(spec)),
            status="passed",
        )
    ),
    # Otherwise self-consistent passing reports (exit 0), so only the identity/integrity check can object.
    "wrong_fixture": lambda spec, phase: _run(_report(spec, "baseline" if phase != "baseline" else "proposed") | {"phase": phase}),
    "modified_harness": lambda spec, phase: _run(_report(spec, phase, harness_sha256="0" * 64)),
    "tampered_binding": lambda spec, phase: _run(_report(spec, phase, binding_sha256="0" * 64)),
    "fixture_integrity": lambda spec, phase: _run(_report(spec, phase, fixture_sha256="0" * 64)),
    "no_request": lambda spec, phase: _run(_with_case(spec, phase, spec.cases[0], requests=[])),
    "wrong_path": lambda spec, phase: _run(
        _with_case(spec, phase, _relevant(spec), requests=[_request("pi_demo_10usd")])
    ),
    "missing_stripe_version": lambda spec, phase: _run(
        _with_case(spec, phase, spec.cases[0], requests=[_request(rid, stripe_version=None) for rid in spec.cases[0].resource_ids])
    ),
    "live_mode_auth": lambda spec, phase: _run(
        _with_case(spec, phase, spec.cases[0], requests=[_request(rid, authorization="other") for rid in spec.cases[0].resource_ids])
    ),
    "unattributed_request": lambda spec, phase: _run(
        _report(spec, phase, unattributed_requests=[_request("pi_demo_10usd")])
    ),
    "failed_report_exit_zero": lambda spec, phase: _run(
        _with_case(spec, phase, _relevant(spec), actual=_wrong_value(_relevant(spec))), exit_code=0
    ),
    "passed_report_exit_nonzero": lambda spec, phase: _run(_report(spec, phase), exit_code=1),
    "control_fail": lambda spec, phase: _run(_with_case(spec, phase, spec.cases[0], actual=_wrong_value(spec.cases[0]))),
}


def _status_for(row: OutcomeRow) -> tuple[str, str]:
    if row.which_function == "interpret_verification":
        assert row.baseline is not None
        result = interpret_verification(
            SYNTHETIC_PLAN,
            CASE_BUILDERS[row.baseline](FRAUD, "baseline"),
            CASE_BUILDERS[row.proposed_or_patched](FRAUD, "proposed"),
        )
        return result.status, result.summary
    if row.which_function == "interpret_patched_run":
        outcome = interpret_patched_run(SYNTHETIC_PLAN, CASE_BUILDERS[row.proposed_or_patched](FRAUD, "patched"))
        return outcome.status, outcome.reason
    control = interpret_control(STRIPE_REPORTING_CONTROL, "proposed", CASE_BUILDERS[row.proposed_or_patched](CONTROL, "proposed"))
    return control.status, control.reason


@pytest.mark.parametrize("row", CASE_OUTCOME_TABLE, ids=[row.case.replace(" ", "-") for row in CASE_OUTCOME_TABLE])
def test_case_outcome_table_row(row: OutcomeRow) -> None:
    status, explanation = _status_for(row)

    assert status == row.expected_status, explanation
    assert explanation


def test_every_case_evidence_key_has_a_builder_and_the_table_is_complete() -> None:
    assert set(CASE_BUILDERS) == set(CASE_PHASE_EVIDENCE)
    for row in CASE_OUTCOME_TABLE:
        assert row.baseline is None or row.baseline in CASE_PHASE_EVIDENCE
        assert row.proposed_or_patched in CASE_PHASE_EVIDENCE
    statuses = {row.expected_status for row in CASE_OUTCOME_TABLE}
    assert statuses == {
        "verified_regression", "compatible_within_tested_scope", "inconclusive", "not_run", "validated",
        "validation_failed", "passed", "failed",
    }
    markdown = render_case_outcome_table_markdown()
    assert len(markdown.strip().splitlines()) == 2 + len(CASE_OUTCOME_TABLE)
    (validated,) = [row for row in CASE_OUTCOME_TABLE if row.expected_status == "validated"]
    assert "pinned to the proposed contract" in validated.why and "cutover" in validated.why


def test_verified_regression_summary_quotes_the_relevant_case() -> None:
    result = interpret_verification(
        SYNTHETIC_PLAN, CASE_BUILDERS["pass"](FRAUD, "baseline"), CASE_BUILDERS["relevant_fail"](FRAUD, "proposed")
    )

    assert result.status == "verified_regression"
    assert "the relevant case above_threshold_flagged failed: expected needs_manual_review=True, actual False" in (
        result.summary
    )
    assert "guard case(s) below_threshold_not_flagged, at_threshold_not_flagged passed" in result.summary
    assert STRIPE_FIXTURE_BASELINE in result.summary and STRIPE_FIXTURE_PROPOSED in result.summary
    assert "Proven for the tested scenario only." in result.summary


@pytest.mark.parametrize(
    ("key", "fragment"),
    [
        ("no_request", "made no request to the fixture server"),
        ("wrong_path", "a request targeted GET /v1/payment_intents/pi_demo_10usd, not GET /v1/payment_intents/{intent}"),
        ("missing_stripe_version", "carried no Stripe-Version header"),
        ("live_mode_auth", "not authenticated with a test-mode key"),
        ("unattributed_request", "outside every case's call"),
        ("guard_fail", "unrelated failure: the guard case at_threshold_not_flagged failed"),
        ("case_error", "raised InvalidRequestError"),
        ("harness_error", "ModuleNotFoundError"),
        ("tampered_expectation", "expectation tampering: case above_threshold_flagged"),
        ("fixture_integrity", "fixture integrity mismatch"),
        ("tampered_binding", "tampered binding"),
        ("modified_harness", "modified harness"),
        ("wrong_fixture", f"requires {STRIPE_FIXTURE_PROPOSED}"),
        ("passed_report_exit_nonzero", "unrelated failure: every case passed but the native run exited 1"),
    ],
)
def test_case_inconclusive_reasons_say_why(key: str, fragment: str) -> None:
    classification = classify_phase(SYNTHETIC_PLAN, "proposed", CASE_BUILDERS[key](FRAUD, "proposed"))

    assert classification.outcome == "inconclusive"
    assert fragment in classification.reason


def test_a_spoofed_request_path_does_not_satisfy_the_preconditions() -> None:
    relevant = _relevant(FRAUD)
    spoofed = [_request("pi_demo_6000usd" + chr(0x200B))]  # renders like the expected path once flattened
    run = _run(_with_case(FRAUD, "proposed", relevant, requests=spoofed))

    classification = classify_phase(SYNTHETIC_PLAN, "proposed", run)

    assert classification.outcome == "inconclusive"
    assert chr(0x200B) not in classification.reason
    assert "a request targeted GET /v1/payment_intents/pi_demo_6000usd" in classification.reason


def test_a_patched_guard_failure_is_a_failed_validation_not_a_validation() -> None:
    outcome = interpret_patched_run(SYNTHETIC_PLAN, CASE_BUILDERS["guard_fail"](FRAUD, "patched"))

    assert outcome.status == "validation_failed"
    assert "the guard case at_threshold_not_flagged failed (expected needs_manual_review=False, actual True)" in (
        outcome.reason
    )
    assert "cutover" not in outcome.reason


def test_control_results_are_not_contract_records() -> None:
    result = interpret_control(STRIPE_REPORTING_CONTROL, "baseline", CASE_BUILDERS["pass"](CONTROL, "baseline"))

    assert isinstance(result, controls.ControlResult)
    assert (result.status, result.variant, result.fixture_id) == ("passed", "baseline", STRIPE_FIXTURE_BASELINE)
    assert result.fixture_sha256 == catalog.fixture_sha256(STRIPE_FIXTURE_BASELINE)
    assert result.tests_run == 1 and len(result.requests) == 5
    document = result.to_document()
    assert document["kind"] == controls.CONTROL_RESULT_KIND
    assert "not tied to any risk" in document["note"]
    assert not {"risk_id", "plan_id", "repository_snapshot_id"} & set(document)
    json.dumps(document, sort_keys=True)  # JSON-ready
    with pytest.raises(ValueError, match="not a control scenario"):
        interpret_control(STRIPE_FRAUD_SCENARIO, "baseline", None)


# Real runs: official SDKs against the harness's loopback fixture server ------------------------------


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "node_modules" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


@pytest.fixture(scope="module")
def repos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Temporary copies of the three templates (checkout keeps its installed node_modules)."""
    root = tmp_path_factory.mktemp("stripe-templates")
    copies: dict[str, Path] = {}
    for name in ("checkout", "fraud", "reporting"):
        destination = root / name
        shutil.copytree(
            TEMPLATES / name,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.sqlite", "node_modules"),
        )
        copies[name] = destination
    modules = TEMPLATES / "checkout" / "node_modules"
    if modules.is_dir():
        # The installed SDK is shared read-only through a symlink (the runner double copies links as
        # links), so no phase checkout duplicates its ~21 MB.
        (copies["checkout"] / "node_modules").symlink_to(modules, target_is_directory=True)
    return copies


@pytest.fixture(scope="module")
def repo_digests(repos) -> dict[str, str]:
    return {name: _tree_digest(path) for name, path in repos.items()}


def _phase(plan, repo: Path, phase: str, work: Path, **options) -> PhaseRun:
    return run_phase_locally(plan, repo, phase, work_dir=work, node_executable=str(NODE), **options)


@pytest.fixture(scope="module")
def checkout_runs(checkout_plan, repos, repo_digests, tmp_path_factory) -> tuple[Path, PhaseRun, PhaseRun]:
    if not NODE.exists() or not (repos["checkout"] / "node_modules" / "stripe").exists():
        pytest.skip("Node.js or the checkout's installed stripe SDK is missing")
    work = tmp_path_factory.mktemp("checkout-runs")
    return work, _phase(checkout_plan, repos["checkout"], "baseline", work), _phase(
        checkout_plan, repos["checkout"], "proposed", work
    )


@pytest.fixture(scope="module")
def fraud_runs(fraud_plan, repos, repo_digests, tmp_path_factory) -> tuple[Path, PhaseRun, PhaseRun]:
    if importlib.util.find_spec("stripe") is None:
        pytest.skip("the stripe Python SDK is not installed")
    work = tmp_path_factory.mktemp("fraud-runs")
    return work, _phase(fraud_plan, repos["fraud"], "baseline", work), _phase(fraud_plan, repos["fraud"], "proposed", work)


def _report_of(run: PhaseRun) -> dict:
    assert isinstance(run.report, bytes), (run.execution, run.output_tail)
    return json.loads(run.report)


def _case(report: dict, case_id: str) -> dict:
    (entry,) = [entry for entry in report["cases"] if entry["case_id"] == case_id]
    return entry


def _edit(relative: str, old: str, new: str) -> Callable[[Path], None]:
    """A patch callable for the runner double; fails loudly if the edit does not apply."""

    def apply(checkout: Path) -> None:
        path = checkout / relative
        text = path.read_text(encoding="utf-8")
        assert old in text, f"patch does not apply to {relative}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    return apply


# The same minimal edits the demo migrations make (DESIGN.md): only the affected expression.
MIGRATE_CHECKOUT = _edit("src/orderSummary.ts", "paymentIntent.amount / 100", "paymentIntent.amount")
MIGRATE_FRAUD = _edit("fraud_service/review.py", "> 500000", "> 5000")


@requires_node
@requires_checkout_sdk
def test_checkout_display_regression_is_verified_through_stripe_node(checkout_plan, checkout_runs) -> None:
    _, baseline, proposed = checkout_runs
    old, new = _report_of(baseline), _report_of(proposed)

    assert (baseline.execution.exit_code, baseline.execution.tests_run) == (0, 1)
    assert (proposed.execution.exit_code, proposed.execution.tests_run) == (1, 1)
    assert (_case(old, "displayed_total_usd")["actual"], _case(old, "displayed_total_usd")["status"]) == ("$10.00", "passed")
    assert (_case(new, "displayed_total_usd")["actual"], _case(new, "displayed_total_usd")["status"]) == ("$0.10", "failed")
    for report in (old, new):  # the unrelated basis-points division is unaffected
        assert _case(report, "tax_note_unchanged")["actual"] == "Includes 8.25% sales tax"

    result = interpret_verification(checkout_plan, baseline, proposed)

    assert result.status == "verified_regression", result.summary
    assert "the relevant case displayed_total_usd failed: expected $10.00, actual $0.10" in result.summary
    assert "guard case(s) tax_note_unchanged passed" in result.summary
    assert (result.baseline, result.proposed) == (baseline.execution, proposed.execution)


@requires_stripe_python
def test_fraud_threshold_regression_is_verified_through_stripe_python(fraud_plan, fraud_runs) -> None:
    _, baseline, proposed = fraud_runs
    old, new = _report_of(baseline), _report_of(proposed)

    assert [entry["status"] for entry in old["cases"]] == ["passed", "passed", "passed"]
    assert [(entry["case_id"], entry["actual"], entry["status"]) for entry in new["cases"]] == [
        ("below_threshold_not_flagged", False, "passed"),
        ("at_threshold_not_flagged", False, "passed"),
        ("above_threshold_flagged", False, "failed"),
    ]
    assert (baseline.execution.exit_code, proposed.execution.exit_code) == (0, 1)

    result = interpret_verification(fraud_plan, baseline, proposed)

    assert result.status == "verified_regression", result.summary
    assert "expected needs_manual_review=True, actual False" in result.summary


@requires_node
@requires_checkout_sdk
@requires_stripe_python
def test_runs_record_the_sdk_requests_without_the_key(checkout_runs, fraud_runs) -> None:
    for runs, expected_ids in (
        (checkout_runs, [["pi_demo_10usd"], ["pi_demo_10usd"]]),
        (fraud_runs, [["pi_demo_4000usd"], ["pi_demo_5000usd"], ["pi_demo_6000usd"]]),
    ):
        for run in runs[1:]:
            report = _report_of(run)
            assert set(report) == set(staging.CASES_REPORT_FIELDS)
            assert all(set(entry) == set(staging.CASE_FIELDS) for entry in report["cases"])
            assert [[request["path"].rsplit("/", 1)[-1] for request in entry["requests"]] for entry in report["cases"]] == (
                expected_ids
            )
            for entry in report["cases"]:
                for request in entry["requests"]:
                    assert request == {
                        "method": "GET",
                        "path": request["path"],
                        "stripe_version": API_VERSION,
                        "authorization": "test_mode_bearer",
                    }
            assert report["unattributed_requests"] == [] and report["requests_dropped"] == 0
            assert report["fixture_sha256"] == catalog.fixture_sha256(report["fixture_id"])
            assert OFFLINE_KEY not in run.report.decode("utf-8") and "Bearer" not in run.report.decode("utf-8")


@pytest.mark.parametrize("which", ["checkout", "fraud"])
def test_patched_proposed_runs_are_validated_with_the_pinned_patch_caveat(
    which, checkout_plan, fraud_plan, repos, tmp_path: Path
) -> None:
    if which == "checkout" and not (NODE.exists() and (repos["checkout"] / "node_modules" / "stripe").exists()):
        pytest.skip("Node.js or the checkout's installed stripe SDK is missing")
    plan, patch = (checkout_plan, MIGRATE_CHECKOUT) if which == "checkout" else (fraud_plan, MIGRATE_FRAUD)

    patched = _phase(plan, repos[which], "patched", tmp_path, patch=patch)

    report = _report_of(patched)
    assert report["fixture_id"] == STRIPE_FIXTURE_PROPOSED
    assert [entry["status"] for entry in report["cases"]] == ["passed"] * len(report["cases"])
    outcome = interpret_patched_run(plan, patched)
    assert outcome.status == "validated", outcome.reason
    assert outcome.reason.startswith("Validated within the tested scope only")
    assert outcome.reason.endswith(PINNED_PATCH_CAVEAT)


@pytest.mark.parametrize("which", ["checkout", "fraud"])
def test_patched_code_fails_against_the_baseline_fixture(which, checkout_plan, fraud_plan, repos, tmp_path: Path) -> None:
    """Deployment-assumption evidence: the migration is pinned to the proposed contract."""
    if which == "checkout" and not (NODE.exists() and (repos["checkout"] / "node_modules" / "stripe").exists()):
        pytest.skip("Node.js or the checkout's installed stripe SDK is missing")
    plan, patch = (checkout_plan, MIGRATE_CHECKOUT) if which == "checkout" else (fraud_plan, MIGRATE_FRAUD)
    old = _phase(plan, repos[which], "baseline", tmp_path / "old", patch=patch)
    new = _phase(plan, repos[which], "proposed", tmp_path / "new", patch=patch)

    report = _report_of(old)
    classification = classify_phase(plan, "baseline", old)
    assert classification.outcome != "passed"
    if which == "checkout":
        assert (_case(report, "displayed_total_usd")["actual"], classification.outcome) == ("$1000.00", "relevant_fail")
    else:  # 400000 and 500000 now exceed 5000: the guards fail against old responses
        assert [entry["status"] for entry in report["cases"]] == ["failed", "failed", "passed"]
        assert classification.guard_failure
    assert classify_phase(plan, "proposed", new).outcome == "passed"  # the same patch passes on proposed responses
    result = interpret_verification(plan, old, new)
    assert result.status == "inconclusive"
    assert "there is no passing baseline" in result.summary


@requires_stripe_python
def test_reporting_summary_control_passes_on_both_variants(repos, tmp_path: Path) -> None:
    for variant, fixture_id in (("baseline", STRIPE_FIXTURE_BASELINE), ("proposed", STRIPE_FIXTURE_PROPOSED)):
        result = run_control_locally(STRIPE_REPORTING_CONTROL, repos["reporting"], variant, work_dir=tmp_path / variant)

        assert result.status == "passed", result.reason
        assert (result.fixture_id, result.fixture_sha256) == (fixture_id, catalog.fixture_sha256(fixture_id))
        assert (result.tests_run, result.execution.exit_code) == (1, 0)
        (summary,) = result.cases
        assert summary.actual == {"eur": {"requires_capture": 1}, "usd": {"processing": 1, "succeeded": 3}}
        assert [request.path for request in result.requests] == [
            f"/v1/payment_intents/{pi}" for pi in catalog.STRIPE_DEMO_PAYMENT_INTENTS
        ]
        assert {(request.stripe_version, request.authorization) for request in result.requests} == {
            (API_VERSION, "test_mode_bearer")
        }
        assert result.reason.startswith(f"Control passed against the {variant} fixture {fixture_id}")
        assert result.output_tail and "1 passed" in result.output_tail  # bounded runner log, not in the document
        stable = result.without_timing().to_document()
        assert stable["execution"]["duration_ms"] == 0 and "output_tail" not in stable
        assert str(tmp_path) not in json.dumps(stable)  # no local paths in the persisted document


@requires_node
@requires_checkout_sdk
@pytest.mark.parametrize(
    ("phase", "patch", "fragment"),
    [
        pytest.param(
            "proposed",
            _edit("src/orderSummary.ts", "retrieve(paymentIntentId)", "retrieve('pi_demo_4000usd')"),
            "a request targeted GET /v1/payment_intents/pi_demo_4000usd",
            id="wrong-resource",
        ),
        pytest.param(
            "baseline",
            _edit(
                "src/orderSummary.ts",
                "await stripe.paymentIntents.retrieve(paymentIntentId)",
                "{ id: paymentIntentId, status: 'succeeded', amount: 1000 }",
            ),
            "made no request to the fixture server",
            id="no-request",
        ),
    ],
)
def test_checkout_precondition_failures_are_inconclusive(checkout_plan, checkout_runs, repos, tmp_path, phase, patch, fragment):
    run = _phase(checkout_plan, repos["checkout"], phase, tmp_path, patch=patch)

    classification = classify_phase(checkout_plan, phase, run)  # type: ignore[arg-type]

    assert classification.outcome == "inconclusive"
    assert fragment in classification.reason
    if phase == "proposed":
        _, baseline, _ = checkout_runs
        result = interpret_verification(checkout_plan, baseline, run)
        assert result.status == "inconclusive", result.summary  # a failing display is not a regression here


@requires_stripe_python
def test_an_unknown_payment_intent_gets_a_stripe_404_and_is_inconclusive(fraud_plan, fraud_runs, repos, tmp_path):
    unknown = _edit("fraud_service/review.py", "retrieve(payment_intent_id)", "retrieve('pi_missing')")

    run = _phase(fraud_plan, repos["fraud"], "proposed", tmp_path, patch=unknown)

    report = _report_of(run)
    assert {entry["status"] for entry in report["cases"]} == {"error"}
    assert {entry["error_type"] for entry in report["cases"]} == {"InvalidRequestError"}
    assert [request["path"] for request in _case(report, "above_threshold_flagged")["requests"]] == [
        "/v1/payment_intents/pi_missing"
    ]
    result = interpret_verification(fraud_plan, fraud_runs[1], run)
    assert result.status == "inconclusive"
    assert "raised InvalidRequestError" in result.summary


@requires_stripe_python
def test_infrastructure_timeout_and_zero_tests_are_inconclusive(analysis, deps, fraud_plan, fraud_runs, repos, tmp_path):
    untrusted = build_verification_plan(risk_with(analysis, deps["fraud_threshold"]), snapshot_of("fraud"), config=PlanConfig())
    refused = _phase(untrusted, repos["fraud"], "proposed", tmp_path / "sandbox")
    slow = _phase(
        fraud_plan,
        repos["fraud"],
        "proposed",
        tmp_path / "slow",
        timeout_seconds=3,
        patch=_edit("fraud_service/review.py", '    configure_stripe()\n', "    import time\n\n    time.sleep(60)\n"),
    )
    silent = _phase(
        fraud_plan,
        repos["fraud"],
        "proposed",
        tmp_path / "silent",
        patch=_edit("fraud_service/review.py", "import stripe\n", "import os\n\nimport stripe\n\nos._exit(0)\n"),
    )

    assert refused.execution.infrastructure_error and "sandbox" in refused.execution.infrastructure_error
    assert slow.execution.timed_out and slow.report is None
    assert (silent.execution.exit_code, silent.execution.tests_run, silent.report) == (0, 0, None)
    for run, fragment in ((refused, "infrastructure error"), (slow, "timed out"), (silent, "zero tests ran")):
        result = interpret_verification(fraud_plan, fraud_runs[1], run)
        assert result.status == "inconclusive", result.summary
        assert fragment in result.summary


GUARD_MESSAGE = "shockwave harness: real network access is disabled"
# Prepended to review_payment. Every attempt targets loopback only (port 9, or a name lookup),
# so even a broken guard sends nothing beyond this machine.
PYTHON_GUARD_PROBE = '''    import socket as _socket

    _outcomes = []
    for _name, _attempt in (
        ("create_connection", lambda: _socket.create_connection(("127.0.0.1", 9), timeout=1)),
        ("connect", lambda: _socket.socket().connect(("127.0.0.1", 9))),
        ("getaddrinfo", lambda: _socket.getaddrinfo("localhost", 443)),
        ("gethostbyname", lambda: _socket.gethostbyname("localhost")),
    ):
        try:
            _attempt()
            _outcomes.append(_name + ":leaked")
        except ConnectionRefusedError as _error:
            _outcomes.append(_name + (":refused" if "shockwave harness" in str(_error) else ":other"))
    with open("guard-probe.txt", "a", encoding="utf-8") as _handle:
        _handle.write(",".join(_outcomes) + "\\n")
'''


@requires_stripe_python
def test_python_guard_allows_only_the_fixture_server(fraud_plan, repos, tmp_path) -> None:
    probe = _edit(
        "fraud_service/review.py",
        '    """Flags payments above USD $5,000.00 for manual review."""\n',
        '    """Flags payments above USD $5,000.00 for manual review."""\n' + PYTHON_GUARD_PROBE,
    )

    run = _phase(fraud_plan, repos["fraud"], "baseline", tmp_path, patch=probe)

    assert _report_of(run)["status"] == "passed"  # the SDK still reaches the fixture server
    lines = (tmp_path / "checkout-baseline" / "guard-probe.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["create_connection:refused,connect:refused,getaddrinfo:refused,gethostbyname:refused"] * 3


NODE_GUARD_IMPORTS = "import { writeFileSync } from 'node:fs';\nimport { lookup } from 'node:dns';\nimport net from 'node:net';\n"
NODE_GUARD_PROBE = """  const outcomes: string[] = [];
  const classify = (name: string, error: unknown) =>
    outcomes.push(`${name}:${String((error as Error)?.message).includes('shockwave harness') ? 'refused' : 'other'}`);
  try {
    await fetch('http://127.0.0.1:9/');
    outcomes.push('fetch:leaked');
  } catch (error) {
    classify('fetch', error);
  }
  try {
    net.connect(9, '127.0.0.1').destroy();
    outcomes.push('net:leaked');
  } catch (error) {
    classify('net', error);
  }
  try {
    await new Promise((resolve, reject) => lookup('localhost', (error) => (error ? reject(error) : resolve(null))));
    outcomes.push('dns:leaked');
  } catch (error) {
    classify('dns', error);
  }
  writeFileSync('guard-probe.txt', `${outcomes.join(',')}\\n`, { flag: 'a' });
"""


@requires_node
@requires_checkout_sdk
def test_node_guard_allows_only_the_fixture_server(checkout_plan, repos, tmp_path) -> None:
    signature = "): Promise<OrderSummary> {\n"
    probe = _edit("src/orderSummary.ts", signature, signature + NODE_GUARD_PROBE)
    imports = _edit("src/orderSummary.ts", "import Stripe from 'stripe';\n", "import Stripe from 'stripe';\n" + NODE_GUARD_IMPORTS)

    def both(checkout: Path) -> None:
        imports(checkout)
        probe(checkout)

    run = _phase(checkout_plan, repos["checkout"], "baseline", tmp_path, patch=both)

    assert _report_of(run)["status"] == "passed", run.output_tail
    lines = (tmp_path / "checkout-baseline" / "guard-probe.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["fetch:refused,net:refused,dns:refused"] * 2


def _load_python_harness() -> types.SimpleNamespace:
    """The harness asset's definitions (runpy compiles in memory: no bytecode cache, no test run)."""
    return types.SimpleNamespace(**runpy.run_path(str(catalog.ASSETS_DIR / "python_stripe_harness.py")))


def test_the_python_fixture_server_serves_stripe_style_responses() -> None:
    harness = _load_python_harness()
    responses = copy.deepcopy(catalog.load_fixture(STRIPE_FIXTURE_BASELINE)["responses"])
    log = harness._RequestLog()
    server, thread = harness._start_fixture_server(responses, PI_PATH, log)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never a proxy

    def call(path: str, method: str = "GET", headers: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(base + path, method=method, headers=headers or {})
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        known = call("/v1/payment_intents/pi_demo_10usd", headers={"Authorization": f"Bearer {OFFLINE_KEY}", "Stripe-Version": API_VERSION})
        unknown = call("/v1/payment_intents/pi_missing", headers={"Authorization": "Bearer sk_live_not_allowed"})
        posted = call("/v1/payment_intents/pi_demo_10usd", method="POST")
        other = call("/v1/charges/ch_1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)

    assert known == (200, responses["pi_demo_10usd"])
    assert unknown[0] == 404 and unknown[1]["error"]["code"] == "resource_missing"
    assert unknown[1]["error"]["type"] == "invalid_request_error" and "pi_missing" in unknown[1]["error"]["message"]
    assert posted[0] == 405 and other[0] == 404
    assert log.since(0) == [
        {"method": "GET", "path": "/v1/payment_intents/pi_demo_10usd", "stripe_version": API_VERSION, "authorization": "test_mode_bearer"},
        {"method": "GET", "path": "/v1/payment_intents/pi_missing", "stripe_version": None, "authorization": "other"},
        {"method": "POST", "path": "/v1/payment_intents/pi_demo_10usd", "stripe_version": None, "authorization": "missing"},
        {"method": "GET", "path": "/v1/charges/ch_1", "stripe_version": None, "authorization": "missing"},
    ]
    assert "sk_live_not_allowed" not in json.dumps(log.since(0)) and OFFLINE_KEY not in json.dumps(log.since(0))


@requires_stripe_python
def test_templates_are_never_mutated_and_checkouts_stay_cache_free(repos, repo_digests, fraud_runs) -> None:
    assert {name: _tree_digest(path) for name, path in repos.items()} == repo_digests
    for name in ("checkout", "fraud", "reporting"):
        assert not any((TEMPLATES / name).rglob(".shockwave"))
        assert not any((TEMPLATES / name).rglob(".pytest_cache"))
    work = fraud_runs[0]
    for phase in ("baseline", "proposed"):
        checkout = work / f"checkout-{phase}"
        assert (checkout / ".shockwave").is_dir()  # staged into the copy, never into the template
        assert not (checkout / ".pytest_cache").exists()
        assert not any(checkout.rglob("__pycache__"))
