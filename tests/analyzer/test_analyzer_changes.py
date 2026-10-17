"""Acceptance tests for analyzer.changes: structural detection and declared semantic changes.

DiscoveryResults are built by hand in the Endpoint shape discovery produces (expanded
schemas with ``x-shockwave-*`` annotations, operation-level openapi evidence), so these
tests do not depend on the discovery parser.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from analyzer.changes import (
    ENDPOINT_REMOVED_NEW,
    ENDPOINT_REMOVED_OLD,
    REFERENCE_NOT_COMPARED,
    InvalidDeclaredChange,
    ProposedContractIncomplete,
    detect_changes,
    validate_declared_changes,
)
from discovery.contracts import (
    APIChange,
    Dependency,
    DiscoveryResult,
    Endpoint,
    Evidence,
)
from discovery.ids import change_id, endpoint_id, evidence_id, fixture_uuid
from discovery.records import (
    PATTERN_NO_DOCUMENTS,
    PATTERN_UNPARSEABLE,
    PATTERN_UNREAD_DOCUMENT,
    PATTERN_UNRESOLVED_PATH_ITEM,
    REF_DIGEST_KEY,
    operation_pointer,
)
from discovery.text import has_unsafe_characters
from verification.scenarios.catalog import SCENARIO_URI_PREFIX, find_applicable_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "provider" / "demo_payments"
DECLARED_FILE = DEMO_DIR / "changes" / "charge_amount_cents_to_dollars.json"
FIXTURE_IDS = json.loads((DEMO_DIR / "fixture_ids.json").read_text(encoding="utf-8"))
DEPENDENCIES = json.loads(
    (REPO_ROOT / "tests" / "analyzer" / "fixtures" / "demo_dependencies.json").read_text(
        encoding="utf-8"
    )
)["dependencies"]

SERVICE_ID = FIXTURE_IDS["service_id"]
PROJECT_ID = FIXTURE_IDS["api_project_id"]
OTHER_PROJECT_ID = fixture_uuid("test.analyzer-changes.other-project")
V1 = FIXTURE_IDS["contract_snapshots"]["v1"]
V2 = FIXTURE_IDS["contract_snapshots"]["v2_proposed"]
SOURCE_SNAPSHOT = FIXTURE_IDS["source_snapshot_id"]
SPEC_URIS = {
    V1: "examples/provider/demo_payments/openapi/demo-payments.v1.yaml",
    V2: "examples/provider/demo_payments/openapi/demo-payments.v2.yaml",
}
SPEC_SHA = {
    snapshot: hashlib.sha256((REPO_ROOT / uri).read_bytes()).hexdigest()
    for snapshot, uri in SPEC_URIS.items()
}
CHARGE_PATH = "/v1/charges/{id}"
REFUND_PATH = "/v1/refunds/{id}"
RECEIPT_PATH = "/v1/charges/{id}/receipt"
CHARGE_POINTER = "/paths/~1v1~1charges~1{id}/get"
AMOUNT_POINTER = "/components/schemas/Charge/properties/amount"
CUSTOMER_REF = "#/components/schemas/Customer"

Edit = Callable[[dict], None]


# Hand-built discovery results ---------------------------------------------------------


def charge_schema() -> dict:
    return {
        "type": "object",
        "required": ["id", "amount", "currency"],
        "properties": {
            "id": {"type": "string"},
            "amount": {"type": "number", "description": "Charge amount."},
            "currency": {
                "type": "string",
                "description": "Three-letter ISO currency code, lowercase.",
            },
        },
        "x-shockwave-origin": "/components/schemas/Charge",
    }


def refund_schema() -> dict:
    return {
        "type": "object",
        "required": ["id", "charge", "amount", "currency"],
        "properties": {
            "id": {"type": "string"},
            "charge": {"type": "string"},
            "amount": {"type": "number", "description": "Refund amount."},
            "currency": {"type": "string"},
        },
        "x-shockwave-origin": "/components/schemas/Refund",
    }


def json_response(schema: dict) -> dict:
    return {**schema, "x-shockwave-status": "200", "x-shockwave-content-type": "application/json"}


def id_request(*, query: dict | None = None, query_required: tuple[str, ...] = ()) -> dict:
    schema: dict = {
        "type": "object",
        "properties": {
            "path": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
        },
        "required": ["path"],
    }
    if query is not None:
        container: dict = {"type": "object", "properties": query}
        if query_required:
            container["required"] = list(query_required)
            schema["required"].append("query")
        schema["properties"]["query"] = container
    return schema


def openapi_evidence(snapshot: str, method: str, path: str, summary: str) -> Evidence:
    sha = SPEC_SHA[snapshot]
    pointer = operation_pointer(path, method)
    return Evidence(
        id=evidence_id("openapi", sha, pointer),
        kind="openapi",
        visibility="provider_owned",
        source_uri=SPEC_URIS[snapshot],
        revision=f"sha256:{sha}",
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=pointer,
        excerpt=f"{method} {path} - {summary}",
    )


def documented(
    snapshot: str,
    method: str,
    path: str,
    summary: str,
    *,
    response: dict,
    request: dict | None = None,
    reconciliation: str = "documented_and_found",
    service_id: str = SERVICE_ID,
) -> Endpoint:
    return Endpoint(
        id=endpoint_id(snapshot, service_id, method, path),
        service_id=service_id,
        method=method,
        path=path,
        snapshot_id=snapshot,
        operation_id=None,
        request_schema=request if request is not None else id_request(),
        response_schema=response,
        evidence=[openapi_evidence(snapshot, method, path, summary)],
        reconciliation=reconciliation,
        confidence="direct",
    )


def receipt_route(snapshot: str) -> Endpoint:
    """The implementation-only route: source evidence, no schema."""
    source = "demo_payments/charges.py"
    lines = (DEMO_DIR / "service" / source).read_text(encoding="utf-8").splitlines()
    line = next(number for number, text in enumerate(lines, start=1) if "/{id}/receipt" in text)
    return Endpoint(
        id=endpoint_id(snapshot, SERVICE_ID, "GET", RECEIPT_PATH),
        service_id=SERVICE_ID,
        method="GET",
        path=RECEIPT_PATH,
        snapshot_id=snapshot,
        operation_id=None,
        request_schema={},
        response_schema={},
        evidence=[
            Evidence(
                id=evidence_id("source", SOURCE_SNAPSHOT, source, line),
                kind="source",
                visibility="provider_owned",
                source_uri=f"provider-source://{SOURCE_SNAPSHOT}/{source}",
                revision=SOURCE_SNAPSHOT,
                file=source,
                line_start=line,
                line_end=line,
                json_pointer=None,
                excerpt=lines[line - 1].strip(),
            )
        ],
        reconciliation="implementation_only",
        confidence="direct",
    )


def source_only(endpoint: Endpoint) -> Endpoint:
    """``endpoint`` as a scan finds it when no document describes it: source evidence only."""
    return endpoint.model_copy(
        update={
            "evidence": receipt_route(str(endpoint.snapshot_id)).evidence,
            "request_schema": {},
            "response_schema": {},
            "reconciliation": "not_compared",
        }
    )


def with_gaps(
    result: DiscoveryResult, *patterns: str, endpoints: list[Endpoint] | None = None
) -> DiscoveryResult:
    """``result`` as discovery reports it when its coverage lists the gap ``patterns``."""
    return DiscoveryResult(
        schema_version="shockwave.v1",
        service_id=result.service_id,
        snapshot_id=result.snapshot_id,
        endpoints=result.endpoints if endpoints is None else endpoints,
        coverage={"files_scanned": 6, "files_skipped": 0, "unsupported_patterns": list(patterns)},
        warnings=[],
    )


def demo_result(
    snapshot: str,
    *,
    charge: dict | None = None,
    charge_request: dict | None = None,
    refund: bool = True,
    receipt: bool = True,
    extra: tuple[Endpoint, ...] | list[Endpoint] = (),
) -> DiscoveryResult:
    endpoints = [
        documented(
            snapshot,
            "GET",
            CHARGE_PATH,
            "Retrieve a charge",
            response=charge if charge is not None else json_response(charge_schema()),
            request=charge_request,
        )
    ]
    if refund:
        endpoints.append(
            documented(
                snapshot,
                "GET",
                REFUND_PATH,
                "Retrieve a refund",
                response=json_response(refund_schema()),
                reconciliation="documented_not_found_in_scan",
            )
        )
    if receipt:
        endpoints.append(receipt_route(snapshot))
    endpoints.extend(extra)
    return DiscoveryResult(
        schema_version="shockwave.v1",
        service_id=SERVICE_ID,
        snapshot_id=snapshot,
        endpoints=endpoints,
        coverage={"files_scanned": 6, "files_skipped": 0, "unsupported_patterns": []},
        warnings=[],
    )


def charge_pair(
    old_edit: Edit | None = None, new_edit: Edit | None = None
) -> tuple[DiscoveryResult, DiscoveryResult]:
    old_charge, new_charge = json_response(charge_schema()), json_response(charge_schema())
    if old_edit is not None:
        old_edit(old_charge)
    if new_edit is not None:
        new_edit(new_charge)
    return demo_result(V1, charge=old_charge), demo_result(V2, charge=new_charge)


def list_endpoint(snapshot: str, items: dict) -> Endpoint:
    response = json_response(
        {
            "type": "object",
            "required": ["object", "data", "has_more"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": items},
                "has_more": {"type": "boolean"},
            },
        }
    )
    return documented(
        snapshot,
        "GET",
        "/v1/charges",
        "List charges",
        response=response,
        request={"type": "object"},
    )


def create_refund_endpoint(snapshot: str, body: dict, *, body_required: bool) -> Endpoint:
    request = {
        "type": "object",
        "properties": {"body": body},
        "required": ["body"] if body_required else [],
        "x-shockwave-content-type": "application/json",
    }
    return documented(
        snapshot,
        "POST",
        "/v1/refunds",
        "Create a refund",
        response=json_response(refund_schema()),
        request=request,
    )


def refund_body(*, required: tuple[str, ...] = ("charge",), **amount: object) -> dict:
    properties: dict = {
        "charge": {"type": "string"},
        "amount": {"type": "number", **amount},
        "reason": {"type": "string"},
    }
    return {
        "type": "object",
        "required": list(required),
        "properties": properties,
        "x-shockwave-origin": "/components/schemas/RefundCreate",
    }


def summary(changes: list[APIChange]) -> list[tuple[str, str, bool]]:
    return [(change.field_path, change.kind, change.structural_breaking) for change in changes]


def declared_change() -> APIChange:
    return APIChange.model_validate_json(DECLARED_FILE.read_text(encoding="utf-8"))


def docs_evidence(name: str, excerpt: str) -> Evidence:
    return Evidence(
        id=fixture_uuid(f"test.analyzer-changes.docs.{name}"),
        kind="docs",
        visibility="provider_owned",
        source_uri=f"https://demo-payments.test/changelog#{name}",
        revision="changelog@2026-10-01",
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=None,
        excerpt=excerpt,
    )


def make_declared(
    kind: str,
    field_path: str,
    *,
    path: str = CHARGE_PATH,
    evidence: list[Evidence] | None = None,
    old_semantics: str = "",
    new_semantics: str = "",
    api_project_id: str = PROJECT_ID,
) -> APIChange:
    return APIChange(
        id=fixture_uuid(f"test.analyzer-changes.declared.{path}.{field_path}.{kind}"),
        api_project_id=api_project_id,
        service_id=SERVICE_ID,
        endpoint_id=endpoint_id(V1, SERVICE_ID, "GET", path),
        old_snapshot_id=V1,
        new_snapshot_id=V2,
        field_path=field_path,
        kind=kind,
        old_semantics=old_semantics,
        new_semantics=new_semantics,
        structural_breaking=False,
        evidence=evidence or [],
    )


# Schema edits ---------------------------------------------------------------------------


def set_field(name: str, **keywords: object) -> Edit:
    def edit(schema: dict) -> None:
        schema["properties"][name].update(keywords)

    return edit


def remove_currency(schema: dict) -> None:
    del schema["properties"]["currency"]
    schema["required"].remove("currency")


def currency_optional(schema: dict) -> None:
    schema["required"].remove("currency")


def with_status(*values: str) -> Edit:
    def edit(schema: dict) -> None:
        schema["properties"]["status"] = {"type": "string", "enum": list(values)}

    return edit


def customer_marker(target: str, digest: str | None) -> dict:
    """A preserved $ref, with discovery's fingerprint of its target when ``digest`` is set."""
    marker = {"$ref": target, "x-shockwave-ref": "depth_limit"}
    if digest is not None:
        marker[REF_DIGEST_KEY] = digest
    return marker


