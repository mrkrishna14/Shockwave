"""demo/expected/dependencies.fixture.json: the contract-shaped scanner FIXTURE, checked against its sources.

The fixture stands in for Noirit's scanner output until that scanner is integrated; it is not a
completed scan. These tests keep it honest:

* every record validates as a shockwave.v1 Dependency (the contracts in use and the strict, literal
  reading of CONTRACTS.md) and is written out in full, explicit nulls included;
* every evidence excerpt is the actual template line at file:line, so any template drift breaks the
  test, and the evidence traces Stripe import -> client initialization -> SDK call -> usage;
* the negative controls are real lines, have no Stripe SDK origin and no dependency points at them;
* ids come from demo/expected/demo-ids.json and the documented derivations, and endpoint_id is the
  pinned baseline contract's GET /v1/payment_intents/{intent} record;
* analyze_impact turns the records into the predicted risks for the SIMULATED change (deterministic
  rules, nothing executed), while currency/status/id usages stay out of both risks and unresolved.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

from analyzer import analyze_impact
from analyzer.impact import risk_category, validate_risk_evidence
from analyzer.rules import source_location
from discovery import discover_endpoints
from discovery.contracts import (
    APIChange,
    Dependency,
    DiscoveryInput,
    Endpoint,
    Evidence,
)
from discovery.ids import change_id, endpoint_id, evidence_id, fixture_uuid
from discovery.pinned import STRIPE_PIN_PATH, load_pinned_document

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "demo"
FIXTURE_PATH = DEMO / "expected" / "dependencies.fixture.json"
IDS_PATH = DEMO / "expected" / "demo-ids.json"
CONSUMER_MANIFEST_PATH = DEMO / "consumer-repos.manifest.json"
MANIFEST_RELATIVE = "demo/scenarios/stripe-payment-intent-amount-usd-major-units.manifest.json"
MANIFEST_PATH = REPO_ROOT / MANIFEST_RELATIVE
STRICT_SCHEMAS = REPO_ROOT / "tests" / "analyzer" / "fixtures" / "strict_contracts" / "shared" / "schemas.py"
STRIPE_DOCUMENT_REF = "stripe/openapi:latest/openapi.spec3.json"

FIXTURE_LABEL = (
    "FIXTURE \u2014 contract-shaped expected scanner output prepared by Kanishk; NOT a completed scan. "
    "Replace with Noirit's scanner output when integrated."
)
SERVICE_ID = "stripe"
METHOD = "GET"
PATH = "/v1/payment_intents/{intent}"
PLACEHOLDER = "unbound:prepared-commit:{repository}"
SDK_CALLS = {
    "stripe.paymentIntents.retrieve": "stripe.paymentIntents.retrieve(",
    "stripe.PaymentIntent.retrieve": "stripe.PaymentIntent.retrieve(",
    "StripeClient.v1.payment_intents.retrieve": ".v1.payment_intents.retrieve(",
}
SDK_IMPORT = re.compile(r"^(?:import \w+ from 'stripe';|import stripe|from stripe import StripeClient)$")
CLIENT_INITIALIZATION = re.compile(r"^(?:return new Stripe\(.*\);|return StripeClient\(|stripe\.api_key = \w+)$")
CONTROL_FILE_STRIPE_IMPORT = re.compile(r"""from ['"]stripe['"]|^\s*import stripe\b|^\s*from stripe\b""", re.MULTILINE)

