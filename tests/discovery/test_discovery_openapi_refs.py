"""``discovery.openapi`` references and reporting: 3.1 ``$ref`` siblings, target
fingerprints (``REF_DIGEST_KEY``), unresolvable path items, relative server URLs, warning
and pattern overflow counts, and sanitized warning text.

Documents are built in memory (data only).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from analyzer.changes import detect_changes
from analyzer.fieldpaths import field_schema
from discovery import openapi
from discovery.contracts import DiscoveryInput, DiscoveryResult
from discovery.openapi import (
    MAX_UNSUPPORTED_PATTERNS,
    MAX_WARNINGS,
    parse_openapi_document,
)
from discovery.records import (
    ORIGIN_KEY,
    PATTERN_UNRESOLVED_PATH_ITEM,
    REF_DIGEST_KEY,
    REF_STATUS_KEY,
    ExpansionLimits,
    FetchedDocument,
    ParsedDocument,
    contract_gaps,
)
from discovery.service import discover
from discovery.text import has_unsafe_characters

STATUS = "x-shockwave-status"
CONTENT_TYPE = "x-shockwave-content-type"
DIGEST = re.compile(r"sha256:[0-9a-f]{32}")
API_PROJECT = "55555555-5555-4555-8555-555555555555"


def fetched(data: bytes, name: str = "inline") -> FetchedDocument:
    return FetchedDocument(content=data, source_uri=f"fixture://{name}")


def parse_doc(document: dict, name: str = "inline", **kwargs: Any) -> ParsedDocument:
    return parse_openapi_document(name, fetched(json.dumps(document).encode(), name), **kwargs)


def ok(schema: Any) -> dict:
    return {"200": {"description": "ok", "content": {"application/json": {"schema": schema}}}}


def response_document(schema: Any, schemas: dict, version: str = "3.1.0") -> dict:
    return {
        "openapi": version,
        "info": {"title": "refs", "version": "1"},
        "paths": {"/v1/charges/{id}": {"get": {"operationId": "getCharge", "responses": ok(schema)}}},
        "components": {"schemas": schemas},
    }


def response_of(document: dict, name: str = "inline") -> dict:
    (operation,) = parse_doc(document, name).operations
    return operation.response_schema


def without_digests(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_digests(child) for key, child in value.items() if key != REF_DIGEST_KEY}
    if isinstance(value, list):
        return [without_digests(child) for child in value]
    return value


def all_markers(value: Any) -> list[dict]:
    found, stack = [], [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if REF_STATUS_KEY in current:
                found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


# OpenAPI 3.1 $ref siblings -------------------------------------------------------------------


BASE = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "currency": {"type": "string"}},
    "required": ["id", "currency"],
}
BASE_INLINED = {**BASE, ORIGIN_KEY: "/components/schemas/Base"}


def test_31_constraining_siblings_apply_in_addition_to_the_target() -> None:
    schema = {
        "$ref": "#/components/schemas/Base",
        "description": "A charge.",
        "properties": {"amount": {"type": "integer"}},
        "required": ["amount"],
    }
    response = response_of(response_document(schema, {"Base": BASE}))
    # Nothing merged, nothing lost: the siblings stay put and the target joins as allOf.
    assert without_digests(response) == {
        "description": "A charge.",
        "properties": {"amount": {"type": "integer"}},
        "required": ["amount"],
        "allOf": [BASE_INLINED],
        STATUS: "200",
        CONTENT_TYPE: "application/json",
    }
    assert ORIGIN_KEY not in response  # sibling fields are not attributed to the target
    assert DIGEST.fullmatch(response["allOf"][0][REF_DIGEST_KEY])
    for field in ("response.id", "response.currency", "response.amount"):
        assert field_schema({}, response, field) is not None, field


@pytest.mark.parametrize(
    ("siblings", "expected"),
    [
        ({"required": ["id"]}, {"required": ["id"], "allOf": [BASE_INLINED]}),
        (  # a sibling allOf is flattened into the same list
            {"allOf": [{"properties": {"y": {"type": "string"}}}]},
            {"allOf": [BASE_INLINED, {"properties": {"y": {"type": "string"}}}]},
        ),
        ({"type": ["object", "null"]}, {"type": ["object", "null"], "allOf": [BASE_INLINED]}),
    ],
)
def test_31_sibling_shapes(siblings: dict, expected: dict) -> None:
    response = response_of(response_document({"$ref": "#/components/schemas/Base", **siblings}, {"Base": BASE}))
    assert without_digests(response) == {**expected, STATUS: "200", CONTENT_TYPE: "application/json"}


def test_31_ref_chain_stops_at_a_target_with_constraining_siblings() -> None:
    schemas = {
        "Alias": {"$ref": "#/components/schemas/Base", "properties": {"b": {"type": "string"}}},
        "Base": BASE,
    }
    response = response_of(response_document({"$ref": "#/components/schemas/Alias", "title": "Outer"}, schemas))
    assert without_digests(response) == {
        "properties": {"b": {"type": "string"}},
        "allOf": [BASE_INLINED],
        "title": "Outer",  # an annotation sibling still overrides
        ORIGIN_KEY: "/components/schemas/Alias",
        STATUS: "200",
        CONTENT_TYPE: "application/json",
    }


def test_31_siblings_of_an_unexpandable_reference_are_kept() -> None:
    schema = {"$ref": "./common.yaml#/Base", "properties": {"amount": {"type": "integer"}}}
    response = response_of(response_document(schema, {}))
    assert response["properties"] == {"amount": {"type": "integer"}}
    assert response["allOf"] == [{"$ref": "./common.yaml#/Base", REF_STATUS_KEY: "remote_unsupported"}]


def test_30_still_ignores_ref_siblings() -> None:
    schema = {"$ref": "#/components/schemas/Base", "properties": {"amount": {"type": "integer"}}}
    response = response_of(response_document(schema, {"Base": BASE}, version="3.0.3"))
    assert without_digests(response) == {**BASE_INLINED, STATUS: "200", CONTENT_TYPE: "application/json"}


def discovered(document: dict, snapshot: str) -> DiscoveryResult:
    request = DiscoveryInput(service_id="refs", snapshot_id=snapshot, openapi_documents=["doc.json"],
                             source_snapshot_id=None, frameworks=[])
    return discover(request, {"doc.json": json.dumps(document).encode()}, None).result


def test_31_extends_idiom_compares_like_the_inline_contract() -> None:
    inline = copy.deepcopy(BASE)
    inline["properties"]["amount"] = {"type": "integer"}
    inline["required"] = ["id", "currency", "amount"]
    extended = {"$ref": "#/components/schemas/Base", "properties": {"amount": {"type": "integer"}}, "required": ["amount"]}
    old = discovered(response_document(inline, {}), "11111111-1111-4111-8111-111111111111")
    new = discovered(response_document(extended, {"Base": BASE}), "22222222-2222-4222-8222-222222222222")
    assert detect_changes(old, new, [], api_project_id=API_PROJECT) == []  # same contract
    smaller = copy.deepcopy(BASE)
    del smaller["properties"]["currency"]
    smaller["required"] = ["id"]
    removed = discovered(response_document(extended, {"Base": smaller}), "33333333-3333-4333-8333-333333333333")
    changes = detect_changes(new, removed, [], api_project_id=API_PROJECT)
    assert [(change.kind, change.field_path, change.structural_breaking) for change in changes] == [
        ("field_removed", "response.currency", True)
    ]


# Target fingerprints ------------------------------------------------------------------------


def chain_document(**edits: Any) -> dict:
    """GET -> Thing -> Owner -> Address (kept at the 2-hop limit) -> Geo; ``a__b=v`` edits."""
    schemas = {
        "Thing": {"type": "object", "properties": {"owner": {"$ref": "#/components/schemas/Owner"}}},
        "Owner": {"type": "object", "properties": {"address": {"$ref": "#/components/schemas/Address"}}},
        "Address": {
            "type": "object",
            "description": "Where the owner lives.",
            "properties": {
                "zip": {"type": "string", "example": "12345"},
                "description": {"type": "string"},  # a property named like an annotation
                "geo": {"$ref": "#/components/schemas/Geo"},
            },
            "required": ["zip"],
        },
        "Geo": {"type": "object", "properties": {"lat": {"type": "number"}}},
        "Unrelated": {"type": "string"},
    }
    for path, value in edits.items():
        *parents, last = path.split("__")
        node = schemas
        for name in parents:
            node = node[name]
        if value is None:
            del node[last]
        else:
            node[last] = value
    return response_document({"$ref": "#/components/schemas/Thing"}, schemas, version="3.0.3")


def address_marker(document: dict) -> dict:
    response = response_of(document, json.dumps(document, sort_keys=True))
    marker = response["properties"]["owner"]["properties"]["address"]
    assert marker[REF_STATUS_KEY] == "depth_limit" and marker["$ref"] == "#/components/schemas/Address"
    return marker


@pytest.mark.parametrize(
    ("edits", "changed"),
    [
        ({}, False),
        ({"Address__description": "Edited."}, False),
        ({"Address__title": "Address"}, False),
        ({"Address__properties__zip__example": "99999"}, False),
        ({"Address__x-internal": {"owner": "team"}}, False),
        ({"Geo__properties__lat__description": "Latitude."}, False),
        ({"Unrelated__type": "integer"}, False),  # not reachable from Address
        ({"Address__properties__zip__type": "integer"}, True),
        ({"Address__required": None}, True),
        ({"Address__properties__description": None}, True),  # a property, not an annotation
        ({"Geo__properties__lat__type": "string"}, True),  # one more hop below the kept ref
    ],
)
def test_fingerprints_follow_structure_below_a_kept_reference(edits: dict, changed: bool) -> None:
    before = address_marker(chain_document())[REF_DIGEST_KEY]
    after = address_marker(chain_document(**edits))[REF_DIGEST_KEY]
    assert DIGEST.fullmatch(before) and DIGEST.fullmatch(after)
    assert (before != after) is changed


def test_inlined_copies_and_kept_references_share_their_target_fingerprint() -> None:
    document = chain_document()
    document["paths"]["/addresses"] = {"get": {"responses": ok({"$ref": "#/components/schemas/Address"})}}
    parsed = parse_doc(document, "shared-fingerprint")
    by_path = {op.path: op.response_schema for op in parsed.operations}
    inlined = by_path["/addresses"]
    kept = by_path["/v1/charges/{id}"]["properties"]["owner"]["properties"]["address"]
    assert inlined[ORIGIN_KEY] == "/components/schemas/Address"
    assert inlined[REF_DIGEST_KEY] == kept[REF_DIGEST_KEY]


def test_fingerprints_are_cycle_safe_and_cover_every_local_marker() -> None:
    schemas = {
        "CycleA": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/CycleB"}}},
        "CycleB": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/CycleA"}}},
        "Tree": {"type": "object", "properties": {
            "children": {"type": "array", "items": {"$ref": "#/components/schemas/Tree"}},
        }},
    }
    document = response_document(
        {"type": "object", "properties": {
            "pair": {"$ref": "#/components/schemas/CycleA"},
            "tree": {"$ref": "#/components/schemas/Tree"},
            "remote": {"$ref": "./other.yaml#/X"},
            "missing": {"$ref": "#/components/schemas/Nope"},
        }},
        schemas,
    )
    response = response_of(document)
    markers = all_markers(response)
    assert {marker[REF_STATUS_KEY] for marker in markers} == {"cycle", "remote_unsupported", "missing_target"}
    for marker in markers:  # only targets that resolve locally have a fingerprint
        assert (REF_DIGEST_KEY in marker) is (marker[REF_STATUS_KEY] == "cycle")
    pair = response["properties"]["pair"]
    # CycleA and CycleB reach each other: one strongly connected group, one fingerprint.
    assert pair[REF_DIGEST_KEY] == pair["properties"]["b"][REF_DIGEST_KEY]
    assert pair["properties"]["b"]["properties"]["a"][REF_DIGEST_KEY] == pair[REF_DIGEST_KEY]


def test_documents_cannot_forge_fingerprints() -> None:
    forged = "sha256:" + "0" * 32
    schemas = {
        "Thing": {"type": "object", REF_DIGEST_KEY: forged, "properties": {
            "owner": {"$ref": "#/components/schemas/Owner", REF_DIGEST_KEY: forged},
        }},
        "Owner": {"type": "object", "properties": {"address": {"$ref": "#/components/schemas/Thing"}}},
    }
    parsed = parse_doc(response_document({"$ref": "#/components/schemas/Thing", REF_DIGEST_KEY: forged}, schemas))
    response = parsed.operations[0].response_schema
    found = [response, response["properties"]["owner"], *all_markers(response)]
    assert len(found) == 3
    assert all(DIGEST.fullmatch(node[REF_DIGEST_KEY]) and node[REF_DIGEST_KEY] != forged for node in found)
    assert any("reserved" in warning for warning in parsed.warnings)


def test_fingerprint_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openapi, "_MAX_FINGERPRINT_NODES", 5)
    parsed = parse_doc(chain_document(), "small-fingerprint-budget")
    marker = parsed.operations[0].response_schema["properties"]["owner"]["properties"]["address"]
    assert REF_DIGEST_KEY not in marker  # unknown, never a made-up value
    assert any("fingerprint budget" in warning for warning in parsed.warnings)


# Unresolvable path items ------------------------------------------------------------------------


def path_items_document() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "split", "version": "1"},
        "servers": [{"url": "https://api.example.test/api"}],
        "paths": {
            "/remote/{id}": {"$ref": "./paths/remote.yaml"},
            "/missing": {"$ref": "#/components/pathItems/Missing"},
            "/loop": {"$ref": "#/components/pathItems/LoopA"},
            "/not-an-object": "get",
            "/points-at-a-string": {"$ref": "#/components/pathItems/Text"},
            "/moved": {"$ref": "./paths/moved.yaml", "servers": [{"url": "/v9"}]},
            "/ok": {"$ref": "#/components/pathItems/Ok"},
        },
        "components": {"pathItems": {
            "LoopA": {"$ref": "#/components/pathItems/LoopB"},
            "LoopB": {"$ref": "#/components/pathItems/LoopA"},
            "Text": "not a path item",
            "Ok": {"get": {"responses": {"204": {"description": "ok"}}}},
        }},
    }


def test_unresolvable_path_items_are_recorded_as_unknown_not_absent() -> None:
    parsed = parse_doc(path_items_document(), "split")
    assert [(op.method, op.path) for op in parsed.operations] == [("GET", "/api/ok")]
    expected = ["/api/remote/{id}", "/api/missing", "/api/loop", "/api/not-an-object",
                "/api/points-at-a-string", "/v9/moved"]
    assert parsed.unresolved_paths == expected
    gaps = [pattern for pattern in parsed.unsupported_patterns if pattern.startswith(PATTERN_UNRESOLVED_PATH_ITEM)]
    assert sorted(gaps) == sorted(f"{PATTERN_UNRESOLVED_PATH_ITEM} {path}" for path in expected)
    assert parsed.warnings[0] == (
        "openapi: 6 documented path(s) have a path item that could not be resolved;"
        " their operations are unknown, not absent"
    )
    # The coverage vocabulary analyzer.changes reads: these paths are unknown, /api/ok is not.
    gaps_view = contract_gaps(parsed.unsupported_patterns)
    assert gaps_view.covers("/api/remote/{charge}") and gaps_view.covers("/api/missing")
    assert not gaps_view.covers("/api/ok") and not gaps_view.documents_incomplete


def test_unsafe_documented_paths_are_clipped_in_warnings_but_recorded_exactly() -> None:
    path = "/v1/" + chr(0x202E) + "x" + chr(7) + "/{id}" + chr(0x2028)
    document = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                "paths": {path: {"$ref": "./elsewhere.yaml#" + chr(0x9B) + "31m"}}}
    parsed = parse_doc(document, "unsafe-path")
    assert parsed.unresolved_paths == [path]
    assert f"{PATTERN_UNRESOLVED_PATH_ITEM} {path}" in parsed.unsupported_patterns
    assert parsed.warnings and not any(has_unsafe_characters(warning) for warning in parsed.warnings)


def test_cached_results_keep_every_field() -> None:
    document = path_items_document()
    for index in range(MAX_WARNINGS + 5):  # past the warning cap too
        document["paths"][f"/bad{index}"] = {"get": "not an operation"}
    openapi._CACHE.clear()
    first = parse_doc(document, "cache-fields")
    second = parse_doc(document, "cache-fields")  # restored from the cache
    assert second == first and second.warnings_omitted == first.warnings_omitted > 0
    second.unresolved_paths.append("mutated")
    assert parse_doc(document, "cache-fields") == first
    openapi._CACHE.clear()


# Relative server URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected", "relative"),
    [
        (".", "/things", True),
        ("./", "/things", True),
        ("./v1", "/v1/things", True),
        ("../api/v1", "/api/v1/things", True),
        ("..", "/things", True),
        ("v1", "/v1/things", True),
        ("https://x.test/api/../v1", "/v1/things", False),
        ("/api/./v1/", "/api/v1/things", False),
        ("http://x.test/a/b/../../c/", "/c/things", False),
    ],
)
def test_relative_server_urls_never_leave_dot_segments(url: str, expected: str, relative: bool) -> None:
    document = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "servers": [{"url": url}],
                "paths": {"/things": {"get": {"responses": {"204": {"description": "ok"}}}}}}
    parsed = parse_doc(document, f"server-{url}")
    (op,) = parsed.operations
    assert op.path == expected
    if relative:
        assert len(parsed.warnings) == 1 and "relative to the document's location" in parsed.warnings[0]
    else:
        assert parsed.warnings == []


def test_an_undefined_scheme_variable_does_not_turn_the_host_into_a_path() -> None:
    document = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                "servers": [{"url": "{scheme}://api.example.test/v1"}],
                "paths": {"/things": {"get": {"responses": {"204": {"description": "ok"}}}}}}
    parsed = parse_doc(document, "scheme-variable")
    assert parsed.operations[0].path == "/v1/things"
    assert len(parsed.warnings) == 1 and "no default for scheme" in parsed.warnings[0]


# Overflow counts and warning text -------------------------------------------------------------


def test_warning_overflow_is_counted_not_appended() -> None:
    document = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                "paths": {f"/r{index:04d}": {"get": "not an operation"} for index in range(1000)}}
    parsed = parse_doc(document, "many-warnings")
    assert len(parsed.warnings) == MAX_WARNINGS
    assert parsed.warnings_omitted == 1000 - MAX_WARNINGS
    assert not any("not shown" in warning or warning.startswith("...") for warning in parsed.warnings)


def test_pattern_overflow_is_counted_and_coverage_gaps_are_never_capped() -> None:
    paths: dict[str, Any] = {
        f"/r{index:04d}": {"get": {"responses": ok({"$ref": f"./schemas/s{index}.yaml"})}}
        for index in range(MAX_UNSUPPORTED_PATTERNS + 50)
    }
    paths.update({f"/split{index:04d}": {"$ref": f"./paths/p{index}.yaml"} for index in range(MAX_UNSUPPORTED_PATTERNS)})
    parsed = parse_doc({"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": paths}, "many-patterns")
    gaps = [pattern for pattern in parsed.unsupported_patterns if pattern.startswith(PATTERN_UNRESOLVED_PATH_ITEM)]
    assert len(gaps) == len(parsed.unresolved_paths) == MAX_UNSUPPORTED_PATTERNS  # every one listed
    assert len(parsed.unsupported_patterns) - len(gaps) == MAX_UNSUPPORTED_PATTERNS
    # 550 schema refs plus 500 path-item refs are remote; 500 of them are listed.
    assert parsed.patterns_omitted == MAX_UNSUPPORTED_PATTERNS + 50
    assert not any("not listed" in warning for warning in parsed.warnings)


def test_whole_document_summaries_come_first_and_survive_the_cap() -> None:
    schemas = {"Big": {"type": "object", "properties": {"x": {"$ref": "#/components/schemas/Leaf"}}},
               "Leaf": {"type": "string"}}
    paths: dict[str, Any] = {f"/bad{index}": {"get": "not an operation"} for index in range(MAX_WARNINGS + 10)}
    paths["/ok"] = {"get": {"responses": ok({"$ref": "#/components/schemas/Big"})}}
    parsed = parse_doc({"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": paths,
                        "components": {"schemas": schemas}}, "summaries-first",
                       limits=ExpansionLimits(max_ref_depth=1))
    assert parsed.warnings[0] == "openapi: 1 nested schema $ref(s) were kept unexpanded at the 1-hop depth limit"
    assert len(parsed.warnings) == MAX_WARNINGS + 1 and parsed.warnings_omitted == 10


def test_document_text_reaches_warnings_only_sanitized() -> None:
    weird = "x" + chr(7) + chr(0x1B) + "[31m" + chr(0x202E) + "y" + chr(0x2028) + "z" + chr(0x85) + "w"
    document = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://h.test/" + weird}, {"url": "https://h.test/{v" + weird + "}"}],
        "paths": {
            weird: {"get": {"responses": ok({"$ref": "./remote.yaml#" + weird}),
                            "parameters": [{"$ref": "#/missing/" + weird}]}},
            "/" + weird: {"get": "not an operation"},
        },
    }
    parsed = parse_doc(document, "weird-text")
    assert len(parsed.warnings) >= 5
    for warning in parsed.warnings:
        assert not has_unsafe_characters(warning), repr(warning)


def test_remote_refs_are_shown_without_credentials_but_keep_their_pointer() -> None:
    secret_ref = "https://user:s3cret@specs.example/shared.yaml?token=abc#/components/schemas/Charge"
    plain_ref = "charges.yaml#/components/schemas/Refund"
    document = response_document(
        {"type": "object", "properties": {"charge": {"$ref": secret_ref}, "refund": {"$ref": plain_ref}}}, {}
    )
    parsed = parse_doc(document)
    shown = [*parsed.warnings, *parsed.unsupported_patterns]
    assert not any(marker in text for text in shown for marker in ("s3cret", "user:", "token=abc"))
    assert "openapi:remote-ref https://specs.example/shared.yaml#/components/schemas/Charge" in (
        parsed.unsupported_patterns
    )
    assert f"openapi:remote-ref {plain_ref}" in parsed.unsupported_patterns