def with_customer_ref(target: str, digest: str | None = None) -> Edit:
    def edit(schema: dict) -> None:
        schema["properties"]["customer"] = customer_marker(target, digest)

    return edit


def inlined_customer(digest: str | None = None) -> Edit:
    def edit(schema: dict) -> None:
        schema["properties"]["customer"] = {
            "type": "object",
            "properties": {"id": {"type": "string"}, "balance": {"type": "integer"}},
            "x-shockwave-origin": "/components/schemas/Customer",
        }
        if digest is not None:
            schema["properties"]["customer"][REF_DIGEST_KEY] = digest

    return edit


def with_inlined_customer(schema: dict) -> None:
    inlined_customer()(schema)


def with_metadata(schema: dict) -> None:
    schema["properties"]["metadata"] = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
    }


def with_composed_customer(*visible: str, digest: str | None = None) -> Edit:
    """customer = allOf[expanded part with ``visible`` properties, unexpanded $ref]."""

    def edit(schema: dict) -> None:
        schema["properties"]["customer"] = {
            "allOf": [
                {"type": "object", "properties": {name: {"type": "string"} for name in visible}},
                customer_marker("#/components/schemas/CustomerBase", digest),
            ]
        }

    return edit


def amount_accepts_anything(schema: dict) -> None:
    schema["properties"]["amount"] = True  # JSON Schema boolean subschema (OpenAPI 3.1)


