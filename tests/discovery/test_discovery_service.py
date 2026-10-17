"""discover / discover_endpoints end to end: the demo provider, pinned Stripe, input gaps.

Expected values come from the data itself (fixture_ids.json, the declared change, the
hand-maintained specs, line numbers found in the provider source, counts computed from
the raw Stripe JSON), never from literals that could drift from it. Nothing here touches
the network: an autouse guard fails any test that tries.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from analyzer.changes import detect_changes
from discovery import discover, discover_endpoints
from discovery.contracts import APIChange, DiscoveryInput, DiscoveryResult, Endpoint
from discovery.ids import endpoint_id, evidence_id
from discovery.inventory import RECONCILIATION_STATUSES, summarize_inventory
from discovery.pinned import STRIPE_PIN_PATH, load_pinned_document
from discovery.reconcile import (
    IMPLEMENTATION_ONLY_SCHEMA,
    SCHEMA_NOTE_KEY,
    SOURCE_TOKEN_KEY,
    UNKNOWN_SOURCE_REVISION,
)
from discovery.records import DiscoveryError, FetchedDocument, operation_pointer
from discovery.service import MAX_UNSUPPORTED_PATTERNS, MAX_WARNINGS, DiscoveryOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "provider" / "demo_payments"
DEMO_SERVICE = DEMO_DIR / "service"
DEMO_PACKAGE = DEMO_SERVICE / "demo_payments"
SPEC_V1 = DEMO_DIR / "openapi" / "demo-payments.v1.yaml"
SPEC_V2 = DEMO_DIR / "openapi" / "demo-payments.v2.yaml"
DECLARED_CHANGE = json.loads(
    (DEMO_DIR / "changes" / "charge_amount_cents_to_dollars.json").read_text(encoding="utf-8")
)
FIXTURE_IDS = json.loads((DEMO_DIR / "fixture_ids.json").read_text(encoding="utf-8"))
SERVICE_ID = FIXTURE_IDS["service_id"]
V1 = FIXTURE_IDS["contract_snapshots"]["v1"]
V2 = FIXTURE_IDS["contract_snapshots"]["v2_proposed"]
SOURCE_SNAPSHOT = FIXTURE_IDS["source_snapshot_id"]
V1_REF, V2_REF = "demo-payments.v1.yaml", "demo-payments.v2.yaml"
STRIPE_REF = "stripe/openapi:latest/openapi.spec3.json"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

CHARGE = ("GET", "/v1/charges/{id}")
RECEIPT = ("GET", "/v1/charges/{id}/receipt")
REFUND = ("GET", "/v1/refunds/{id}")
HEALTH = ("GET", "/healthz")


# Helpers --------------------------------------------------------------------------------


def line_of(path: Path, needle: str) -> int:
    matches = [
        number
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if needle in line
    ]
    assert len(matches) == 1, f"{needle!r} must appear exactly once in {path.name}"
    return matches[0]


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def demo_request(
    snapshot: str = V1,
    ref: str = V1_REF,
    *,
    frameworks: tuple[str, ...] = ("fastapi",),
    source_snapshot_id: str | None = SOURCE_SNAPSHOT,
    service_id: str = SERVICE_ID,
) -> DiscoveryInput:
    return DiscoveryInput(
        service_id=service_id,
        snapshot_id=snapshot,
        openapi_documents=[ref],
        source_snapshot_id=source_snapshot_id,
        frameworks=list(frameworks),
    )


def demo_inputs(spec: Path = SPEC_V1, ref: str = V1_REF) -> dict[str, FetchedDocument]:
    return {
        ref: FetchedDocument(spec.read_bytes(), source_uri=spec.relative_to(REPO_ROOT).as_posix())
    }


def by_key(result: DiscoveryResult) -> dict[tuple[str, str], Endpoint]:
    endpoints = {(endpoint.method, endpoint.path): endpoint for endpoint in result.endpoints}
    assert len(endpoints) == len(result.endpoints), "endpoint identity must be unique"
    return endpoints


def coverage(result: DiscoveryResult) -> dict[str, Any]:
    return result.model_dump(mode="json")["coverage"]


def raw_spec(spec: Path) -> dict[str, Any]:
    return yaml.safe_load(spec.read_text(encoding="utf-8"))


def things_spec(*paths: str) -> bytes:
    """A small hand-maintained spec documenting GET for each path (data for the tests)."""
    document: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": "Things (test)", "version": "1.0.0"},
        "paths": {
            path: {
                "get": {
                    "summary": f"Get {path}",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            }
            for path in paths
        },
    }
    return json.dumps(document).encode()


def things_request(*, frameworks: tuple[str, ...] = ("fastapi",)) -> DiscoveryInput:
    return DiscoveryInput(
        service_id="things-test",
        snapshot_id=V1,
        openapi_documents=["things.json"],
        source_snapshot_id=SOURCE_SNAPSHOT,
        frameworks=list(frameworks),
    )


@pytest.fixture(autouse=True, scope="module")
def no_network() -> Iterator[None]:
    """Module-scoped and autouse, so it is active before the module-scoped outcomes are built."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("discovery must never open a network connection")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", refuse)
        patch.setattr(socket.socket, "connect_ex", refuse)
        patch.setattr(socket, "create_connection", refuse)
        patch.setattr(socket, "getaddrinfo", refuse)
        yield