# The amount usages the simulated units change reaches, and the usages it leaves alone.
AFFECTED = {
    "checkout_amount_divide": ("conversion", "high", "direct"),
    "fraud_amount_compare": ("comparison", "high", None),  # confidence is not asserted (rules give inferred)
    "reporting_ledger_amount_store": ("storage", "medium", "inferred"),
}
UNAFFECTED = (
    "checkout_status_read",
    "checkout_id_read",
    "fraud_id_read_flagged",
    "fraud_id_read_cleared",
    "reporting_summary_currency_read",
    "reporting_summary_status_read",
    "reporting_ledger_currency_store",
    "reporting_ledger_id_store",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


FIXTURE = read_json(FIXTURE_PATH)
IDS = read_json(IDS_PATH)
RECORDS: dict[str, dict] = FIXTURE["dependencies"]
ANNOTATIONS: dict[str, dict] = FIXTURE["annotations"]
CONTROLS: list[dict] = FIXTURE["negative_controls"]["items"]


def repository_of(handle: str) -> str:
    return ANNOTATIONS[handle]["repository"]


def template_root(repository: str) -> Path:
    return REPO_ROOT / IDS["consumers"][repository]["template"]


@cache
def template_lines(repository: str, file: str) -> tuple[str, ...]:
    return tuple((template_root(repository) / file).read_text(encoding="utf-8").splitlines())


def evidence_by_role(handle: str) -> dict[str, dict]:
    return dict(zip(ANNOTATIONS[handle]["evidence_roles"], RECORDS[handle]["evidence"], strict=True))


def dependencies() -> list[Dependency]:
    return [Dependency.model_validate(record) for record in RECORDS.values()]


def load_strict_contracts() -> ModuleType:
    """The strict literal reading of CONTRACTS.md (tests/analyzer), loaded under a private name."""
    name = "_strict_contracts_for_demo_dependency_fixture"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, STRICT_SCHEMAS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # pydantic resolves the postponed annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# Labels, ids and derivations -------------------------------------------------------------------


def test_fixture_is_labeled_as_a_fixture_not_a_scan() -> None:
    assert FIXTURE["label"] == FIXTURE_LABEL
    assert "Noirit" in FIXTURE["provenance"] and "did not run" in FIXTURE["provenance"]
    binding = FIXTURE["revision_binding"]
    assert binding["placeholder_format"] == PLACEHOLDER
    assert binding["placeholders"] == {repository: PLACEHOLDER.format(repository=repository) for repository in IDS["consumers"]}
    assert "verify every evidence item" in binding["explanation"]
    assert "prepared local commit SHA" in binding["explanation"]


def test_demo_ids_are_derived_fixture_uuids_for_three_separate_companies() -> None:
    for key, name in IDS["fixture_names"].items():
        assert IDS[key] == fixture_uuid(name), key
        assert name.startswith("stripe-demo/")
    manifest = read_json(CONSUMER_MANIFEST_PATH)
    assert sorted(IDS["consumers"]) == sorted(repository["name"] for repository in manifest["repositories"])
    templates = {repository["name"]: repository["template"] for repository in manifest["repositories"]}
    per_consumer_keys = ("consumer_org_id", "enrollment_id", "repository_connection_id", "repository_snapshot_id")
    for repository, consumer in IDS["consumers"].items():
        assert consumer["template"] == templates[repository]
        assert sorted(consumer["fixture_names"]) == sorted(per_consumer_keys)
        for key in per_consumer_keys:
            assert consumer[key] == fixture_uuid(consumer["fixture_names"][key]), (repository, key)
        assert "fictional" in consumer["consumer_org_label"]
    for key in per_consumer_keys:  # three distinct companies, enrollments, connections and snapshots
        assert len({consumer[key] for consumer in IDS["consumers"].values()}) == 3, key
    assert len({consumer["consumer_org_name"] for consumer in IDS["consumers"].values()}) == 3
    every_id = [IDS[key] for key in IDS["fixture_names"]] + [
        consumer[key] for consumer in IDS["consumers"].values() for key in per_consumer_keys
    ]
    assert len(every_id) == len(set(every_id))
    endpoint = IDS["endpoint"]
    assert (endpoint["service_id"], endpoint["method"], endpoint["path"]) == (SERVICE_ID, METHOD, PATH)
    assert endpoint["baseline_endpoint_id"] == endpoint_id(IDS["baseline_contract_snapshot_id"], SERVICE_ID, METHOD, PATH)
    assert endpoint["proposed_endpoint_id"] == endpoint_id(IDS["proposed_contract_snapshot_id"], SERVICE_ID, METHOD, PATH)
    assert "not a Shockwave customer" in IDS["provider_note"]


def test_every_dependency_validates_and_is_written_out_in_full() -> None:
    strict = load_strict_contracts()
    for handle, record in RECORDS.items():
        dependency = Dependency.model_validate(record)
        # Explicit nulls and no extras: the record is exactly what the contract model serializes.
        assert dependency.model_dump(mode="json") == record, handle
        strict.Dependency.model_validate(record)
    assert sorted(ANNOTATIONS) == sorted(RECORDS)


def test_ids_ownership_and_endpoint_follow_the_documented_derivations() -> None:
    expected_endpoint = endpoint_id(IDS["baseline_contract_snapshot_id"], SERVICE_ID, METHOD, PATH)
    assert FIXTURE["endpoint"]["endpoint_id"] == expected_endpoint == IDS["endpoint"]["baseline_endpoint_id"]
    assert FIXTURE["endpoint"]["catalog_snapshot_id"] == IDS["baseline_contract_snapshot_id"]
    sites: Counter[tuple[str, str, str]] = Counter()
    for handle, record in RECORDS.items():
        repository = repository_of(handle)
        consumer = IDS["consumers"][repository]
        usage = record["evidence"][0]
        site = f"{usage['file']}:{usage['line_start']}"
        assert ANNOTATIONS[handle]["usage_site"] == site
        assert record["id"] == fixture_uuid(f"stripe-demo/dependency/{repository}/{record['field_path']}@{site}")
        assert record["consumer_org_id"] == consumer["consumer_org_id"], handle
        assert record["enrollment_id"] == consumer["enrollment_id"], handle
        assert record["repository_snapshot_id"] == consumer["repository_snapshot_id"], handle
        assert record["language"] == consumer["language"], handle
        assert record["service_id"] == SERVICE_ID
        assert record["endpoint_id"] == expected_endpoint, handle
        for item in record["evidence"]:
            assert item["id"] == evidence_id(
                "stripe-demo/consumer-source", consumer["repository_snapshot_id"], item["file"], item["line_start"],
                item["line_end"],
            ), (handle, item["file"], item["line_start"])
        sites[(repository, record["field_path"], site)] += 1
    assert max(sites.values()) == 1, "one record per (repository, response field, usage site)"
    assert len({record["id"] for record in RECORDS.values()}) == len(RECORDS)


# Evidence against the templates ------------------------------------------------------------------


@pytest.mark.parametrize("handle", sorted(RECORDS))
def test_evidence_excerpts_are_the_actual_template_lines(handle: str) -> None:
    repository = repository_of(handle)
    for item in RECORDS[handle]["evidence"]:
        location = f"{repository}:{item['file']}:{item['line_start']}"
        assert item["kind"] == "source" and item["visibility"] == "consumer_private", location
        assert item["source_uri"] == f"repo://{repository}", location
        assert item["revision"] == PLACEHOLDER.format(repository=repository), location
        assert item["json_pointer"] is None, location
        assert not item["file"].startswith("/") and ".." not in item["file"].split("/"), location
        assert item["line_end"] == item["line_start"], location
        lines = template_lines(repository, item["file"])
        assert 1 <= item["line_start"] <= len(lines), location
        assert item["excerpt"], location
        assert item["excerpt"] == lines[item["line_start"] - 1].strip(), f"template drift at {location}"


@pytest.mark.parametrize("handle", sorted(RECORDS))
def test_evidence_traces_import_client_call_and_usage(handle: str) -> None:
    record = RECORDS[handle]
    roles = ANNOTATIONS[handle]["evidence_roles"]
    assert roles[0] == "usage", "the usage line must come first (analyzer explanations cite it)"
    assert {"sdk_call", "client", "client_initialization", "sdk_import"} <= set(roles)
    items = evidence_by_role(handle)
    assert source_location(Dependency.model_validate(record)) == ANNOTATIONS[handle]["usage_site"]
    assert record["origin"] == {"kind": "sdk_response", "library": "stripe", "call": record["origin"]["call"]}
    assert SDK_CALLS[record["origin"]["call"]] in items["sdk_call"]["excerpt"]
    assert SDK_IMPORT.match(items["sdk_import"]["excerpt"]), items["sdk_import"]
    assert CLIENT_INITIALIZATION.match(items["client_initialization"]["excerpt"]), items["client_initialization"]
    assert "GET /v1/payment_intents/{intent}" in ANNOTATIONS[handle]["sdk_operation"]
    # The SDK call, its client and the usage sit in one module, in that order.
    assert items["usage"]["file"] == items["sdk_call"]["file"] == items["client"]["file"]
    assert items["client"]["line_start"] < items["sdk_call"]["line_start"] < items["usage"]["line_start"]
    # The usage reads the field from the very variable the SDK call assigned.
    assigned = re.match(r"(?:const\s+)?(\w+)\s*=", items["sdk_call"]["excerpt"])
    assert assigned is not None
    leaf = record["field_path"].removeprefix("response.")
    access = f"{assigned.group(1)}.{leaf}"
    usage = record["usage"]
    line = items["usage"]["excerpt"]
    assert re.search(re.escape(access) + r"\b", line), (access, line)
    if usage["operation"] in ("divide", "compare"):
        # The literal operand exactly (no longer number such as 1000 for 100, or 500000 for 50000).
        symbol = "/" if usage["operation"] == "divide" else ">"
        assert re.search(re.escape(f"{access} {symbol} {usage['operand']}") + r"(?![\w.])", line), (usage, line)
    elif usage["operation"] == "store":
        assert_store_destination(items, access, usage["destination"])
    else:
        assert usage == {"operation": "read", "operand": None, "destination": None}
    assert ANNOTATIONS[handle]["confidence_basis"].startswith(f"{record['confidence']}:")


def assert_store_destination(items: dict[str, dict], access: str, destination: str) -> None:
    """The parameter tuple binds ``access`` to the column ``destination`` names, by position."""
    insert = re.fullmatch(r'"INSERT OR REPLACE INTO (\w+) \(([^)]*)\) VALUES \(([^)]*)\)",', items["destination"]["excerpt"])
    assert insert is not None, items["destination"]
    table, columns = insert.group(1), [column.strip() for column in insert.group(2).split(",")]
    parameters = [value.strip() for value in items["usage"]["excerpt"].strip("(),").split(",")]
    assert len(parameters) == len(columns) == insert.group(3).count("?")
    assert destination == f"sqlite:{table}.{columns[parameters.index(access)]}"


def test_storage_evidence_shows_the_destination_unit() -> None:
    items = evidence_by_role("reporting_ledger_amount_store")
    assert items["destination_schema"]["excerpt"].startswith("amount_minor INTEGER")
    assert "smallest currency unit" in items["destination_schema"]["excerpt"]


# Negative controls -------------------------------------------------------------------------------


@pytest.mark.parametrize("control", CONTROLS, ids=[control["id"] for control in CONTROLS])
def test_negative_control_lines_exist_and_no_dependency_points_at_them(control: dict) -> None:
    repository = control["repository"]
    lines = template_lines(repository, control["file"])
    assert lines[control["line"] - 1].strip() == control["excerpt"], f"template drift at {control['file']}"
    for related in control["related_lines"]:
        assert lines[related["line"] - 1].strip() == related["excerpt"]
    assert "amount" in control["excerpt"] or "/ 100" in control["excerpt"]
    source = (template_root(repository) / control["file"]).read_text(encoding="utf-8")
    assert not CONTROL_FILE_STRIPE_IMPORT.search(source), "a negative control's module must not use the Stripe SDK"
    control_lines = {control["line"], *(related["line"] for related in control["related_lines"])}
    for handle, record in RECORDS.items():
        if repository_of(handle) != repository:
            continue
        for item in record["evidence"]:
            assert item["file"] != control["file"], f"{handle} has evidence in negative-control file {control['file']}"
            assert (item["file"], item["line_start"]) not in {(control["file"], line) for line in control_lines}


def test_negative_controls_cover_the_required_local_values() -> None:
    assert {(control["repository"], control["file"], control["symbol"]) for control in CONTROLS} == {
        ("shockwave-stripe-checkout", "src/tax.ts", "basisPointsToPercent(basisPoints)"),
        ("shockwave-stripe-checkout", "src/rewards.ts", "LoyaltyReward.amount"),
        ("shockwave-stripe-fraud", "fraud_service/velocity.py", "VelocityLimit.amount"),
        ("shockwave-stripe-reporting", "reporting_app/budgets.py", "Budget.amount"),
    }
    assert all(control["reason"] for control in CONTROLS)


# Analysis of the simulated change ----------------------------------------------------------------


@pytest.fixture(scope="module")
def baseline_endpoint() -> Endpoint:
    """GET /v1/payment_intents/{intent} as discovered from the pinned, unmodified Stripe contract."""
    request = DiscoveryInput(
        service_id=SERVICE_ID,
        snapshot_id=IDS["baseline_contract_snapshot_id"],
        openapi_documents=[STRIPE_DOCUMENT_REF],
        source_snapshot_id=None,  # public contract only; Stripe provider source is unavailable
        frameworks=[],
    )
    result = discover_endpoints(request, {STRIPE_DOCUMENT_REF: load_pinned_document(STRIPE_PIN_PATH)}, None)
    matches = [item for item in result.endpoints if (item.method, item.path) == (METHOD, PATH)]
    assert len(matches) == 1
    return matches[0]


def simulated_change(endpoint: Endpoint) -> APIChange:
    """The units_changed APIChange the manifest describes (SIMULATED; not announced by Stripe)."""
    manifest = read_json(MANIFEST_PATH)
    baseline, proposed = IDS["baseline_contract_snapshot_id"], IDS["proposed_contract_snapshot_id"]
    declared = Evidence(
        id=evidence_id("stripe-demo/simulated-change-manifest", manifest["manifest_id"]),
        kind="declared_change",
        visibility="provider_owned",
        source_uri=MANIFEST_RELATIVE,
        revision=f"sha256:{hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()}",
        file=None,
        line_start=None,
        line_end=None,
        json_pointer="",
        excerpt=(
            f"SIMULATED, not announced by Stripe: {METHOD} {PATH} {manifest['field_path']}: "
            f"{manifest['old_semantics']} -> {manifest['new_semantics']}"
        ),
    )
    operation_evidence = [item for item in endpoint.evidence if item.kind == "openapi"]
    return APIChange(
        id=change_id(baseline, proposed, SERVICE_ID, METHOD, PATH, manifest["field_path"], manifest["kind"]),
        api_project_id=IDS["api_project_id"],
        service_id=manifest["service_id"],
        endpoint_id=endpoint.id,
        old_snapshot_id=baseline,
        new_snapshot_id=proposed,
        field_path=manifest["field_path"],
        kind=manifest["kind"],
        old_semantics=manifest["old_semantics"],
        new_semantics=manifest["new_semantics"],
        structural_breaking=manifest["structural_schema"]["changed"],
        evidence=[declared, *operation_evidence],
    )


def test_endpoint_id_is_the_pinned_baseline_record(baseline_endpoint: Endpoint) -> None:
    assert str(baseline_endpoint.id) == endpoint_id(IDS["baseline_contract_snapshot_id"], SERVICE_ID, METHOD, PATH)
    assert str(baseline_endpoint.id) == IDS["endpoint"]["baseline_endpoint_id"]
    assert baseline_endpoint.operation_id == IDS["endpoint"]["operation_id"] == "GetPaymentIntentsIntent"
    assert {record["endpoint_id"] for record in RECORDS.values()} == {str(baseline_endpoint.id)}
    # The published structural type stays integer: a units change is invisible to a schema diff.
    assert baseline_endpoint.response_schema["properties"]["amount"]["type"] == "integer"


def test_analyze_impact_predicts_the_three_amount_risks_and_leaves_other_fields_out(
    baseline_endpoint: Endpoint,
) -> None:
    manifest = read_json(MANIFEST_PATH)
    assert (manifest["provider"], manifest["change_status"], manifest["announced_by_provider"]) == (
        "Stripe",
        "simulated",
        False,
    )
    assert (manifest["endpoint"]["method"], manifest["endpoint"]["path"], manifest["kind"]) == (
        METHOD,
        PATH,
        "units_changed",
    )
    change = simulated_change(baseline_endpoint)
    records = dependencies()
    result = analyze_impact(change, records, endpoints=[baseline_endpoint])

    assert result.analysis_mode == "rules"
    assert validate_risk_evidence(result, change, records, endpoints=[baseline_endpoint]) == []
    handle_of = {record["id"]: handle for handle, record in RECORDS.items()}
    risks = {
        handle_of[str(risk.dependency_ids[0])]: risk for risk in result.risks if len(risk.dependency_ids) == 1
    }
    assert len(result.risks) == len(risks) == len(AFFECTED), [
        sorted(handle_of[str(item)] for item in risk.dependency_ids) for risk in result.risks
    ]
    for handle, (category, severity, confidence) in AFFECTED.items():
        risk = risks[handle]
        assert risk_category(change, risk) == category, handle
        assert risk.severity == severity, handle
        if confidence is not None:
            assert risk.confidence == confidence, handle
        consumer = IDS["consumers"][repository_of(handle)]
        assert (str(risk.consumer_org_id), str(risk.enrollment_id), str(risk.repository_snapshot_id)) == (
            consumer["consumer_org_id"],
            consumer["enrollment_id"],
            consumer["repository_snapshot_id"],
        )
    assert len({str(risk.consumer_org_id) for risk in result.risks}) == 3
    # Unaffected: the change's field is response.amount, so these usages are neither risks nor unresolved.
    in_risks = {str(item) for risk in result.risks for item in risk.dependency_ids}
    unresolved = {str(item) for item in result.unresolved_dependency_ids}
    for handle in UNAFFECTED:
        assert RECORDS[handle]["id"] not in in_risks | unresolved, handle
    assert unresolved == set()
    assert sorted([*AFFECTED, *UNAFFECTED]) == sorted(RECORDS)