def remove_amount(schema: dict) -> None:
    del schema["properties"]["amount"]
    schema["required"].remove("amount")


def describe_amount(text: str) -> Edit:
    return set_field("amount", description=text)


def reword_documentation(schema: dict) -> None:
    schema["description"] = "A charge (reworded for v2)."
    schema["properties"]["amount"].update(description="Charge amount, see docs.", example=10)
    schema["properties"]["currency"]["title"] = "Currency"
    schema["x-shockwave-origin"] = "/components/schemas/ChargeV2"
    schema["x-shockwave-status"] = "201"


# Demo acceptance ------------------------------------------------------------------------


def test_demo_schema_comparison_alone_reports_no_change() -> None:
    old, new = demo_result(V1), demo_result(V2)
    assert detect_changes(old, new, []) == []
    assert detect_changes(old, new, [], api_project_id=PROJECT_ID) == []


def test_declared_units_change_is_the_only_change_and_reaches_the_display_scenario() -> None:
    old, new = demo_result(V1), demo_result(V2)
    declared = declared_change()
    assert validate_declared_changes(old, new, [declared]) == []

    [change] = detect_changes(old, new, [declared])

    assert change.kind == "units_changed"
    assert change.structural_breaking is False
    assert change.field_path == "response.amount"
    assert change.id == declared.id
    assert change.endpoint_id == endpoint_id(V1, SERVICE_ID, "GET", CHARGE_PATH)
    assert change.endpoint_id == DEPENDENCIES["python_display"]["endpoint_id"]
    assert (change.old_snapshot_id, change.new_snapshot_id) == (V1, V2)
    assert (change.old_semantics, change.new_semantics) == (
        declared.old_semantics,
        declared.new_semantics,
    )
    declared_items = [item.model_dump(mode="json") for item in declared.evidence]
    assert [item.model_dump(mode="json") for item in change.evidence[: len(declared_items)]] == (
        declared_items
    )
    operation = old.endpoints[0].evidence[0]
    assert operation.json_pointer == CHARGE_POINTER
    assert change.evidence[len(declared_items) :] == [operation]

    python = find_applicable_scenario(
        change, Dependency.model_validate(DEPENDENCIES["python_display"])
    )
    assert python is not None
    assert python.scenario_id == "demo-payments.charge-amount-units.display.python"
    typescript = find_applicable_scenario(
        change, Dependency.model_validate(DEPENDENCIES["typescript_display"])
    )
    assert typescript is not None
    assert typescript.scenario_id == "demo-payments.charge-amount-units.display.typescript"
    stripe = Dependency.model_validate(DEPENDENCIES["negative_stripe_service"])
    assert find_applicable_scenario(change, stripe) is None


def test_units_described_only_in_descriptions_are_never_inferred() -> None:
    old, new = charge_pair(
        describe_amount("Charge amount in cents (minor units)."),
        describe_amount("Charge amount in dollars (major units)."),
    )
    assert detect_changes(old, new, [], api_project_id=PROJECT_ID) == []


# Declared change validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("update", "expected_problem"),
    [
        ({"evidence": []}, "units_changed requires explicit evidence"),
        ({"field_path": "response.amount_cents"}, "is not in the current contract schema"),
        ({"field_path": "amount"}, "is not an API field path"),
        ({"field_path": "*"}, "only valid for behavior_changed"),
        (
            {"old_snapshot_id": FIXTURE_IDS["stripe"]["contract_snapshot_id"]},
            "is not the current contract snapshot",
        ),
        ({"new_snapshot_id": V1}, "is not the proposed contract snapshot"),
        ({"service_id": "stripe"}, "does not match the compared service 'demo-payments'"),
        (
            {"endpoint_id": endpoint_id(V2, SERVICE_ID, "GET", CHARGE_PATH)},
            "is not an endpoint of the current contract snapshot",
        ),
        ({"old_semantics": "   "}, "requires non-empty old_semantics and new_semantics"),
        (
            {"new_semantics": "USD amount in minor currency units (cents): 1000 represents $10.00"},
            "requires old_semantics and new_semantics to differ",
        ),
    ],
    ids=[
        "no-evidence",
        "unknown-field",
        "local-name",
        "endpoint-wide-units",
        "wrong-old-snapshot",
        "wrong-new-snapshot",
        "stripe-service",
        "new-snapshot-record-id",
        "empty-semantics",
        "same-semantics",
    ],
)
def test_invalid_declared_change_is_rejected(update: dict, expected_problem: str) -> None:
    old, new = demo_result(V1), demo_result(V2)
    invalid = declared_change().model_copy(update=update)

    problems = validate_declared_changes(old, new, [invalid])

    assert any(expected_problem in problem for problem in problems), problems
    with pytest.raises(InvalidDeclaredChange) as raised:
        detect_changes(old, new, [invalid])
    assert raised.value.problems == problems
    assert isinstance(raised.value, ValueError)


def test_openapi_evidence_alone_does_not_establish_a_units_change() -> None:
    old, new = demo_result(V1), demo_result(V2)
    operation = old.endpoints[0].evidence[0]
    invalid = declared_change().model_copy(update={"evidence": [operation]})
    with pytest.raises(InvalidDeclaredChange, match="requires explicit evidence"):
        detect_changes(old, new, [invalid])


def test_every_problem_is_reported_not_just_the_first() -> None:
    old, new = demo_result(V1), demo_result(V2)
    invalid = declared_change().model_copy(update={"evidence": [], "new_snapshot_id": V1})
    problems = validate_declared_changes(old, new, [invalid])
    assert len(problems) == 2
    assert all(problem.startswith(f"declared change 0 ({invalid.id}): ") for problem in problems)


def test_declared_field_behind_an_unexpanded_ref_cannot_be_verified() -> None:
    old, new = charge_pair(with_customer_ref(CUSTOMER_REF), with_customer_ref(CUSTOMER_REF))
    invalid = declared_change().model_copy(update={"field_path": "response.customer.balance"})
    with pytest.raises(InvalidDeclaredChange) as raised:
        detect_changes(old, new, [invalid])
    assert any(
        problem.split(": ", 1)[1].startswith("cannot verify field: schema not expanded")
        for problem in raised.value.problems
    ), raised.value.problems