@pytest.fixture(scope="module")
def demo_v1() -> DiscoveryOutcome:
    return discover(demo_request(), demo_inputs(), DEMO_SERVICE)


@pytest.fixture(scope="module")
def demo_v2() -> DiscoveryOutcome:
    return discover(demo_request(V2, V2_REF), demo_inputs(SPEC_V2, V2_REF), DEMO_SERVICE)


@pytest.fixture(scope="module")
def stripe() -> tuple[DiscoveryOutcome, dict[str, Any], dict[str, Any], float]:
    pin = json.loads(STRIPE_PIN_PATH.read_text(encoding="utf-8"))
    start = time.perf_counter()
    fetched = load_pinned_document(STRIPE_PIN_PATH)
    request = DiscoveryInput(
        service_id=FIXTURE_IDS["stripe"]["service_id"],
        snapshot_id=FIXTURE_IDS["stripe"]["contract_snapshot_id"],
        openapi_documents=[STRIPE_REF],
        frameworks=[],
    )
    outcome = discover(request, {STRIPE_REF: fetched}, None)
    elapsed = time.perf_counter() - start
    return outcome, json.loads(fetched.content), pin, elapsed


# Demo Payments v1 -----------------------------------------------------------------------


def test_demo_v1_reconciles_documented_and_implemented_routes(demo_v1: DiscoveryOutcome) -> None:
    result = demo_v1.result
    assert {key: endpoint.reconciliation for key, endpoint in by_key(result).items()} == {
        CHARGE: "documented_and_found",
        RECEIPT: "implementation_only",
        REFUND: "documented_not_found_in_scan",
        HEALTH: "not_compared",
    }
    assert (result.service_id, result.snapshot_id) == (SERVICE_ID, V1)
    assert all(e.service_id == SERVICE_ID and e.snapshot_id == V1 for e in result.endpoints)
    assert [(e.path, e.method) for e in result.endpoints] == sorted(
        (e.path, e.method) for e in result.endpoints
    )


def test_demo_charge_route_carries_openapi_and_source_evidence(demo_v1: DiscoveryOutcome) -> None:
    charge = by_key(demo_v1.result)[CHARGE]
    documented = raw_spec(SPEC_V1)["paths"]["/v1/charges/{id}"]["get"]
    sha = hashlib.sha256(SPEC_V1.read_bytes()).hexdigest()
    pointer = "/paths/~1v1~1charges~1{id}/get"
    assert pointer == operation_pointer("/v1/charges/{id}", "get")

    openapi, source = charge.evidence
    assert (openapi.kind, openapi.json_pointer, openapi.visibility) == (
        "openapi",
        pointer,
        "provider_owned",
    )
    assert openapi.id == evidence_id("openapi", sha, pointer)
    assert openapi.revision == f"sha256:{sha}"
    assert openapi.source_uri == "examples/provider/demo_payments/openapi/demo-payments.v1.yaml"
    assert openapi.excerpt == f"GET /v1/charges/{{id}} - {documented['summary']}"

    line = line_of(DEMO_PACKAGE / "charges.py", '@router.get("/{id}")')
    assert (source.kind, source.file, source.line_start, source.line_end) == (
        "source",
        "demo_payments/charges.py",
        line,
        line,
    )
    assert source.source_uri == f"source-snapshot://{SOURCE_SNAPSHOT}/demo_payments/charges.py"
    assert (source.revision, source.visibility) == (SOURCE_SNAPSHOT, "provider_owned")
    assert source.excerpt == '@router.get("/{id}")'
    assert (charge.operation_id, charge.confidence) == (documented["operationId"], "direct")
    assert charge.response_schema["properties"]["amount"]["type"] == "number"


