"""Reconciliation rules over hand-built records: namespaces, matching, statuses, evidence.

Records are constructed directly (no parser, no scanner), so each rule from PRODUCT.md is
pinned independently of how documents are parsed or how source is scanned.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import uuid
from typing import Literal

import pytest

from discovery.contracts import Endpoint
from discovery.ids import endpoint_id, evidence_id, fixture_uuid
from discovery.reconcile import (
    IMPLEMENTATION_ONLY_SCHEMA,
    MAX_EXCERPT_CHARS,
    SCHEMA_NOTE_KEY,
    SOURCE_ONLY_SCHEMA,
    SOURCE_TOKEN_KEY,
    UNKNOWN_SOURCE_REVISION,
    UNPARSEABLE_URI,
    Reconciliation,
    display_uri,
    documented_namespaces,
    in_namespace,
    merge_documented,
    openapi_evidence,
    reconcile,
    source_evidence,
)
from discovery.records import (
    DocumentedOperation,
    DocumentProvenance,
    ParsedDocument,
    SourceRoute,
    SourceScan,
    operation_pointer,
)
from discovery.text import has_unsafe_characters

SERVICE = "reconcile-test-service"
SNAPSHOT = fixture_uuid("test.discovery-reconcile.snapshot")
SOURCE_SNAPSHOT = fixture_uuid("test.discovery-reconcile.source-snapshot")


# Record builders -----------------------------------------------------------------------


def object_schema(*names: str) -> dict:
    return {"type": "object", "properties": {name: {"type": "string"} for name in names}}


def operation(
    method: str,
    path: str,
    *,
    summary: str | None = None,
    response: dict | None = None,
    operation_id: str | None = None,
) -> DocumentedOperation:
    return DocumentedOperation(
        method=method,
        path=path,
        documented_path=path,
        operation_id=operation_id,
        summary=summary,
        deprecated=False,
        request_schema=object_schema(),
        response_schema=response if response is not None else object_schema("id"),
        json_pointer=operation_pointer(path, method),
        document_ref="unset",
    )


def document(
    *operations: DocumentedOperation,
    ref: str = "spec.yaml",
    content: str = "spec content v1",
    visibility: Literal["public", "provider_owned"] = "provider_owned",
) -> ParsedDocument:
    provenance = DocumentProvenance(
        document_ref=ref,
        source_uri=f"specs/{ref}",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        byte_size=len(content),
        openapi_version="3.1.0",
        info_title="Reconcile test",
        info_version="1.0.0",
        revision_label=None,
        visibility=visibility,
        path_count=len({op.path for op in operations}),
        operation_count=len(operations),
        component_schema_count=0,
        server_urls=(),
    )
    return ParsedDocument(
        provenance=provenance,
        operations=[dataclasses.replace(op, document_ref=ref) for op in operations],
    )


def route(
    method: str,
    path: str,
    *,
    file: str = "svc/app.py",
    line: int = 1,
    handler: str | None = "handler",
    include_in_schema: bool | None = True,
    confidence: Literal["direct", "inferred"] = "direct",
) -> SourceRoute:
    parameters = tuple(
        (segment[1:-1], segment) for segment in path.split("/") if segment.startswith("{")
    )
    return SourceRoute(
        framework="fastapi",
        method=method,
        path=path,
        raw_path=path,
        param_mapping=parameters,
        file=file,
        line_start=line,
        line_end=line,
        handler=handler,
        excerpt=f'@app.{method.lower()}("{path}")',
        confidence=confidence,
        include_in_schema=include_in_schema,
    )


def scan(
    *routes: SourceRoute,
    files_scanned: int = 1,
    files_skipped: int = 0,
    unsupported: tuple[str, ...] = (),
) -> SourceScan:
    return SourceScan(
        routes=list(routes),
        files_scanned=files_scanned,
        files_skipped=files_skipped,
        unsupported_patterns=list(unsupported),
        warnings=[],
    )


def run(
    documents: list[ParsedDocument],
    source: SourceScan | None,
    *,
    gap: str | None = None,
    unresolved: tuple[str, ...] = (),
    service_id: str = SERVICE,
    source_snapshot_id: str | None = SOURCE_SNAPSHOT,
) -> Reconciliation:
    documented, merge_warnings = merge_documented(documents)
    result = reconcile(
        documented,
        source,
        service_id=service_id,
        snapshot_id=SNAPSHOT,
        source_snapshot_id=source_snapshot_id,
        documentation_gap=gap,
        unresolved_paths=unresolved,
    )
    return Reconciliation(result.endpoints, [*merge_warnings, *result.warnings])


def by_key(reconciliation: Reconciliation) -> dict[tuple[str, str], Endpoint]:
    endpoints = {
        (endpoint.method, endpoint.path): endpoint for endpoint in reconciliation.endpoints
    }
    assert len(endpoints) == len(reconciliation.endpoints), "endpoint identity must be unique"
    return endpoints


def statuses(reconciliation: Reconciliation) -> dict[tuple[str, str], str]:
    return {key: endpoint.reconciliation for key, endpoint in by_key(reconciliation).items()}


def kinds(endpoint: Endpoint) -> list[str]:
    return [item.kind for item in endpoint.evidence]


def warnings_with(reconciliation: Reconciliation, needle: str) -> list[str]:
    return [warning for warning in reconciliation.warnings if needle in warning]


# Namespaces -----------------------------------------------------------------------------


def test_documented_namespace_is_the_set_of_versioned_first_segments() -> None:
    paths = ["/v1/charges/{id}", "/v2/charges", "/healthz", "/version/x", "/v10", "/"]
    assert documented_namespaces(paths) == {"v1", "v2", "v10"}
    assert documented_namespaces(["/healthz", "/", "/V1/x", "/v1beta/x", "/{v}/x"]) == frozenset()
    # A prefix in front of the version (a server base path, or written in the path keys).
    assert documented_namespaces(["/api/v1/charges/{id}", "/healthz"]) == {"api"}

    versioned = frozenset({"v1"})
    assert in_namespace("/v1/anything/{id}", versioned)
    assert not in_namespace("/healthz", versioned)
    assert not in_namespace("/v2/charges", versioned)
    assert in_namespace("/healthz", frozenset())  # no versioned segment: the whole path space


def based(method: str, base: str, documented_path: str) -> DocumentedOperation:
    """An operation documented under ``documented_path`` behind the server base path ``base``."""
    return dataclasses.replace(operation(method, documented_path), path=f"{base}{documented_path}")


def test_a_server_base_path_is_a_documented_namespace_even_without_a_version() -> None:
    # servers: [{url: http://demo-payments.test/api}] with the path key /v1/charges/{id}:
    # a proxy prefix the app's own routes do not carry.
    source = scan(
        route("GET", "/v1/charges/{id}"),
        route("GET", "/v1/charges/{id}/receipt", line=2),
        route("GET", "/healthz", line=3),
        route("GET", "/api/v1/charges/{id}/refunds", line=4),
    )
    result = run([document(based("GET", "/api", "/v1/charges/{id}"))], source)

    assert statuses(result) == {
        ("GET", "/api/v1/charges/{id}"): "documented_not_found_in_scan",
        ("GET", "/v1/charges/{id}"): "not_compared",
        ("GET", "/v1/charges/{id}/receipt"): "not_compared",
        ("GET", "/healthz"): "not_compared",
        ("GET", "/api/v1/charges/{id}/refunds"): "implementation_only",
    }
    (outside,) = warnings_with(result, "outside the documented namespace (/api)")
    assert outside.startswith("3 source route(s)")

    # An unversioned API behind a base path: the base path is still the namespace.
    unversioned = run(
        [document(based("GET", "/api", "/charges/{id}"))],
        scan(route("GET", "/charges/{id}"), route("GET", "/api/other", line=2)),
    )
    assert statuses(unversioned) == {
        ("GET", "/api/charges/{id}"): "documented_not_found_in_scan",
        ("GET", "/charges/{id}"): "not_compared",
        ("GET", "/api/other"): "implementation_only",
    }
    assert warnings_with(unversioned, "outside the documented namespace (/api)")


# Status rules ---------------------------------------------------------------------------


def test_each_status_follows_the_namespace_and_scan_rules() -> None:
    documents = [document(operation("GET", "/v1/things/{id}"), operation("GET", "/v1/gone/{id}"))]
    source = scan(
        route("GET", "/v1/things/{id}", line=3),
        route("POST", "/v1/extra", line=7),
        route("GET", "/healthz", line=11),
        route("GET", "/v2/things/{id}", line=15),
    )
    result = run(documents, source)

    assert statuses(result) == {
        ("GET", "/v1/things/{id}"): "documented_and_found",
        ("GET", "/v1/gone/{id}"): "documented_not_found_in_scan",
        ("POST", "/v1/extra"): "implementation_only",
        ("GET", "/healthz"): "not_compared",
        ("GET", "/v2/things/{id}"): "not_compared",
    }
    endpoints = by_key(result)
    assert kinds(endpoints["GET", "/v1/things/{id}"]) == ["openapi", "source"]
    assert kinds(endpoints["GET", "/v1/gone/{id}"]) == ["openapi"]
    assert kinds(endpoints["POST", "/v1/extra"]) == ["source"]
    assert kinds(endpoints["GET", "/healthz"]) == ["source"]
    assert {endpoint.confidence for endpoint in result.endpoints} == {"direct"}
    assert [(endpoint.path, endpoint.method) for endpoint in result.endpoints] == sorted(
        (endpoint.path, endpoint.method) for endpoint in result.endpoints
    )

    (outside,) = warnings_with(result, "outside the documented namespace (/v1)")
    assert "GET /healthz (svc/app.py:11)" in outside
    assert "GET /v2/things/{id} (svc/app.py:15)" in outside
    # A complete scan (no unsupported patterns, no skipped files) needs no caveat.
    assert not warnings_with(result, "may explain")


def test_a_documented_route_outside_the_versioned_namespace_can_still_be_found() -> None:
    documents = [document(operation("GET", "/v1/things/{id}"), operation("GET", "/healthz"))]
    result = run(documents, scan(route("GET", "/healthz"), route("GET", "/metrics", line=2)))

    assert statuses(result) == {
        ("GET", "/healthz"): "documented_and_found",
        ("GET", "/v1/things/{id}"): "documented_not_found_in_scan",
        ("GET", "/metrics"): "not_compared",
    }


def test_matching_ignores_parameter_names_but_reports_the_difference() -> None:
    documents = [
        document(
            operation("GET", "/v1/things/{thing}"),
            operation("GET", "/v1/orgs/{org}/members/{member}"),
        )
    ]
    source = scan(
        route("GET", "/v1/things/{id}", line=4),
        route("GET", "/v1/orgs/{org}/members/{id}", line=9),
    )
    result = run(documents, source)

    thing = by_key(result)["GET", "/v1/things/{thing}"]  # the documented name is the identity
    assert thing.reconciliation == "documented_and_found"
    assert thing.id == endpoint_id(SNAPSHOT, SERVICE, "GET", "/v1/things/{thing}")
    assert kinds(thing) == ["openapi", "source"]
    assert thing.evidence[1].line_start == 4
    (thing_warning,) = warnings_with(result, "GET /v1/things/{thing}: ")
    assert (
        "parameter names differ: documented {thing} vs source {id} (svc/app.py:4)" in thing_warning
    )
    (member_warning,) = warnings_with(result, "GET /v1/orgs/{org}/members/{member}: ")
    assert "documented {member} vs source {id}" in member_warning  # only the differing name


def test_unscanned_source_leaves_every_documented_operation_not_compared() -> None:
    result = run(
        [document(operation("GET", "/v1/things/{id}"), operation("POST", "/v1/things"))], None
    )

    assert set(statuses(result).values()) == {"not_compared"}
    assert all(kinds(endpoint) == ["openapi"] for endpoint in result.endpoints)
    assert result.warnings == []


def test_a_scan_that_read_no_files_is_not_a_comparison() -> None:
    result = run(
        [document(operation("GET", "/v1/things/{id}"))], scan(files_scanned=0, files_skipped=2)
    )

    assert statuses(result) == {("GET", "/v1/things/{id}"): "not_compared"}
    assert warnings_with(result, "read no files")


def test_an_incomplete_scan_may_explain_documented_routes_not_found() -> None:
    documents = [document(operation("GET", "/v1/a"), operation("GET", "/v1/b"))]
    source = scan(
        route("GET", "/v1/c"), unsupported=("fastapi:dynamic-prefix svc/b.py:3",), files_skipped=1
    )
    result = run(documents, source)

    assert statuses(result)["GET", "/v1/a"] == "documented_not_found_in_scan"
    assert statuses(result)["GET", "/v1/b"] == "documented_not_found_in_scan"
    (caveat,) = warnings_with(result, "may explain unmatched documented routes")
    assert caveat.startswith("2 documented operation(s) were not found in the scanned source")
    assert "1 unsupported pattern(s)" in caveat and "skipped 1 file(s)" in caveat
    assert "not proof of absence" in caveat


# Conflicts ------------------------------------------------------------------------------


def test_one_route_registered_by_different_handlers_is_a_conflict() -> None:
    documents = [document(operation("GET", "/v1/widgets/{widget}"))]
    source = scan(
        route("GET", "/v1/widgets/{id}", file="svc/a.py", line=5, handler="get_widget"),
        route("GET", "/v1/widgets/{widget_id}", file="svc/b.py", line=8, handler="fetch_widget"),
        route("GET", "/v1/gadgets/{id}", file="svc/a.py", line=9, handler="get_gadget"),
        route("GET", "/v1/gadgets/{gadget_id}", file="svc/b.py", line=12, handler="fetch_gadget"),
    )
    result = run(documents, source)
    endpoints = by_key(result)

    widget = endpoints["GET", "/v1/widgets/{widget}"]
    assert widget.reconciliation == "conflict"
    assert kinds(widget) == ["openapi", "source", "source"]
    assert [item.file for item in widget.evidence[1:]] == ["svc/a.py", "svc/b.py"]

    gadget = endpoints["GET", "/v1/gadgets/{gadget_id}"]  # source-only: first path in sort order
    assert gadget.reconciliation == "conflict"
    assert [(item.file, item.line_start) for item in gadget.evidence] == [
        ("svc/b.py", 12),
        ("svc/a.py", 9),
    ]
    conflicts = warnings_with(result, "is registered by more than one handler in source")
    assert len(conflicts) == 2
    assert any("svc/a.py:5 get_widget; svc/b.py:8 fetch_widget" in warning for warning in conflicts)


def test_the_same_handler_registered_twice_is_not_a_conflict() -> None:
    source = scan(
        route("GET", "/v1/items/{id}", line=3, handler="get_item"),
        route("GET", "/v1/items/{item_id}", line=4, handler="get_item"),
    )
    result = run([document(operation("GET", "/v1/other"))], source)

    item = by_key(result)["GET", "/v1/items/{id}"]
    assert item.reconciliation == "implementation_only"
    assert [item.line_start for item in item.evidence] == [3, 4]
    assert warnings_with(result, "registered more than once by the same handler")


def test_unnamed_handlers_are_never_assumed_to_be_the_same() -> None:
    source = scan(
        route("GET", "/v1/items", line=3, handler=None),
        route("GET", "/v1/items", line=4, handler=None),
    )
    result = run([document(operation("GET", "/v1/other"))], source)

    assert statuses(result)["GET", "/v1/items"] == "conflict"


def test_documented_templates_with_the_same_shape_are_a_conflict() -> None:
    documents = [document(operation("GET", "/v1/x/{a}"), operation("GET", "/v1/x/{b}"))]
    result = run(documents, scan(route("GET", "/v1/x/{id}")))

    assert statuses(result) == {("GET", "/v1/x/{a}"): "conflict", ("GET", "/v1/x/{b}"): "conflict"}
    assert all(kinds(endpoint) == ["openapi", "source"] for endpoint in result.endpoints)
    assert warnings_with(result, "same path template")


def test_documents_merge_on_method_and_path_and_flag_schema_differences() -> None:
    first = document(
        operation("GET", "/v1/things/{id}", response=object_schema("id")),
        operation("GET", "/v1/stable"),
        ref="a.yaml",
        content="document a",
    )
    second = document(
        operation("GET", "/v1/things/{id}", response=object_schema("id", "amount")),
        operation("GET", "/v1/stable"),
        ref="b.yaml",
        content="document b",
    )
    result = run([first, second], None)
    endpoints = by_key(result)

    things = endpoints["GET", "/v1/things/{id}"]
    assert things.reconciliation == "conflict"
    assert things.response_schema == object_schema("id")  # the first document wins
    assert [item.source_uri for item in things.evidence] == ["specs/a.yaml", "specs/b.yaml"]
    stable = endpoints["GET", "/v1/stable"]
    assert stable.reconciliation == "not_compared"
    assert len(stable.evidence) == 2
    assert warnings_with(result, "GET /v1/things/{id} is documented more than once")[0].endswith(
        "their schemas differ, so the endpoint is marked conflict"
    )
    assert warnings_with(result, "GET /v1/stable is documented more than once")[0].endswith(
        "their schemas match"
    )


def test_schema_comparison_ignores_component_names_but_not_content_or_json_types() -> None:
    def inlined(origin: str, flag: object) -> dict:
        return {
            "type": "object",
            "properties": {"live": {"type": "boolean", "default": flag}},
            "x-shockwave-origin": origin,
        }

    def merged(first: dict, second: dict) -> str:
        documents = [
            document(operation("GET", "/v1/x", response=first), ref="a.yaml", content="a"),
            document(operation("GET", "/v1/x", response=second), ref="b.yaml", content="b"),
        ]
        (endpoint,) = run(documents, None).endpoints
        return endpoint.reconciliation

    base = inlined("/components/schemas/Thing", True)
    assert merged(base, inlined("/components/schemas/ThingV1", True)) == "not_compared"
    assert merged(base, inlined("/components/schemas/Thing", 1)) == "conflict"  # true is not 1
    assert merged(base, inlined("/components/schemas/Thing", False)) == "conflict"


def test_byte_identical_documents_add_no_duplicate_evidence() -> None:
    ops = (operation("GET", "/v1/things/{id}"),)
    result = run(
        [
            document(*ops, ref="a.yaml", content="same"),
            document(*ops, ref="b.yaml", content="same"),
        ],
        None,
    )

    (things,) = result.endpoints
    assert len(things.evidence) == 1
    assert things.reconciliation == "not_compared"
    assert warnings_with(result, "openapi document b.yaml has the same content (sha256) as a.yaml")
    assert not warnings_with(result, "documented more than once")


# Documentation gaps ---------------------------------------------------------------------


def test_unread_documents_keep_unmatched_source_routes_not_compared() -> None:
    gap = "1 requested OpenAPI document(s) could not be read"
    source = scan(route("GET", "/v1/things/{id}"), route("GET", "/v1/unmatched", line=2))
    result = run([document(operation("GET", "/v1/things/{id}"))], source, gap=gap)

    assert statuses(result) == {
        ("GET", "/v1/things/{id}"): "documented_and_found",
        ("GET", "/v1/unmatched"): "not_compared",
    }
    unmatched = by_key(result)["GET", "/v1/unmatched"]
    assert unmatched.response_schema == {SCHEMA_NOTE_KEY: SOURCE_ONLY_SCHEMA}
    (warning,) = warnings_with(result, "not classified as implementation-only")
    assert gap in warning and "GET /v1/unmatched (svc/app.py:2)" in warning


def test_with_no_documentation_requested_nothing_is_called_implementation_only() -> None:
    source = scan(route("GET", "/v1/things/{id}"), route("GET", "/healthz", line=2))
    result = run([], source, gap="no OpenAPI document was requested")
    assert set(statuses(result).values()) == {"not_compared"}

    # A document that was read but documents nothing is a real comparison.
    empty = run([document()], source)
    assert set(statuses(empty).values()) == {"implementation_only"}


def test_a_route_on_a_documented_path_with_an_unresolved_path_item_is_not_compared() -> None:
    # The document lists /v1/charges/{id}, but its path item is a remote $ref: the path is
    # documented and its operations are unknown, which is not "undocumented".
    source = scan(
        route("GET", "/v1/charges/{charge}", line=2),
        route("POST", "/v1/charges/{id}", line=3),  # any method: the operations are unknown
        route("GET", "/v1/charges/{id}/receipt", line=4),  # a path the document never lists
        route("GET", "/healthz", line=5),
    )
    result = run(
        [document(operation("GET", "/v1/refunds/{id}"))], source, unresolved=("/v1/charges/{id}",)
    )

    assert statuses(result) == {
        ("GET", "/v1/refunds/{id}"): "documented_not_found_in_scan",
        ("GET", "/v1/charges/{charge}"): "not_compared",
        ("POST", "/v1/charges/{id}"): "not_compared",
        ("GET", "/v1/charges/{id}/receipt"): "implementation_only",
        ("GET", "/healthz"): "not_compared",
    }
    charge = by_key(result)["GET", "/v1/charges/{charge}"]
    assert charge.response_schema == {SCHEMA_NOTE_KEY: SOURCE_ONLY_SCHEMA}
    (warning,) = warnings_with(result, "path item could not be resolved")
    assert warning.startswith("2 source route(s) match a documented path")
    assert "GET /v1/charges/{charge} (svc/app.py:2)" in warning
    assert "POST /v1/charges/{id} (svc/app.py:3)" in warning
    assert "not classified as implementation-only" in warning


def test_when_every_path_item_is_unresolved_the_namespace_still_comes_from_the_document() -> None:
    # A split spec whose every path item is a remote $ref parses to zero operations.
    source = scan(
        route("GET", "/v1/charges/{id}"),
        route("GET", "/v1/charges/{id}/receipt", line=2),
        route("GET", "/healthz", line=3),
    )
    result = run([document()], source, unresolved=("/v1/charges/{id}", "/v1/refunds/{id}"))

    assert statuses(result) == {
        ("GET", "/v1/charges/{id}"): "not_compared",
        ("GET", "/v1/charges/{id}/receipt"): "implementation_only",
        ("GET", "/healthz"): "not_compared",
    }
    assert warnings_with(result, "outside the documented namespace (/v1)")

    # Behind a server base path (/api/v1/...), as the parser joins it.
    behind = run([document()], source, unresolved=("/api/v1/charges/{id}",))
    assert set(statuses(behind).values()) == {"not_compared"}
    assert warnings_with(behind, "outside the documented namespace (/api)")


# Evidence and schemas -------------------------------------------------------------------


def test_openapi_evidence_is_content_addressed_and_bounded() -> None:
    summary = "Retrieve\na thing\x00" + " with detail" * 60
    parsed = document(
        operation("GET", "/v1/things/{id}", summary=summary), visibility="public", content="pinned"
    )
    (op,) = parsed.operations
    evidence = openapi_evidence(op, parsed.provenance)
    sha = hashlib.sha256(b"pinned").hexdigest()

    assert evidence.id == evidence_id("openapi", sha, "/paths/~1v1~1things~1{id}/get")
    assert (evidence.kind, evidence.visibility) == ("openapi", "public")
    assert (evidence.source_uri, evidence.revision) == ("specs/spec.yaml", f"sha256:{sha}")
    assert evidence.json_pointer == op.json_pointer
    assert (evidence.file, evidence.line_start, evidence.line_end) == (None, None, None)
    assert evidence.excerpt is not None
    assert evidence.excerpt.startswith("GET /v1/things/{id} - Retrieve a thing with detail")
    assert len(evidence.excerpt) <= MAX_EXCERPT_CHARS
    assert evidence.excerpt.isprintable()

    without_summary = openapi_evidence(dataclasses.replace(op, summary="  "), parsed.provenance)
    assert without_summary.excerpt == "GET /v1/things/{id}"


def test_source_evidence_names_the_snapshot_or_says_the_revision_is_unknown() -> None:
    registration = route("GET", "/v1/things/{id}", file="svc/things.py", line=12)
    pinned = source_evidence(registration, SOURCE_SNAPSHOT)
    unpinned = source_evidence(registration, None)

    assert pinned.source_uri == f"source-snapshot://{SOURCE_SNAPSHOT}/svc/things.py"
    assert pinned.revision == SOURCE_SNAPSHOT
    assert pinned.id == evidence_id(
        "source", SOURCE_SNAPSHOT, "svc/things.py", 12, "GET", "/v1/things/{id}"
    )
    assert unpinned.source_uri == "source-root://svc/things.py"
    assert unpinned.revision == UNKNOWN_SOURCE_REVISION == "unknown-source-revision"
    assert unpinned.id == evidence_id(
        "source", "unknown", "svc/things.py", 12, "GET", "/v1/things/{id}"
    )
    for item in (pinned, unpinned):
        assert (item.kind, item.visibility) == ("source", "provider_owned")
        assert (item.file, item.line_start, item.line_end) == ("svc/things.py", 12, 12)
        assert item.excerpt == '@app.get("/v1/things/{id}")'
        assert item.json_pointer is None


def test_source_only_schemas_say_unknown_and_are_never_shared() -> None:
    source = scan(
        route("GET", "/v1/a"), route("GET", "/v1/b", line=2), route("GET", "/healthz", line=3)
    )
    result = run([document(operation("GET", "/v1/documented"))], source)
    endpoints = by_key(result)

    unknown_request = {
        "type": "object",
        "properties": {},  # no path parameters: still unknown, not "no inputs"
        SCHEMA_NOTE_KEY: IMPLEMENTATION_ONLY_SCHEMA,
    }
    for key in (("GET", "/v1/a"), ("GET", "/v1/b")):
        assert endpoints[key].request_schema == unknown_request
        assert endpoints[key].response_schema == {SCHEMA_NOTE_KEY: IMPLEMENTATION_ONLY_SCHEMA}
        assert endpoints[key].operation_id is None
    assert endpoints["GET", "/healthz"].request_schema[SCHEMA_NOTE_KEY] == SOURCE_ONLY_SCHEMA
    assert endpoints["GET", "/healthz"].response_schema == {SCHEMA_NOTE_KEY: SOURCE_ONLY_SCHEMA}

    endpoints["GET", "/v1/a"].response_schema.clear()
    endpoints["GET", "/v1/a"].request_schema["properties"]["x"] = {}
    assert endpoints["GET", "/v1/b"].response_schema == {
        SCHEMA_NOTE_KEY: IMPLEMENTATION_ONLY_SCHEMA
    }
    assert endpoints["GET", "/v1/b"].request_schema == unknown_request


def test_source_only_request_schemas_list_the_path_parameters_and_raw_paths_are_kept() -> None:
    mounted = dataclasses.replace(  # Express: app.use('/v1/accounts/:acct', router)
        route("GET", "/v1/accounts/{acct}/charges/{chargeId}", file="app.js", line=4),
        framework="express",
        raw_path="/v1/accounts/:acct/charges/:chargeId",
        param_mapping=(("acct", ":acct"), ("chargeId", ":chargeId")),
        excerpt="router.get('/charges/:chargeId', (req, res) => res.json({}));",
    )
    converted = dataclasses.replace(  # FastAPI path converters
        route("GET", "/v1/items/{item_id}/parts/{part_path}", line=8),
        raw_path="/v1/items/{item_id:int}/parts/{part_path:path}",
        param_mapping=(("item_id", "{item_id:int}"), ("part_path", "{part_path:path}")),
    )
    result = run([document(operation("GET", "/v1/documented"))], scan(mounted, converted))
    endpoints = by_key(result)

    account = endpoints["GET", "/v1/accounts/{acct}/charges/{chargeId}"]
    assert account.reconciliation == "implementation_only"
    assert account.request_schema == {
        "type": "object",
        SCHEMA_NOTE_KEY: IMPLEMENTATION_ONLY_SCHEMA,  # unknown, not "no parameters"
        "properties": {
            "path": {
                "type": "object",
                "properties": {
                    "acct": {SOURCE_TOKEN_KEY: ":acct"},
                    "chargeId": {SOURCE_TOKEN_KEY: ":chargeId"},
                },
                "required": ["acct", "chargeId"],
            }
        },
        "required": ["path"],
    }
    (evidence,) = account.evidence
    assert evidence.excerpt == (
        "raw path /v1/accounts/:acct/charges/:chargeId;"
        " router.get('/charges/:chargeId', (req, res) => res.json({}));"
    )
    assert evidence.id == evidence_id(  # the note never changes evidence identity
        "source", SOURCE_SNAPSHOT, "app.js", 4, "GET", "/v1/accounts/{acct}/charges/{chargeId}"
    )

    items = endpoints["GET", "/v1/items/{item_id}/parts/{part_path}"]
    assert items.request_schema["properties"]["path"]["properties"] == {
        "item_id": {SOURCE_TOKEN_KEY: "{item_id:int}"},
        "part_path": {SOURCE_TOKEN_KEY: "{part_path:path}"},
    }
    assert items.evidence[0].excerpt == (
        "raw path /v1/items/{item_id:int}/parts/{part_path:path};"
        ' @app.get("/v1/items/{item_id}/parts/{part_path}")'
    )


def test_route_confidence_and_hidden_schema_flags_are_preserved() -> None:
    source = scan(
        route("GET", "/v1/inferred", confidence="inferred"),
        route("GET", "/v1/hidden", line=2, include_in_schema=False),
    )
    result = run([document(operation("GET", "/v1/documented"))], source)

    assert by_key(result)["GET", "/v1/inferred"].confidence == "inferred"
    assert by_key(result)["GET", "/v1/hidden"].reconciliation == "implementation_only"
    (hidden,) = warnings_with(result, "include_in_schema=False")
    assert hidden.startswith("GET /v1/hidden (svc/app.py:2)")


def test_routes_without_a_contract_representation_are_ignored_with_a_warning() -> None:
    result = run(
        [document(operation("GET", "/v1/documented"))],
        scan(route("CONNECT", "/v1/tunnel"), route("GET", "relative", line=2)),
    )

    assert set(by_key(result)) == {("GET", "/v1/documented")}
    assert len(warnings_with(result, "has no contract representation and was ignored")) == 2


# Identity, purity, text -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (
            "https://ci-bot:SECRET-1@specs.example/payments.yaml?access_token=SECRET-2#SECRET-3",
            "https://specs.example/payments.yaml",
        ),
        ("https://user@[::1]:8443/spec.json?sig=SECRET", "https://[::1]:8443/spec.json"),
        ("s3://bucket/spec.yaml?X-Amz-Signature=SECRET", "s3://bucket/spec.yaml"),
        ("//user:SECRET@specs.example/spec.yaml", "//specs.example/spec.yaml"),
        ("http://u:p%40SECRET@host:8080/spec.yaml", "http://host:8080/spec.yaml"),
        ("https://u:SECRET@[::1/spec.yaml", UNPARSEABLE_URI),  # unparseable: never echoed
    ],
)
def test_display_uri_drops_userinfo_query_and_fragment(value: str, shown: str) -> None:
    assert display_uri(value) == shown


@pytest.mark.parametrize(
    "value",
    [
        "https://raw.githubusercontent.com/stripe/openapi/0123abcd/openapi/spec3.json",
        "examples/provider/demo_payments/openapi/demo-payments.v1.yaml",
        "stripe/openapi:latest/openapi.spec3.json",
        "demo-payments.v1.yaml",
        "fixture://demo",
        "spec.yaml?not-a-url=1",  # no host: not a URL, left as written
    ],
)
def test_display_uri_leaves_values_without_credentials_or_a_host_unchanged(value: str) -> None:
    assert display_uri(value) == value


def test_document_uris_with_credentials_never_reach_evidence_or_warnings() -> None:
    def fetched(name: str, op: DocumentedOperation, content: str) -> ParsedDocument:
        uri = f"https://ci-bot:SECRET-1@specs.example/{name}.yaml?access_token=SECRET-2#SECRET-3"
        parsed = document(op, ref=uri, content=content)
        parsed.provenance = dataclasses.replace(parsed.provenance, source_uri=uri)
        return parsed

    things = operation("GET", "/v1/things/{id}")
    reworded = operation("GET", "/v1/things/{id}", summary="reworded")
    result = run(
        [fetched("a", things, "same"), fetched("b", things, "same"), fetched("c", reworded, "c")],
        None,
    )

    (endpoint,) = result.endpoints
    assert [item.source_uri for item in endpoint.evidence] == [
        "https://specs.example/a.yaml",
        "https://specs.example/c.yaml",
    ]
    assert endpoint.evidence[0].id == evidence_id(  # identity is the content, never the URI
        "openapi", hashlib.sha256(b"same").hexdigest(), "/paths/~1v1~1things~1{id}/get"
    )
    assert warnings_with(
        result,
        "openapi document https://specs.example/b.yaml has the same content (sha256) as"
        " https://specs.example/a.yaml",
    )
    (twice,) = warnings_with(result, "is documented more than once")
    assert "(https://specs.example/a.yaml /paths/" in twice
    assert "; https://specs.example/c.yaml /paths/" in twice
    shown = [*result.warnings, *(item.source_uri for item in endpoint.evidence)]
    assert not [text for text in shown if "SECRET" in text]


def test_ids_may_be_uuid_objects() -> None:
    documents = [document(operation("GET", "/v1/things/{id}"))]
    source = scan(route("GET", "/v1/things/{id}"), route("GET", "/v1/extra", line=2))
    documented, _ = merge_documented(documents)
    as_uuids = reconcile(
        documented,
        source,
        service_id=SERVICE,
        snapshot_id=uuid.UUID(SNAPSHOT),
        source_snapshot_id=uuid.UUID(SOURCE_SNAPSHOT),
    )

    assert [endpoint.model_dump(mode="json") for endpoint in as_uuids.endpoints] == [
        endpoint.model_dump(mode="json") for endpoint in run(documents, source).endpoints
    ]
    revisions = {
        item.revision for e in as_uuids.endpoints for item in e.evidence if item.kind == "source"
    }
    assert revisions == {SOURCE_SNAPSHOT}
    pinned = source_evidence(route("GET", "/v1/x"), uuid.UUID(SOURCE_SNAPSHOT))
    assert (pinned.revision, pinned.source_uri) == (
        SOURCE_SNAPSHOT,
        f"source-snapshot://{SOURCE_SNAPSHOT}/svc/app.py",
    )


def test_services_never_merge_even_when_paths_are_identical() -> None:
    documents = [document(operation("GET", "/v1/charges/{id}"))]
    source = scan(route("GET", "/v1/charges/{id}"))
    first = run(documents, source, service_id="demo-payments")
    second = run(documents, source, service_id="stripe")

    assert {endpoint.service_id for endpoint in first.endpoints} == {"demo-payments"}
    assert {endpoint.service_id for endpoint in second.endpoints} == {"stripe"}
    assert not {endpoint.id for endpoint in first.endpoints} & {
        endpoint.id for endpoint in second.endpoints
    }


def test_reconciliation_is_deterministic_and_never_mutates_its_inputs() -> None:
    documents = [
        document(
            operation("GET", "/v1/things/{thing}"), operation("GET", "/v1/gone"), ref="a.yaml"
        ),
        document(operation("GET", "/v1/things/{thing}"), ref="b.yaml", content="other"),
    ]
    routes = [
        route("GET", "/healthz", line=1),
        route("GET", "/v1/things/{id}", line=2),
        route("GET", "/v1/extra", file="svc/z.py", line=3),
        route("GET", "/v1/extra", file="svc/y.py", line=4, handler="other"),
    ]
    source = scan(*routes, unsupported=("fastapi:dynamic-path svc/app.py:9",))
    snapshot = copy.deepcopy((documents, source))

    first = run(documents, source)
    # Document order is meaningful (the first wins); route order from a scanner is not.
    second = run(documents, scan(*reversed(routes), unsupported=tuple(source.unsupported_patterns)))

    assert (documents, source) == snapshot
    assert [endpoint.model_dump(mode="json") for endpoint in first.endpoints] == [
        endpoint.model_dump(mode="json") for endpoint in second.endpoints
    ]
    assert first.warnings == second.warnings
    assert len(first.warnings) == len(set(first.warnings))


def test_untrusted_text_in_warnings_and_excerpts_goes_through_discovery_text() -> None:
    # Built with chr() so no invisible character sits in this source file.
    unsafe = "".join(chr(code) for code in (0x07, 0x9B, 0x200B, 0x2028, 0x202E, 0x2066, 0xFEFF))
    documents = [document(operation("GET", "/v1/things/{id}", summary=f"Get{unsafe}it"))]
    source = scan(
        route("GET", "/v1/things/{thing}", file="svc/app.py", line=2),
        route("GET", "/v1/extra", line=3, handler=f"h{unsafe}1"),
        route("GET", "/v1/extra", line=4, handler=f"h{unsafe}2"),
    )
    result = run(documents, source)

    texts = [*result.warnings]
    texts += [item.excerpt or "" for endpoint in result.endpoints for item in endpoint.evidence]
    assert texts and all(not has_unsafe_characters(text) and text.isprintable() for text in texts)
    (openapi, _source) = by_key(result)["GET", "/v1/things/{id}"].evidence
    assert openapi.excerpt == "GET /v1/things/{id} - Get it"
    (conflict,) = warnings_with(result, "registered by more than one handler")
    assert "svc/app.py:3 h 1; svc/app.py:4 h 2" in conflict

    long_route = dataclasses.replace(route("GET", "/v1/long"), excerpt="x" * 500)
    clipped = source_evidence(long_route, SOURCE_SNAPSHOT).excerpt
    assert clipped is not None and len(clipped) == MAX_EXCERPT_CHARS and clipped.endswith("...")