def test_declared_field_on_a_source_only_endpoint_is_unknown_not_absent() -> None:
    note = {"x-shockwave-schema": "unknown: implementation-only route has no documented schema"}

    def noted(snapshot: str) -> Endpoint:
        return receipt_route(snapshot).model_copy(
            update={"request_schema": {"type": "object", "properties": {}, **note}, "response_schema": dict(note)}
        )

    old = demo_result(V1, receipt=False, extra=[noted(V1)])
    new = demo_result(V2, receipt=False, extra=[noted(V2)])
    declared = make_declared(
        "units_changed",
        "response.amount",
        path=RECEIPT_PATH,
        evidence=[docs_evidence("receipt-units", "receipt amounts move to dollars")],
        old_semantics="USD amount in cents",
        new_semantics="USD amount in dollars",
    )
    problems = [problem.split(": ", 1)[1] for problem in validate_declared_changes(old, new, [declared])]
    assert problems, "an undocumented field cannot be checked"
    assert all(problem.startswith("cannot verify field: GET /v1/charges/{id}/receipt has no documented") for problem in problems)
    assert not any("is not in the" in problem for problem in problems)


def test_submitted_structural_breaking_is_recomputed_for_an_unchanged_field() -> None:
    old, new = demo_result(V1), demo_result(V2)
    submitted = declared_change().model_copy(update={"structural_breaking": True})
    [change] = detect_changes(old, new, [submitted])
    assert change.structural_breaking is False


def test_declared_change_on_a_structurally_broken_field_is_marked_breaking() -> None:
    old, new = charge_pair(None, set_field("amount", type="string"))
    declared = declared_change()

    changes = detect_changes(old, new, [declared])

    assert summary(changes) == [
        ("response.amount", "type_changed", True),
        ("response.amount", "units_changed", True),
    ]
    assert changes[1].id == declared.id


def test_declared_and_structural_change_for_the_same_field_and_kind_merge() -> None:
    old, new = charge_pair(None, set_field("amount", type="string"))
    note = docs_evidence("amount-string", "amount is serialized as a decimal string in v2")
    submitted = make_declared(
        "type_changed",
        "response.amount",
        evidence=[note],
        old_semantics="decimal number",
        new_semantics="decimal string",
    )

    [change] = detect_changes(old, new, [submitted])

    assert change.id == submitted.id
    assert change.structural_breaking is True
    assert (change.old_semantics, change.new_semantics) == (
        "decimal number; structural: type: number",
        "decimal string; structural: type: string",
    )
    assert change.evidence[0] == note
    assert [item.json_pointer for item in change.evidence[1:]] == [
        CHARGE_POINTER,
        CHARGE_POINTER,
        AMOUNT_POINTER,
        AMOUNT_POINTER,
    ]


def test_merged_declared_change_keeps_the_structural_observation_it_is_flagged_for() -> None:
    old, new = charge_pair(None, currency_optional)
    note = docs_evidence("currency-omitted", "currency is omitted for USD charges in v2")
    submitted = make_declared(
        "behavior_changed",
        "response.currency",
        evidence=[note],
        old_semantics="currency is always sent",
        new_semantics="currency is omitted for USD charges",
    )

    [change] = detect_changes(old, new, [submitted])

    assert change.id == submitted.id
    assert summary([change]) == [("response.currency", "behavior_changed", True)]
    assert (change.old_semantics, change.new_semantics) == (
        "currency is always sent; structural: always present (required)",
        "currency is omitted for USD charges; structural: may be absent (optional)",
    )

    restated = submitted.model_copy(
        update={
            "old_semantics": "always present (required)",
            "new_semantics": "may be absent (optional) for USD charges",
        }
    )
    [change] = detect_changes(old, new, [restated])
    assert (change.old_semantics, change.new_semantics) == (
        "always present (required)",
        "may be absent (optional) for USD charges",
    )


def test_declared_structural_kind_without_support_is_rejected() -> None:
    old, new = demo_result(V1), demo_result(V2)
    unsupported = make_declared("enum_changed", "response.currency")
    with pytest.raises(InvalidDeclaredChange, match="neither found by structural comparison"):
        detect_changes(old, new, [unsupported])


def test_declared_endpoint_removal_merges_with_the_structural_removal() -> None:
    old, new = demo_result(V1), demo_result(V2, refund=False)
    note = docs_evidence("refund-retired", "GET /v1/refunds/{id} is retired in v2")
    submitted = make_declared(
        "behavior_changed",
        "*",
        path=REFUND_PATH,
        evidence=[note],
        old_semantics="refunds can be retrieved by id",
        new_semantics="refund retrieval is retired",
    )

    [change] = detect_changes(old, new, [submitted])

    assert change.id == submitted.id
    assert summary([change]) == [("*", "behavior_changed", True)]
    assert change.evidence == [note, old.endpoints[1].evidence[0]]


def test_declared_field_removal_must_match_the_proposed_contract() -> None:
    still_documented = make_declared("field_removed", "response.currency")
    old, new = demo_result(V1), demo_result(V2)
    with pytest.raises(InvalidDeclaredChange, match="proposed contract documents it"):
        detect_changes(old, new, [still_documented])

    old, new = charge_pair(None, remove_currency)
    [change] = detect_changes(old, new, [still_documented])
    assert change.id == still_documented.id
    assert summary([change]) == [("response.currency", "field_removed", True)]
    assert change.old_semantics == "documented (type: string)"  # filled from the structural diff


def test_conflicting_evidence_under_one_id_is_rejected() -> None:
    old, new = demo_result(V1), demo_result(V2)
    declared = declared_change()
    first = declared.evidence[0]
    altered = first.model_copy(update={"excerpt": "a different claim under the same id"})
    invalid = declared.model_copy(update={"evidence": [*declared.evidence, altered]})
    with pytest.raises(InvalidDeclaredChange, match=f"evidence id {first.id} is used"):
        detect_changes(old, new, [invalid])


def test_duplicate_declared_changes_are_rejected() -> None:
    old, new = demo_result(V1), demo_result(V2)
    declared = declared_change()
    renamed = declared.model_copy(update={"id": fixture_uuid("test.analyzer-changes.duplicate")})
    assert any(
        "repeats an earlier declared change" in problem
        for problem in validate_declared_changes(old, new, [declared, renamed])
    )
    assert any(
        "reuses the id of an earlier declared change" in problem
        for problem in validate_declared_changes(old, new, [declared, declared])
    )


def attached_evidence(kind: str, source_uri: str) -> Evidence:
    return Evidence(
        id=fixture_uuid(f"test.analyzer-changes.attached.{kind}.{source_uri}"),
        kind=kind,
        visibility="provider_owned",
        source_uri=source_uri,
        revision="provider-supplied",
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=None,
        excerpt="provider-attached test reference",
    )


@pytest.mark.parametrize("kind", ["test", "docs"])
def test_declared_evidence_may_not_use_the_reserved_scenario_scheme(kind: str) -> None:
    old, new = demo_result(V1), demo_result(V2)
    declared = declared_change()
    pointer = attached_evidence(
        kind, f"{SCENARIO_URI_PREFIX}demo-payments.charge-amount-units.display.python"
    )
    smuggled = declared.model_copy(update={"evidence": [*declared.evidence, pointer]})

    problems = validate_declared_changes(old, new, [smuggled])

    assert problems == [
        (
            f"declared change 0 ({declared.id}): evidence {pointer.id} uses the reserved "
            f"{SCENARIO_URI_PREFIX} scheme; scenario pointers are added by impact analysis, "
            "never declared"
        )
    ]
    with pytest.raises(InvalidDeclaredChange):
        detect_changes(old, new, [smuggled])