def test_demo_charge_endpoint_id_is_the_declared_change_endpoint(demo_v1: DiscoveryOutcome) -> None:
    charge = by_key(demo_v1.result)[CHARGE]
    assert charge.id == endpoint_id(V1, SERVICE_ID, *CHARGE) == DECLARED_CHANGE["endpoint_id"]
    assert (DECLARED_CHANGE["service_id"], DECLARED_CHANGE["old_snapshot_id"]) == (SERVICE_ID, V1)


def test_demo_receipt_route_is_implementation_only(demo_v1: DiscoveryOutcome) -> None:
    assert "/v1/charges/{id}/receipt" not in raw_spec(SPEC_V1)["paths"]  # omitted by hand
    receipt = by_key(demo_v1.result)[RECEIPT]

    (source,) = receipt.evidence
    line = line_of(DEMO_PACKAGE / "charges.py", '@router.get("/{id}/receipt")')
    assert (source.kind, source.file, source.line_start) == (
        "source",
        "demo_payments/charges.py",
        line,
    )
    assert receipt.operation_id is None
    assert receipt.request_schema == {  # unknown, but the {id} its route declares is listed
        "type": "object",
        SCHEMA_NOTE_KEY: IMPLEMENTATION_ONLY_SCHEMA,
        "properties": {
            "path": {
                "type": "object",
                "properties": {"id": {SOURCE_TOKEN_KEY: "{id}"}},
                "required": ["id"],
            }
        },
        "required": ["path"],
    }
    assert receipt.response_schema == {SCHEMA_NOTE_KEY: IMPLEMENTATION_ONLY_SCHEMA}
    assert receipt.id == endpoint_id(V1, SERVICE_ID, *RECEIPT)


def test_demo_refund_route_behind_a_dynamic_prefix_is_not_found_in_scan(
    demo_v1: DiscoveryOutcome,
) -> None:
    refund = by_key(demo_v1.result)[REFUND]
    assert refund.reconciliation == "documented_not_found_in_scan"
    assert [item.kind for item in refund.evidence] == ["openapi"]

    line = line_of(DEMO_PACKAGE / "refunds.py", "prefix=settings.REFUNDS_PREFIX")
    pattern = f"fastapi:dynamic-prefix demo_payments/refunds.py:{line}"
    assert pattern in coverage(demo_v1.result)["unsupported_patterns"]
    (caveat,) = [
        w for w in demo_v1.result.warnings if "may explain unmatched documented routes" in w
    ]
    assert "not proof of absence" in caveat


def test_demo_health_route_outside_the_namespace_is_not_compared(demo_v1: DiscoveryOutcome) -> None:
    health = by_key(demo_v1.result)[HEALTH]
    assert health.reconciliation == "not_compared"
    assert [item.kind for item in health.evidence] == ["source"]
    assert health.response_schema[SCHEMA_NOTE_KEY].startswith("unknown:")
    line = line_of(DEMO_PACKAGE / "health.py", '@router.get("/healthz")')
    assert any(
        "outside the documented namespace (/v1)" in warning
        and f"GET /healthz (demo_payments/health.py:{line})" in warning
        for warning in demo_v1.result.warnings
    )


def test_demo_result_validates_and_round_trips(demo_v1: DiscoveryOutcome) -> None:
    dumped = demo_v1.result.model_dump(mode="json")
    assert dumped["schema_version"] == "shockwave.v1"
    assert DiscoveryResult.model_validate(dumped) == demo_v1.result
    assert json.loads(json.dumps(dumped)) == dumped
    excerpts = [item["excerpt"] for e in dumped["endpoints"] for item in e["evidence"]]
    assert all(excerpt and len(excerpt) <= 300 for excerpt in excerpts)
    assert len(dumped["warnings"]) == len(set(dumped["warnings"]))


def test_demo_coverage_and_provenance_are_computed(demo_v1: DiscoveryOutcome) -> None:
    python_files = [p for p in DEMO_SERVICE.rglob("*.py") if "__pycache__" not in p.parts]
    assert coverage(demo_v1.result)["files_scanned"] == len(python_files)
    assert coverage(demo_v1.result)["files_skipped"] == 0

    raw = raw_spec(SPEC_V1)
    (document,) = demo_v1.documents
    assert document.document_ref == V1_REF
    assert document.sha256 == hashlib.sha256(SPEC_V1.read_bytes()).hexdigest()
    assert document.info_version == raw["info"]["version"]
    assert document.operation_count == sum(
        1 for item in raw["paths"].values() for method in item if method in HTTP_METHODS
    )
    assert {(r.method, r.path) for r in demo_v1.source_routes} == {CHARGE, RECEIPT, HEALTH}


def test_discover_endpoints_is_the_contract_view_of_discover(demo_v1: DiscoveryOutcome) -> None:
    result = discover_endpoints(demo_request(), demo_inputs(), DEMO_SERVICE)
    assert result.model_dump(mode="json") == demo_v1.result.model_dump(mode="json")


def test_discovery_is_deterministic_and_never_mutates_inputs(demo_v1: DiscoveryOutcome) -> None:
    request, inputs = demo_request(), demo_inputs()
    before = (request.model_dump(), copy.deepcopy(inputs))
    first = discover(request, inputs, DEMO_SERVICE)
    expected = first.result.model_dump(mode="json")
    assert expected == demo_v1.result.model_dump(mode="json")
    assert (request.model_dump(), inputs) == before

    # Results share no state: mutating one cannot change the next.
    by_key(first.result)[CHARGE].response_schema["properties"].clear()
    by_key(first.result)[RECEIPT].response_schema.clear()
    assert discover(request, inputs, DEMO_SERVICE).result.model_dump(mode="json") == expected


# Proposed contract ----------------------------------------------------------------------


def test_v2_contract_is_structurally_identical_to_v1(
    demo_v1: DiscoveryOutcome, demo_v2: DiscoveryOutcome
) -> None:
    old, new = by_key(demo_v1.result), by_key(demo_v2.result)
    assert old.keys() == new.keys()
    amount = new[CHARGE].response_schema["properties"]["amount"]
    assert amount["type"] == "number"
    assert amount == old[CHARGE].response_schema["properties"]["amount"]
    for key, endpoint in new.items():
        assert endpoint.request_schema == old[key].request_schema
        assert endpoint.response_schema == old[key].response_schema
        assert endpoint.reconciliation == old[key].reconciliation
        assert endpoint.id == endpoint_id(V2, SERVICE_ID, *key) != old[key].id
    assert demo_v2.documents[0].info_version == raw_spec(SPEC_V2)["info"]["version"]
    assert demo_v2.documents[0].info_version != demo_v1.documents[0].info_version


def test_discovered_snapshots_feed_change_detection(
    demo_v1: DiscoveryOutcome, demo_v2: DiscoveryOutcome
) -> None:
    project = FIXTURE_IDS["api_project_id"]
    assert detect_changes(demo_v1.result, demo_v2.result, [], api_project_id=project) == []

    declared = APIChange.model_validate(DECLARED_CHANGE)
    changes = detect_changes(demo_v1.result, demo_v2.result, [declared])
    assert [(c.kind, c.endpoint_id, c.field_path, c.structural_breaking) for c in changes] == [
        ("units_changed", by_key(demo_v1.result)[CHARGE].id, "response.amount", False)
    ]


# Source handling ------------------------------------------------------------------------


def test_without_source_every_endpoint_is_not_compared() -> None:
    outcome = discover(demo_request(frameworks=(), source_snapshot_id=None), demo_inputs(), None)

    assert {key: e.reconciliation for key, e in by_key(outcome.result).items()} == {
        CHARGE: "not_compared",
        REFUND: "not_compared",
    }
    assert coverage(outcome.result) == {
        "files_scanned": 0,
        "files_skipped": 0,
        "unsupported_patterns": [],
    }
    assert all(item.kind == "openapi" for e in outcome.result.endpoints for item in e.evidence)
    assert outcome.source_routes == ()
    assert outcome.result.warnings == []


def test_source_without_frameworks_and_frameworks_without_source_are_not_scanned() -> None:
    unscanned = discover(demo_request(frameworks=()), demo_inputs(), DEMO_SERVICE)
    assert (
        "source provided but no frameworks requested; source not scanned"
        in unscanned.result.warnings
    )
    assert {e.reconciliation for e in unscanned.result.endpoints} == {"not_compared"}
    assert coverage(unscanned.result)["files_scanned"] == 0
    assert unscanned.source_routes == ()

    sourceless = discover(demo_request(), demo_inputs(), None)
    assert any("no source root was supplied" in w for w in sourceless.result.warnings)
    assert {e.reconciliation for e in sourceless.result.endpoints} == {"not_compared"}