def test_declared_test_evidence_outside_the_reserved_scheme_is_published() -> None:
    old, new = demo_result(V1), demo_result(V2)
    declared = declared_change()
    ci_run = attached_evidence("test", "https://ci.demo-payments.test/runs/42")
    with_ci = declared.model_copy(update={"evidence": [*declared.evidence, ci_run]})

    [change] = detect_changes(old, new, [with_ci])

    assert ci_run in change.evidence


def test_declared_changes_on_boolean_subschema_fields_are_checked_like_any_field() -> None:
    # OpenAPI 3.1 allows ``amount: true`` (any value): the field exists, as the diff agrees.
    old, new = charge_pair(amount_accepts_anything, amount_accepts_anything)
    [change] = detect_changes(old, new, [declared_change()])
    assert summary([change]) == [("response.amount", "units_changed", False)]

    removal = make_declared("field_removed", "response.amount")
    old, new = charge_pair(amount_accepts_anything, remove_amount)
    assert summary(detect_changes(old, new, [], api_project_id=PROJECT_ID)) == [
        ("response.amount", "field_removed", True)
    ]
    [change] = detect_changes(old, new, [removal])
    assert (change.id, change.structural_breaking) == (removal.id, True)

    old, new = charge_pair(None, amount_accepts_anything)
    with pytest.raises(InvalidDeclaredChange, match="proposed contract documents it"):
        detect_changes(old, new, [removal])

    expand = id_request(query={"expand": True})
    note = docs_evidence("expand-default", "expand now defaults to customer")
    declared = make_declared(
        "behavior_changed",
        "request.query.expand",
        evidence=[note],
        old_semantics="expand defaults to none",
        new_semantics="expand defaults to customer",
    )
    old = demo_result(V1, charge_request=expand)
    new = demo_result(V2, charge_request=expand)
    assert validate_declared_changes(old, new, [declared]) == []


# Structural detection -------------------------------------------------------------------

RESPONSE_CASES: dict[str, tuple[Edit | None, Edit | None, list[tuple[str, str, bool]]]] = {
    "amount-number-to-string": (
        None,
        set_field("amount", type="string"),
        [("response.amount", "type_changed", True)],
    ),
    "currency-removed": (None, remove_currency, [("response.currency", "field_removed", True)]),
    "enum-gains-value": (
        with_status("pending", "succeeded"),
        with_status("pending", "succeeded", "failed"),
        [("response.status", "enum_changed", True)],
    ),
    "enum-loses-value": (
        with_status("pending", "succeeded", "failed"),
        with_status("pending", "succeeded"),
        [("response.status", "enum_changed", False)],
    ),
    "required-to-optional": (
        None,
        currency_optional,
        [("response.currency", "behavior_changed", True)],
    ),
    "nullable-added-3.0": (
        None,
        set_field("amount", nullable=True),
        [("response.amount", "type_changed", True)],
    ),
    "null-type-added-3.1": (
        None,
        set_field("amount", type=["number", "null"]),
        [("response.amount", "type_changed", True)],
    ),
    "nullable-3.0-equals-null-type-3.1": (
        set_field("amount", nullable=True),
        set_field("amount", type=["number", "null"]),
        [],
    ),
    "integer-to-number": (
        set_field("amount", type="integer"),
        None,
        [("response.amount", "type_changed", True)],
    ),
    "number-to-integer": (
        None,
        set_field("amount", type="integer"),
        [("response.amount", "type_changed", False)],
    ),
    "format-changed": (
        set_field("id", format="uuid"),
        set_field("id", format="ulid"),
        [("response.id", "type_changed", True)],
    ),
    "format-removed": (
        set_field("id", format="uuid"),
        None,
        [("response.id", "type_changed", True)],
    ),
    "format-added": (
        None,
        set_field("id", format="uuid"),
        [("response.id", "type_changed", False)],
    ),
    "description-title-example-annotation-only": (None, reword_documentation, []),
    "optional-response-field-added": (None, with_metadata, []),
    "boolean-true-subschema-is-not-a-removal": (
        None,
        amount_accepts_anything,
        [("response.amount", "type_changed", True)],
    ),
    "preserved-ref-same-target": (
        with_customer_ref(CUSTOMER_REF),
        with_customer_ref(CUSTOMER_REF),
        [],
    ),
    "preserved-ref-different-target": (
        with_customer_ref(CUSTOMER_REF),
        with_customer_ref("#/components/schemas/CustomerV2"),
        [("response.customer", "behavior_changed", False)],
    ),
    "preserved-ref-vs-inlined-same-target": (
        with_customer_ref(CUSTOMER_REF),
        with_inlined_customer,
        [],
    ),
    "preserved-ref-same-target-same-digest": (
        with_customer_ref(CUSTOMER_REF, "digest-1"),
        with_customer_ref(CUSTOMER_REF, "digest-1"),
        [],
    ),
    "preserved-ref-same-target-changed-digest-is-not-compared": (
        with_customer_ref(CUSTOMER_REF, "digest-1"),
        with_customer_ref(CUSTOMER_REF, "digest-2"),
        [("response.customer", "behavior_changed", False)],
    ),
    "preserved-ref-same-target-one-digest": (
        with_customer_ref(CUSTOMER_REF),
        with_customer_ref(CUSTOMER_REF, "digest-2"),
        [],
    ),
    "preserved-ref-vs-inlined-changed-digest-is-not-compared": (
        with_customer_ref(CUSTOMER_REF, "digest-1"),
        inlined_customer("digest-2"),
        [("response.customer", "behavior_changed", False)],
    ),
    "unexpanded-member-same-digest": (
        with_composed_customer("name", digest="digest-1"),
        with_composed_customer("name", digest="digest-1"),
        [],
    ),
    "unexpanded-member-changed-digest-is-not-compared": (
        with_composed_customer("name", digest="digest-1"),
        with_composed_customer("name", digest="digest-2"),
        [("response.customer", "behavior_changed", False)],
    ),
    "property-hidden-by-unexpanded-member-is-not-claimed-removed": (
        with_composed_customer("name"),
        with_composed_customer(),
        [("response.customer.name", "behavior_changed", False)],
    ),
    "numeric-enum-spelling-only": (
        set_field("amount", enum=[1, 2]),
        set_field("amount", enum=[1.0, 2.0]),
        [],
    ),
    "boolean-enum-value-is-not-a-number": (
        set_field("amount", enum=[1]),
        set_field("amount", enum=[True]),
        [("response.amount", "enum_changed", True)],
    ),
}