def test_framework_names_are_case_insensitive_and_unknown_ones_are_reported() -> None:
    outcome = discover(
        demo_request(frameworks=("Django", " FastAPI ")), demo_inputs(), DEMO_SERVICE
    )
    assert "framework:unsupported django" in coverage(outcome.result)["unsupported_patterns"]
    assert any("framework django is not supported" in w for w in outcome.result.warnings)
    assert by_key(outcome.result)[CHARGE].reconciliation == "documented_and_found"

    only_unknown = discover(demo_request(frameworks=("django",)), demo_inputs(), DEMO_SERVICE)
    assert any("no requested framework is supported" in w for w in only_unknown.result.warnings)
    assert {e.reconciliation for e in only_unknown.result.endpoints} == {"not_compared"}


def test_source_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    not_a_directory = write(tmp_path, "file.py", "x = 1\n")
    for candidate in (not_a_directory, tmp_path / "missing"):
        with pytest.raises(DiscoveryError) as error:
            discover(demo_request(), demo_inputs(), candidate)
        assert str(tmp_path) not in str(error.value)  # internal paths are not echoed


def test_source_scanned_without_a_source_snapshot_says_its_revision_is_unknown() -> None:
    outcome = discover(demo_request(source_snapshot_id=None), demo_inputs(), DEMO_SERVICE)
    (source,) = [item for item in by_key(outcome.result)[CHARGE].evidence if item.kind == "source"]
    line = line_of(DEMO_PACKAGE / "charges.py", '@router.get("/{id}")')

    assert source.revision == UNKNOWN_SOURCE_REVISION == "unknown-source-revision"
    assert source.source_uri == "source-root://demo_payments/charges.py"
    assert source.id == evidence_id("source", "unknown", "demo_payments/charges.py", line, *CHARGE)
    assert any("without a source_snapshot_id" in w for w in outcome.result.warnings)


def test_parameter_name_differences_are_matched_and_reported(tmp_path: Path) -> None:
    write(
        tmp_path,
        "things/app.py",
        """
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/v1/things/{id}")
        def get_thing(id: str) -> dict:
            return {"id": id}
        """,
    )
    outcome = discover(
        things_request(), {"things.json": things_spec("/v1/things/{thing}")}, tmp_path
    )

    (thing,) = outcome.result.endpoints
    assert (thing.method, thing.path, thing.reconciliation) == (
        "GET",
        "/v1/things/{thing}",
        "documented_and_found",
    )
    line = line_of(tmp_path / "things" / "app.py", '@app.get("/v1/things/{id}")')
    (warning,) = [w for w in outcome.result.warnings if "parameter names differ" in w]
    assert warning == (
        "GET /v1/things/{thing}: parameter names differ: documented {thing} vs source {id}"
        f" (things/app.py:{line})"
    )


def test_duplicate_source_registrations_are_a_conflict(tmp_path: Path) -> None:
    write(tmp_path, "svc/__init__.py", "")
    write(
        tmp_path,
        "svc/main.py",
        """
        from fastapi import FastAPI

        from svc import a, b

        app = FastAPI()
        app.include_router(a.router)
        app.include_router(b.router)
        """,
    )
    write(
        tmp_path,
        "svc/a.py",
        """
        from fastapi import APIRouter

        router = APIRouter(prefix="/v1")


        @router.get("/widgets/{id}")
        def get_widget(id: str) -> dict:
            return {}


        @router.get("/gadgets/{id}")
        def get_gadget(id: str) -> dict:
            return {}
        """,
    )
    write(
        tmp_path,
        "svc/b.py",
        """
        from fastapi import APIRouter

        router = APIRouter()


        @router.get("/v1/widgets/{widget_id}")
        def fetch_widget(widget_id: str) -> dict:
            return {}


        @router.get("/v1/gadgets/{gadget_id}")
        def fetch_gadget(gadget_id: str) -> dict:
            return {}
        """,
    )
    outcome = discover(
        things_request(), {"things.json": things_spec("/v1/widgets/{widget}")}, tmp_path
    )
    endpoints = by_key(outcome.result)

    widget = endpoints["GET", "/v1/widgets/{widget}"]
    assert widget.reconciliation == "conflict"
    assert [(item.kind, item.file) for item in widget.evidence] == [
        ("openapi", None),
        ("source", "svc/a.py"),
        ("source", "svc/b.py"),
    ]
    gadget = endpoints["GET", "/v1/gadgets/{gadget_id}"]
    assert gadget.reconciliation == "conflict"
    assert sorted(item.file for item in gadget.evidence) == ["svc/a.py", "svc/b.py"]
    conflicts = [w for w in outcome.result.warnings if "registered by more than one handler" in w]
    assert len(conflicts) == 2