@pytest.mark.parametrize(
    ("old_edit", "new_edit", "expected"), list(RESPONSE_CASES.values()), ids=list(RESPONSE_CASES)
)
def test_structural_response_changes(
    old_edit: Edit | None, new_edit: Edit | None, expected: list[tuple[str, str, bool]]
) -> None:
    old, new = charge_pair(old_edit, new_edit)
    assert summary(detect_changes(old, new, [], api_project_id=PROJECT_ID)) == expected


@pytest.mark.parametrize(
    ("old_edit", "new_edit", "old_semantics", "new_semantics"),
    [
        (None, set_field("amount", type="string"), "type: number", "type: string"),
        (with_status("a", "b"), with_status("a", "b", "c"), "enum: [a, b]", "enum: [a, b, c]"),
        (None, currency_optional, "always present (required)", "may be absent (optional)"),
        (None, set_field("amount", nullable=True), "type: number", "type: number | null"),
        (None, remove_currency, "documented (type: string)", "absent from the proposed contract"),
    ],
    ids=["type", "enum", "required", "nullable", "removed"],
)
def test_structural_semantics_are_concise(
    old_edit: Edit | None, new_edit: Edit | None, old_semantics: str, new_semantics: str
) -> None:
    [change] = detect_changes(*charge_pair(old_edit, new_edit), [], api_project_id=PROJECT_ID)
    assert (change.old_semantics, change.new_semantics) == (old_semantics, new_semantics)


def test_document_text_reaches_semantics_and_excerpts_as_one_clean_line() -> None:
    hostile = "pend" + chr(0x202E) + "ing" + chr(0x07) + "\nnow"
    old, new = charge_pair(with_status("succeeded"), with_status("succeeded", hostile))

    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)

    assert summary([change]) == [("response.status", "enum_changed", True)]
    assert change.new_semantics == "enum: [succeeded, pend ing now]"
    texts = [change.old_semantics, change.new_semantics]
    texts.extend(item.excerpt or "" for item in change.evidence)
    assert "type: string; enum: [succeeded, pend ing now]" in texts
    assert not any(has_unsafe_characters(text) for text in texts), texts


def test_large_enum_semantics_stay_bounded() -> None:
    values = [f"country_{index:03d}" for index in range(150)]
    old, new = charge_pair(with_status(*values), with_status(*values, "country_new"))
    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)
    assert change.old_semantics == "enum: 150 values"
    assert change.new_semantics == "enum: 151 values (added: [country_new]; removed: [])"
    assert all(len(item.excerpt or "") <= 300 for item in change.evidence)


def test_changed_ref_target_says_it_was_not_structurally_compared() -> None:
    old, new = charge_pair(
        with_customer_ref(CUSTOMER_REF), with_customer_ref("#/components/schemas/CustomerV2")
    )
    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)
    assert CUSTOMER_REF in change.old_semantics
    assert "#/components/schemas/CustomerV2" in change.new_semantics
    assert "referenced schema changed and was not structurally compared" in change.new_semantics


def test_same_ref_target_with_a_changed_fingerprint_is_unknown_not_unchanged() -> None:
    old, new = charge_pair(
        with_customer_ref(CUSTOMER_REF, "digest-1"), with_customer_ref(CUSTOMER_REF, "digest-2")
    )
    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)
    assert (change.field_path, change.kind, change.structural_breaking) == (
        "response.customer",
        "behavior_changed",
        False,
    )
    assert change.old_semantics == f"references {CUSTOMER_REF} (not expanded: depth_limit)"
    assert change.new_semantics == (
        f"references {CUSTOMER_REF} (not expanded: depth_limit); {REFERENCE_NOT_COMPARED}"
    )


def test_structural_change_identity_and_evidence() -> None:
    old, new = charge_pair(None, set_field("amount", type="string"))

    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)

    assert change.id == change_id(
        V1, V2, SERVICE_ID, "GET", CHARGE_PATH, "response.amount", "type_changed"
    )
    assert change.endpoint_id == old.endpoints[0].id != new.endpoints[0].id
    assert (change.api_project_id, change.service_id) == (PROJECT_ID, SERVICE_ID)
    assert (change.old_snapshot_id, change.new_snapshot_id) == (V1, V2)
    assert change.evidence[:2] == [old.endpoints[0].evidence[0], new.endpoints[0].evidence[0]]
    field_items = change.evidence[2:]
    assert [(item.json_pointer, item.revision, item.source_uri) for item in field_items] == [
        (AMOUNT_POINTER, f"sha256:{SPEC_SHA[V1]}", SPEC_URIS[V1]),
        (AMOUNT_POINTER, f"sha256:{SPEC_SHA[V2]}", SPEC_URIS[V2]),
    ]
    for item in field_items:
        assert item.kind == "openapi"
        assert item.visibility == "provider_owned"
        assert item.id == evidence_id("openapi-field", item.revision, item.json_pointer)
        assert item.excerpt is not None and len(item.excerpt) <= 300
    assert [item.excerpt for item in field_items] == ["type: number", "type: string"]


def test_nested_array_item_field_removed() -> None:
    new_items = charge_schema()
    del new_items["properties"]["amount"]
    new_items["required"].remove("amount")
    old = demo_result(V1, extra=[list_endpoint(V1, charge_schema())])
    new = demo_result(V2, extra=[list_endpoint(V2, new_items)])

    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)

    assert summary([change]) == [("response.data[].amount", "field_removed", True)]
    assert change.endpoint_id == endpoint_id(V1, SERVICE_ID, "GET", "/v1/charges")
    assert AMOUNT_POINTER in {item.json_pointer for item in change.evidence}


def test_documented_endpoint_removed_is_one_endpoint_wide_change() -> None:
    old = demo_result(V1)

    [change] = detect_changes(old, demo_result(V2, refund=False), [], api_project_id=PROJECT_ID)

    assert summary([change]) == [("*", "behavior_changed", True)]
    assert change.endpoint_id == endpoint_id(V1, SERVICE_ID, "GET", REFUND_PATH)
    assert change.id == change_id(V1, V2, SERVICE_ID, "GET", REFUND_PATH, "*", "behavior_changed")
    assert (change.old_semantics, change.new_semantics) == (
        "endpoint documented in the current contract",
        "endpoint absent from the proposed contract",
    )
    assert (ENDPOINT_REMOVED_OLD, ENDPOINT_REMOVED_NEW) == (
        change.old_semantics,
        change.new_semantics,
    )
    assert change.evidence == [old.endpoints[1].evidence[0]]


def test_endpoint_that_loses_its_documentation_counts_as_removed_from_the_contract() -> None:
    old, new = demo_result(V1), demo_result(V2)
    refund = new.endpoints[1]
    undocumented = refund.model_copy(
        update={"evidence": receipt_route(V2).evidence, "reconciliation": "implementation_only"}
    )
    new = new.model_copy(update={"endpoints": [new.endpoints[0], undocumented, new.endpoints[2]]})
    changes = detect_changes(old, new, [], api_project_id=PROJECT_ID)
    assert [(change.endpoint_id, change.field_path) for change in changes] == [
        (old.endpoints[1].id, "*")
    ]