def test_express_routes_reconcile_with_the_documented_namespace(tmp_path: Path) -> None:
    write(
        tmp_path,
        "server.js",
        """
        const express = require('express');
        const app = express();

        app.get('/v1/things/:thing', (req, res) => res.json({}));
        app.post('/v1/things', (req, res) => res.json({}));
        app.get('/status', (req, res) => res.json({}));
        app.listen(3000);
        """,
    )
    outcome = discover(
        things_request(frameworks=("Express",)),
        {"things.json": things_spec("/v1/things/{thing}")},
        tmp_path,
    )

    assert {key: e.reconciliation for key, e in by_key(outcome.result).items()} == {
        ("GET", "/v1/things/{thing}"): "documented_and_found",
        ("POST", "/v1/things"): "implementation_only",
        ("GET", "/status"): "not_compared",
    }
    assert coverage(outcome.result)["files_scanned"] == 1
    assert all(route.framework == "express" for route in outcome.source_routes)


def test_a_server_base_path_scopes_the_documented_namespace() -> None:
    # The same hand spec served behind a gateway: servers url .../api, as FastAPI
    # root_path="/api" generates. The app's own routes do not carry /api.
    spec = SPEC_V1.read_bytes().replace(
        b"url: http://demo-payments.test\n", b"url: http://demo-payments.test/api\n"
    )
    assert spec != SPEC_V1.read_bytes()
    outcome = discover(demo_request(), {V1_REF: spec}, DEMO_SERVICE)

    assert {key: e.reconciliation for key, e in by_key(outcome.result).items()} == {
        ("GET", "/api/v1/charges/{id}"): "documented_not_found_in_scan",
        ("GET", "/api/v1/refunds/{id}"): "documented_not_found_in_scan",
        CHARGE: "not_compared",  # never "undocumented": the docs describe it under /api
        RECEIPT: "not_compared",
        HEALTH: "not_compared",
    }
    (outside,) = [w for w in outcome.result.warnings if "outside the documented namespace" in w]
    assert outside.startswith("3 source route(s) are outside the documented namespace (/api)")


def test_mounted_express_parameters_survive_in_the_result(tmp_path: Path) -> None:
    write(
        tmp_path,
        "app.js",
        """
        const express = require('express');
        const app = express();
        const router = express.Router();
        router.get('/charges/:chargeId', (req, res) => res.json({}));
        app.use('/v1/accounts/:acct', router);
        app.listen(3000);
        """,
    )
    outcome = discover(
        things_request(frameworks=("express",)), {"things.json": things_spec("/v1/other")}, tmp_path
    )

    charge = by_key(outcome.result)["GET", "/v1/accounts/{acct}/charges/{chargeId}"]
    assert charge.reconciliation == "implementation_only"
    assert charge.request_schema["properties"]["path"]["properties"] == {
        "acct": {SOURCE_TOKEN_KEY: ":acct"},
        "chargeId": {SOURCE_TOKEN_KEY: ":chargeId"},
    }
    assert charge.request_schema[SCHEMA_NOTE_KEY] == IMPLEMENTATION_ONLY_SCHEMA
    (source,) = charge.evidence
    line = line_of(tmp_path / "app.js", "router.get('/charges/:chargeId'")
    assert (source.file, source.line_start) == ("app.js", line)
    assert source.excerpt is not None
    assert source.excerpt.startswith("raw path /v1/accounts/:acct/charges/:chargeId; router.get(")


# Document inputs ------------------------------------------------------------------------


def test_missing_unparseable_and_unlisted_inputs_are_warnings_not_errors() -> None:
    request = DiscoveryInput(
        service_id=SERVICE_ID,
        snapshot_id=V1,
        openapi_documents=["missing.yaml", "broken.yaml", "swagger.json", V1_REF],
        frameworks=[],
    )
    inputs = {
        "broken.yaml": b"openapi: 3.0.3\npaths: [unclosed\n",
        "swagger.json": b'{"swagger": "2.0", "paths": {}}',
        V1_REF: SPEC_V1.read_bytes(),
        "extra.yaml": SPEC_V2.read_bytes(),
    }
    outcome = discover(request, inputs, None)
    warnings = outcome.result.warnings

    assert "openapi document missing.yaml was not fetched; its operations are unknown" in warnings
    assert any(w.startswith("openapi document broken.yaml could not be parsed") for w in warnings)
    assert any(w.startswith("openapi document swagger.json could not be parsed") for w in warnings)
    assert "fetched input extra.yaml is not listed in openapi_documents; ignored" in warnings
    patterns = coverage(outcome.result)["unsupported_patterns"]
    assert {"openapi:unparseable broken.yaml", "openapi:unparseable swagger.json"} <= set(patterns)

    assert set(by_key(outcome.result)) == {CHARGE, REFUND}  # the readable document still counts
    assert [document.document_ref for document in outcome.documents] == [V1_REF]
    assert by_key(outcome.result)[CHARGE].evidence[0].source_uri == V1_REF  # bytes: ref is the URI


def test_inputs_that_are_not_fetched_bytes_are_never_opened_or_fetched(tmp_path: Path) -> None:
    local = tmp_path / "spec.yaml"
    local.write_bytes(SPEC_V1.read_bytes())
    url = "https://example.invalid/openapi.json"
    request = DiscoveryInput(
        service_id=SERVICE_ID,
        snapshot_id=V1,
        openapi_documents=[str(local), url, "https://example.invalid/unlisted.json"],
        frameworks=[],
    )
    outcome = discover(request, {str(local): str(local), url: url}, None)

    assert outcome.result.endpoints == []
    assert outcome.documents == ()
    assert sum("not as fetched bytes" in w for w in outcome.result.warnings) == 2
    assert any("unlisted.json was not fetched" in w for w in outcome.result.warnings)


def test_operations_documented_twice_keep_the_first_and_flag_differences() -> None:
    v1 = SPEC_V1.read_bytes()
    reworded = v1.replace(b"description: Charge amount.", b"description: Charge amount (reworded).")
    assert reworded != v1
    request = DiscoveryInput(
        service_id=SERVICE_ID,
        snapshot_id=V1,
        openapi_documents=["a.yaml", "b.yaml", "copy.yaml"],
        frameworks=[],
    )
    outcome = discover(request, {"a.yaml": v1, "b.yaml": reworded, "copy.yaml": v1}, None)
    endpoints = by_key(outcome.result)

    charge = endpoints[CHARGE]
    assert charge.reconciliation == "conflict"  # the documents disagree about its schema
    assert [item.source_uri for item in charge.evidence] == ["a.yaml", "b.yaml"]
    assert charge.response_schema["properties"]["amount"]["description"] == "Charge amount."
    refund = endpoints[REFUND]
    assert refund.reconciliation == "not_compared"
    assert [item.source_uri for item in refund.evidence] == ["a.yaml", "b.yaml"]
    assert any(
        "copy.yaml has the same content (sha256) as a.yaml" in w for w in outcome.result.warnings
    )
    assert [document.document_ref for document in outcome.documents] == [
        "a.yaml",
        "b.yaml",
        "copy.yaml",
    ]


def test_warnings_and_unsupported_patterns_are_bounded() -> None:
    names = tuple(f"framework{index:03d}" for index in range(MAX_WARNINGS + 50))
    outcome = discover(demo_request(frameworks=names), demo_inputs(), None)

    warnings = outcome.result.warnings
    distinct = len(names) + 1  # one per unknown framework, plus "no source root was supplied"
    assert len(warnings) == MAX_WARNINGS
    assert warnings[-1] == f"... {distinct - (MAX_WARNINGS - 1)} more warning(s) not shown"
    assert warnings[0] == "framework framework000 is not supported; its routes were not scanned"

    patterns = coverage(outcome.result)["unsupported_patterns"]
    expected = sorted(f"framework:unsupported {name}" for name in names)
    assert patterns == [
        *expected[:MAX_UNSUPPORTED_PATTERNS],
        f"... and {len(names) - MAX_UNSUPPORTED_PATTERNS} more",
    ]