def test_implementation_only_and_new_only_endpoints_produce_no_change() -> None:
    old = demo_result(V1)
    new = demo_result(
        V2,
        receipt=False,
        extra=[create_refund_endpoint(V2, refund_body(), body_required=True)],
    )
    assert detect_changes(old, new, []) == []


def test_endpoint_of_another_service_is_never_a_counterpart() -> None:
    old, new = demo_result(V1), demo_result(V2)
    stripe_charge = documented(
        V2,
        "GET",
        CHARGE_PATH,
        "Retrieve a charge",
        response=json_response(charge_schema()),
        service_id="stripe",
    )
    new = new.model_copy(update={"endpoints": [*new.endpoints[1:], stripe_charge]})
    [change] = detect_changes(old, new, [], api_project_id=PROJECT_ID)
    assert (change.endpoint_id, change.field_path, change.service_id) == (
        old.endpoints[0].id,
        "*",
        SERVICE_ID,
    )


def test_results_for_different_services_are_not_comparable() -> None:
    stripe = DiscoveryResult(
        schema_version="shockwave.v1",
        service_id="stripe",
        snapshot_id=FIXTURE_IDS["stripe"]["contract_snapshot_id"],
        endpoints=[],
        coverage={"files_scanned": 0, "files_skipped": 0, "unsupported_patterns": []},
        warnings=[],
    )
    with pytest.raises(ValueError, match="different services"):
        detect_changes(demo_result(V1), stripe, [])


# Incomplete proposed contracts ----------------------------------------------------------

V2_DOCUMENT = SPEC_URIS[V2]
WHOLE_DOCUMENT_GAPS = {
    "unparseable": f"{PATTERN_UNPARSEABLE} {V2_DOCUMENT}",
    "unread": f"{PATTERN_UNREAD_DOCUMENT} {V2_DOCUMENT}",
    "no-documents": PATTERN_NO_DOCUMENTS,
}


def unread_v2(gap: str) -> DiscoveryResult:
    """v2 when its document was not read: only the source scan's routes, none documented."""
    read = demo_result(V2)
    return with_gaps(read, gap, endpoints=[source_only(read.endpoints[0]), receipt_route(V2)])


@pytest.mark.parametrize("gap", list(WHOLE_DOCUMENT_GAPS.values()), ids=list(WHOLE_DOCUMENT_GAPS))
def test_unread_proposed_contract_is_never_reported_as_removed_endpoints(gap: str) -> None:
    old = demo_result(V1)

    with pytest.raises(ProposedContractIncomplete) as raised:
        detect_changes(old, unread_v2(gap), [], api_project_id=PROJECT_ID)

    assert isinstance(raised.value, ValueError)
    assert raised.value.snapshot_id == V2
    assert raised.value.endpoints == [f"GET {CHARGE_PATH}", f"GET {REFUND_PATH}"]
    assert raised.value.gaps == [gap]
    assert str(raised.value) == (
        f"proposed contract snapshot {V2} was not fully read ({gap}); the removal of 2 "
        "documented endpoint(s) cannot be established because their operations are unknown, "
        f"not absent: GET {CHARGE_PATH}, GET {REFUND_PATH}"
    )


def test_unresolved_path_item_makes_only_its_own_endpoint_unknown() -> None:
    old, new = demo_result(V1), demo_result(V2, refund=False)
    unresolved = f"{PATTERN_UNRESOLVED_PATH_ITEM} /v1/refunds/{{refund}}"  # parameter names vary

    with pytest.raises(ProposedContractIncomplete) as raised:
        detect_changes(old, with_gaps(new, unresolved), [], api_project_id=PROJECT_ID)
    assert raised.value.endpoints == [f"GET {REFUND_PATH}"]
    assert raised.value.gaps == [f"{PATTERN_UNRESOLVED_PATH_ITEM} {REFUND_PATH}"]

    elsewhere = with_gaps(new, f"{PATTERN_UNRESOLVED_PATH_ITEM} /v1/customers/{{id}}")
    [change] = detect_changes(old, elsewhere, [], api_project_id=PROJECT_ID)
    assert (change.endpoint_id, change.field_path) == (old.endpoints[1].id, "*")
    assert change.new_semantics == ENDPOINT_REMOVED_NEW


def test_unread_document_does_not_hide_changes_in_endpoints_that_were_read() -> None:
    old, new = charge_pair(None, set_field("amount", type="string"))
    gap = f"{PATTERN_UNPARSEABLE} examples/provider/demo_payments/openapi/extra.yaml"
    changes = detect_changes(old, with_gaps(new, gap), [], api_project_id=PROJECT_ID)
    assert summary(changes) == [("response.amount", "type_changed", True)]


def test_declaration_on_an_unread_endpoint_reports_the_incomplete_contract() -> None:
    gap = WHOLE_DOCUMENT_GAPS["unread"]
    old, new = demo_result(V1), unread_v2(gap)
    note = docs_evidence("refund-retired", "GET /v1/refunds/{id} is retired in v2")
    removal = make_declared(
        "behavior_changed",
        "*",
        path=REFUND_PATH,
        evidence=[note],
        old_semantics="refunds can be retrieved by id",
        new_semantics="refund retrieval is retired",
    )
    units = declared_change()

    problems = validate_declared_changes(old, new, [units, removal])

    assert problems == [
        f"declared change {position} ({change.id}): GET {path} cannot be checked against the "
        f"proposed contract snapshot {V2}, which was not fully read ({gap}); its operations "
        "there are unknown, not absent"
        for position, (change, path) in enumerate([(units, CHARGE_PATH), (removal, REFUND_PATH)])
    ]
    with pytest.raises(ProposedContractIncomplete):
        detect_changes(old, new, [units, removal])

    # Checks of the current contract still run, so every problem is reported at once.
    local_name = units.model_copy(update={"field_path": "amount"})
    [unread, not_a_field] = validate_declared_changes(old, new, [local_name])
    assert "cannot be checked against the proposed contract snapshot" in unread
    assert "is not an API field path" in not_a_field