def test_services_never_merge_even_with_identical_inputs(demo_v1: DiscoveryOutcome) -> None:
    other = discover(demo_request(service_id="other-payments"), demo_inputs(), DEMO_SERVICE)

    assert {e.service_id for e in other.result.endpoints} == {"other-payments"}
    assert not {e.id for e in other.result.endpoints} & {e.id for e in demo_v1.result.endpoints}
    assert by_key(other.result)[CHARGE].id == endpoint_id(V1, "other-payments", *CHARGE)


# Pinned Stripe --------------------------------------------------------------------------


def test_stripe_inventory_is_computed_from_the_pinned_document(stripe: tuple) -> None:
    outcome, raw, pin, elapsed = stripe
    base = urlsplit(raw["servers"][0]["url"]).path.rstrip("/")
    expected = {
        (method.upper(), f"{base}{path}")
        for path, item in raw["paths"].items()
        if not path.startswith("x-")
        for method in item
        if method in HTTP_METHODS
    }
    summary = summarize_inventory(outcome)

    assert {(e.method, e.path) for e in outcome.result.endpoints} == expected
    assert summary["endpoint_count"] == summary["documented_operation_count"] == len(expected)
    assert summary["by_reconciliation"]["not_compared"] == len(expected)
    assert sum(summary["by_method"].values()) == len(expected)
    (document,) = summary["documents"]
    assert document["info_version"] == raw["info"]["version"]
    assert document["sha256"] == pin["content"]["sha256"]
    assert document["openapi_version"] == raw["openapi"]
    assert document["operation_count"] == len(expected)
    assert document["revision_label"].endswith(f"@{pin['source']['commit'][:12]}")
    assert document["visibility"] == pin["visibility"] == "public"
    assert json.loads(json.dumps(summary)) == summary
    assert elapsed < 30


def test_stripe_endpoints_are_public_documented_and_not_compared(stripe: tuple) -> None:
    outcome, _raw, pin, _elapsed = stripe
    for endpoint in outcome.result.endpoints:
        assert endpoint.service_id == "stripe"
        assert (endpoint.reconciliation, endpoint.confidence) == ("not_compared", "direct")
        (evidence,) = endpoint.evidence
        assert (evidence.kind, evidence.visibility) == ("openapi", "public")
        assert evidence.revision == f"sha256:{pin['content']['sha256']}"
        assert evidence.source_uri == pin["source"]["raw_url"]
    assert coverage(outcome.result) == {
        "files_scanned": 0,
        "files_skipped": 0,
        "unsupported_patterns": [],
    }
    assert outcome.source_routes == ()


def test_stripe_never_absorbs_the_fictional_demo_change(
    stripe: tuple, demo_v1: DiscoveryOutcome
) -> None:
    outcome = stripe[0]
    stripe_ids = {e.id for e in outcome.result.endpoints}
    assert DECLARED_CHANGE["endpoint_id"] not in stripe_ids
    assert not stripe_ids & {e.id for e in demo_v1.result.endpoints}
    assert DECLARED_CHANGE["service_id"] != outcome.result.service_id


# Inventory ------------------------------------------------------------------------------


def test_inventory_summary_counts_are_computed_from_the_outcome(demo_v1: DiscoveryOutcome) -> None:
    summary = summarize_inventory(demo_v1)
    endpoints = demo_v1.result.endpoints

    assert (summary["service_id"], summary["snapshot_id"]) == (SERVICE_ID, V1)
    assert summary["endpoint_count"] == len(endpoints)
    assert summary["documented_operation_count"] == sum(
        1 for e in endpoints if any(item.kind == "openapi" for item in e.evidence)
    )
    assert summary["source_route_count"] == len(demo_v1.source_routes)
    assert summary["by_method"] == {"GET": len(endpoints)}
    assert list(summary["by_reconciliation"]) == list(RECONCILIATION_STATUSES)
    assert summary["by_reconciliation"] == {
        status: sum(1 for e in endpoints if e.reconciliation == status)
        for status in RECONCILIATION_STATUSES
    }
    assert summary["coverage"] == coverage(demo_v1.result)
    assert summary["warning_count"] == len(demo_v1.result.warnings)
    (document,) = summary["documents"]
    assert document["sha256"] == hashlib.sha256(SPEC_V1.read_bytes()).hexdigest()
    assert document["server_urls"] == [server["url"] for server in raw_spec(SPEC_V1)["servers"]]
    assert json.loads(json.dumps(summary)) == summary