REQUEST_CASES: dict[str, tuple[dict, dict, list[tuple[str, str, bool]]]] = {
    "optional-param-becomes-required": (
        id_request(query={"expand": {"type": "string"}}),
        id_request(query={"expand": {"type": "string"}}, query_required=("expand",)),
        [("request.query.expand", "behavior_changed", True)],
    ),
    "new-required-param": (
        id_request(),
        id_request(query={"limit": {"type": "integer"}}, query_required=("limit",)),
        [("request.query.limit", "behavior_changed", True)],
    ),
    "new-optional-param": (id_request(), id_request(query={"limit": {"type": "integer"}}), []),
    "param-removed": (
        id_request(query={"expand": {"type": "string"}}),
        id_request(),
        [("request.query.expand", "field_removed", True)],
    ),
    "accepted-enum-loses-value": (
        id_request(query={"expand": {"type": "string", "enum": ["customer", "invoice"]}}),
        id_request(query={"expand": {"type": "string", "enum": ["customer"]}}),
        [("request.query.expand", "enum_changed", True)],
    ),
    "accepted-enum-gains-value": (
        id_request(query={"expand": {"type": "string", "enum": ["customer"]}}),
        id_request(query={"expand": {"type": "string", "enum": ["customer", "invoice"]}}),
        [("request.query.expand", "enum_changed", False)],
    ),
    "accepted-type-loses-values": (
        id_request(query={"limit": {"type": "number"}}),
        id_request(query={"limit": {"type": "integer"}}),
        [("request.query.limit", "type_changed", True)],
    ),
    "accepted-type-gains-values": (
        id_request(query={"limit": {"type": "integer"}}),
        id_request(query={"limit": {"type": "number"}}),
        [("request.query.limit", "type_changed", False)],
    ),
}


@pytest.mark.parametrize(
    ("old_request", "new_request", "expected"),
    list(REQUEST_CASES.values()),
    ids=list(REQUEST_CASES),
)
def test_structural_request_changes(
    old_request: dict, new_request: dict, expected: list[tuple[str, str, bool]]
) -> None:
    old = demo_result(V1, charge_request=old_request)
    new = demo_result(V2, charge_request=new_request)
    assert summary(detect_changes(old, new, [], api_project_id=PROJECT_ID)) == expected


@pytest.mark.parametrize(
    ("old_body", "new_body", "expected"),
    [
        (
            (refund_body(), True),
            (refund_body(type="integer"), True),
            [("request.body.amount", "type_changed", True)],
        ),
        (
            (refund_body(), False),
            (refund_body(), True),
            [("request.body", "behavior_changed", True)],
        ),
        (
            (refund_body(), True),
            (refund_body(required=("charge", "reason")), True),
            [("request.body.reason", "behavior_changed", True)],
        ),
    ],
    ids=["body-type-narrows", "body-becomes-required", "body-property-becomes-required"],
)
def test_structural_request_body_changes(
    old_body: tuple[dict, bool], new_body: tuple[dict, bool], expected: list[tuple[str, str, bool]]
) -> None:
    (old_schema, old_required), (new_schema, new_required) = old_body, new_body
    old_create = create_refund_endpoint(V1, old_schema, body_required=old_required)
    new_create = create_refund_endpoint(V2, new_schema, body_required=new_required)
    old, new = demo_result(V1, extra=[old_create]), demo_result(V2, extra=[new_create])
    changes = detect_changes(old, new, [], api_project_id=PROJECT_ID)
    assert summary(changes) == expected
    create_refund = endpoint_id(V1, SERVICE_ID, "POST", "/v1/refunds")
    assert all(change.endpoint_id == create_refund for change in changes)


# api_project_id, determinism, purity ------------------------------------------------------


def test_api_project_id_is_required_for_structural_changes_without_declarations() -> None:
    old, new = charge_pair(None, set_field("amount", type="string"))
    with pytest.raises(ValueError, match="api_project_id required") as raised:
        detect_changes(old, new, [])
    # The message names the keyword a caller of the 3-argument contract form must add.
    assert "api_project_id=<APIProject id>" in str(raised.value)


def test_api_project_id_comes_from_the_argument_or_the_declared_changes() -> None:
    old, new = charge_pair(None, remove_currency)

    [explicit] = detect_changes(old, new, [], api_project_id=OTHER_PROJECT_ID)
    assert explicit.api_project_id == OTHER_PROJECT_ID

    changes = detect_changes(old, new, [declared_change()])
    assert summary(changes) == [
        ("response.amount", "units_changed", False),
        ("response.currency", "field_removed", True),
    ]
    assert {change.api_project_id for change in changes} == {PROJECT_ID}

    spelled_differently = detect_changes(
        old, new, [declared_change()], api_project_id=PROJECT_ID.upper()
    )
    assert [change.model_dump(mode="json") for change in spelled_differently] == [
        change.model_dump(mode="json") for change in changes
    ]


def test_conflicting_api_project_ids_are_rejected() -> None:
    old, new = demo_result(V1), demo_result(V2)
    with pytest.raises(InvalidDeclaredChange, match="does not match the assessed project"):
        detect_changes(old, new, [declared_change()], api_project_id=OTHER_PROJECT_ID)

    note = docs_evidence("refund-latency", "refund retrieval becomes eventually consistent")
    other = make_declared(
        "behavior_changed",
        "*",
        path=REFUND_PATH,
        evidence=[note],
        old_semantics="refund reads are immediately consistent",
        new_semantics="refund reads may lag by up to 5 seconds",
        api_project_id=OTHER_PROJECT_ID,
    )
    with pytest.raises(ValueError, match="disagree on api_project_id"):
        detect_changes(old, new, [declared_change(), other])


def build_mixed_pair() -> tuple[DiscoveryResult, DiscoveryResult]:
    new_charge = json_response(charge_schema())
    remove_currency(new_charge)
    set_field("amount", nullable=True)(new_charge)
    new_items = charge_schema()
    del new_items["properties"]["amount"]
    new_items["required"].remove("amount")
    old = demo_result(V1, extra=[list_endpoint(V1, charge_schema())])
    new = demo_result(V2, charge=new_charge, refund=False, extra=[list_endpoint(V2, new_items)])
    return old, new


def test_output_is_deterministic_sorted_and_inputs_are_not_mutated() -> None:
    old, new = build_mixed_pair()
    declared = declared_change()
    before = [copy.deepcopy(model.model_dump(mode="json")) for model in (old, new, declared)]

    first = detect_changes(old, new, [declared])
    second = detect_changes(*build_mixed_pair(), [declared_change()])

    assert [change.model_dump(mode="json") for change in first] == [
        change.model_dump(mode="json") for change in second
    ]
    assert [model.model_dump(mode="json") for model in (old, new, declared)] == before
    paths = {endpoint.id: (endpoint.path, endpoint.method) for endpoint in old.endpoints}
    assert [(*paths[change.endpoint_id], change.field_path, change.kind) for change in first] == [
        ("/v1/charges", "GET", "response.data[].amount", "field_removed"),
        (CHARGE_PATH, "GET", "response.amount", "type_changed"),
        (CHARGE_PATH, "GET", "response.amount", "units_changed"),
        (CHARGE_PATH, "GET", "response.currency", "field_removed"),
        (REFUND_PATH, "GET", "*", "behavior_changed"),
    ]
    for change in first:
        path, method = paths[change.endpoint_id]
        if change.id != declared.id:
            expected = change_id(V1, V2, SERVICE_ID, method, path, change.field_path, change.kind)
            assert change.id == expected
    units = next(change for change in first if change.kind == "units_changed")
    assert units.structural_breaking is True  # amount became nullable in this proposal
    assert all(item is not original for item in units.evidence for original in declared.evidence)
    json.dumps([change.model_dump(mode="json") for change in first])
