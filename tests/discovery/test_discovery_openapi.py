"""Tests for ``discovery.openapi.parse_openapi_document`` (Stage A OpenAPI parsing).

Fixture documents in ``fixtures/openapi`` are data only. Expectations for the compact
fixture use explicit expansion limits so they do not drift with the module defaults.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from analyzer.fieldpaths import field_schema
from discovery import openapi
from discovery.openapi import DEFAULT_MAX_BYTES, parse_openapi_document
from discovery.records import (
    ORIGIN_KEY,
    REF_DIGEST_KEY,
    REF_STATUS_KEY,
    DocumentedOperation,
    ExpansionLimits,
    FetchedDocument,
    OpenAPIDocumentError,
    ParsedDocument,
    escape_pointer_token,
    unescape_pointer_token,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openapi"
DEMO_V1 = REPO_ROOT / "examples" / "provider" / "demo_payments" / "openapi" / "demo-payments.v1.yaml"
COMPACT = "compact_refs_cycle.yaml"
LIMITS = ExpansionLimits(max_ref_depth=2, max_nodes=6000)
STATUS = "x-shockwave-status"
CONTENT_TYPE = "x-shockwave-content-type"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


# Helpers -------------------------------------------------------------------------------


def fetched(data: bytes, name: str = "inline", media_type: str | None = None) -> FetchedDocument:
    return FetchedDocument(content=data, source_uri=f"fixture://{name}", media_type=media_type)


def parse_fixture(name: str, *, limits: ExpansionLimits = LIMITS) -> ParsedDocument:
    return parse_openapi_document(name, fetched((FIXTURES / name).read_bytes(), name), limits=limits)


def parse_doc(document: dict, **kwargs: Any) -> ParsedDocument:
    return parse_openapi_document("inline", fetched(json.dumps(document).encode()), **kwargs)


def by_id(parsed: ParsedDocument) -> dict[str, DocumentedOperation]:
    return {op.operation_id: op for op in parsed.operations}


def markers(schema: Any, status: str | None = None) -> list[tuple[str, dict]]:
    """``(pointer inside schema, marker)`` for every preserved ``$ref``, sorted."""
    found = []
    stack: list[tuple[str, Any]] = [("", schema)]
    while stack:
        pointer, node = stack.pop()
        if isinstance(node, dict):
            if REF_STATUS_KEY in node and status in (None, node[REF_STATUS_KEY]):
                found.append((pointer, node))
            stack.extend((f"{pointer}/{escape_pointer_token(k)}", v) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend((f"{pointer}/{index}", value) for index, value in enumerate(node))
    return sorted(found, key=lambda item: item[0])


def operation_markers(parsed: ParsedDocument, status: str) -> set[tuple[str, str, str]]:
    return {
        (op.operation_id, part, pointer)
        for op in parsed.operations
        for part, schema in (("request", op.request_schema), ("response", op.response_schema))
        for pointer, _ in markers(schema, status)
    }


def without_digests(value: Any) -> Any:
    """``value`` with every ``REF_DIGEST_KEY`` removed (fingerprints are tested separately)."""
    if isinstance(value, dict):
        return {key: without_digests(child) for key, child in value.items() if key != REF_DIGEST_KEY}
    if isinstance(value, list):
        return [without_digests(child) for child in value]
    return value


def containers(value: Any) -> list[Any]:
    found = []
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            found.append(node)
            stack.extend(node)
    return found


def resolve(document: Any, pointer: str) -> Any:
    """Test-side RFC 6901 resolver that follows local ``$ref`` objects along the way."""

    def follow(node: Any) -> Any:
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            node = resolve(document, node["$ref"][1:])
        return node

    node = document
    for token in pointer.lstrip("/").split("/") if pointer else []:
        node = follow(node)
        node = node[unescape_pointer_token(token)] if isinstance(node, dict) else node[int(token)]
    return follow(node)


def single_operation_document(responses: dict | None = None, **operation: Any) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "inline", "version": "1"},
        "paths": {
            "/things": {
                "get": {
                    "operationId": "op",
                    "responses": responses or {"204": {"description": "ok"}},
                    **operation,
                }
            }
        },
    }


def json_content(schema: dict) -> dict:
    return {"description": "ok", "content": {"application/json": {"schema": schema}}}


# Compact fixture: inventory, provenance and pointers ----------------------------------


def test_compact_fixture_provenance_and_counts_come_from_the_document() -> None:
    data = (FIXTURES / COMPACT).read_bytes()
    raw = yaml.safe_load(data)
    parsed = parse_fixture(COMPACT)

    items = [resolve(raw, "/paths/" + escape_pointer_token(path)) for path in raw["paths"]]
    expected_operations = sum(1 for item in items for key in item if key in HTTP_METHODS)
    provenance = parsed.provenance
    assert provenance.operation_count == len(parsed.operations) == expected_operations == 8
    assert provenance.path_count == len(raw["paths"]) == 5
    assert provenance.component_schema_count == len(raw["components"]["schemas"]) == 11
    assert provenance.sha256 == hashlib.sha256(data).hexdigest()
    assert provenance.byte_size == len(data)
    assert provenance.openapi_version == "3.1.0"
    assert provenance.info_title == raw["info"]["title"]
    assert provenance.info_version == "0.3.0"
    assert provenance.server_urls == ("http://compact.test/api/",)
    assert provenance.document_ref == COMPACT
    assert provenance.source_uri == f"fixture://{COMPACT}"
    assert provenance.revision_label is None
    assert provenance.visibility == "provider_owned"
    assert parsed.unsupported_patterns == []
    assert parsed.warnings == [
        "openapi: 2 nested schema $ref(s) were kept unexpanded at the 2-hop depth limit"
    ]


def test_compact_fixture_operations_paths_and_json_pointers() -> None:
    parsed = parse_fixture(COMPACT)
    rows = [
        (op.method, op.path, op.documented_path, op.operation_id, op.json_pointer)
        for op in parsed.operations
    ]
    assert rows == [
        ("GET", "/api/things", "/things", "listThings", "/paths/~1things/get"),
        ("POST", "/api/things", "/things", "createThing", "/paths/~1things/post"),
        ("GET", "/api/things/{thing_id}", "/things/{thing_id}", "getThing",
         "/paths/~1things~1{thing_id}/get"),
        ("PUT", "/api/things/{thing_id}", "/things/{thing_id}", "replaceThing",
         "/paths/~1things~1{thing_id}/put"),
        ("DELETE", "/api/things/{thing_id}", "/things/{thing_id}", "deleteThing",
         "/paths/~1things~1{thing_id}/delete"),
        ("GET", "/api/trees/{tree_id}", "/trees/{tree_id}", "getTree",
         "/paths/~1trees~1{tree_id}/get"),
        ("GET", "/api/pairs", "/pairs", "getPair", "/paths/~1pairs/get"),
        ("GET", "/api/loops", "/loops", "getLoop", "/paths/~1loops/get"),
    ]
    raw = yaml.safe_load((FIXTURES / COMPACT).read_bytes())
    for op in parsed.operations:  # getTree's pointer resolves through its $ref'd path item
        assert resolve(raw, op.json_pointer)["operationId"] == op.operation_id
    ops = by_id(parsed)
    assert ops["replaceThing"].deprecated is True
    assert ops["getThing"].deprecated is False
    assert ops["listThings"].summary == "List things"
    assert ops["getThing"].summary is None
    assert {op.document_ref for op in parsed.operations} == {COMPACT}


# Compact fixture: $ref expansion ----------------------------------------------------------


def test_cycle_markers_appear_exactly_where_the_graph_loops() -> None:
    parsed = parse_fixture(COMPACT)
    assert operation_markers(parsed, "cycle") == {
        ("getTree", "response", "/properties/children/items"),
        ("getPair", "response", "/properties/b/properties/a"),
        ("getLoop", "response", ""),
    }
    ops = by_id(parsed)
    tree = ops["getTree"].response_schema
    assert tree[ORIGIN_KEY] == "/components/schemas/TreeNode"
    assert tree["properties"]["children"]["items"] == {
        "$ref": "#/components/schemas/TreeNode",
        REF_STATUS_KEY: "cycle",
        REF_DIGEST_KEY: tree[REF_DIGEST_KEY],  # the same target, so the same fingerprint
    }
    pair = ops["getPair"].response_schema
    assert pair[ORIGIN_KEY] == "/components/schemas/CycleA"
    assert pair["properties"]["b"][ORIGIN_KEY] == "/components/schemas/CycleB"
    # LoopA -> LoopB -> LoopA: the chain is followed, then preserved where it closes.
    assert without_digests(ops["getLoop"].response_schema) == {
        "$ref": "#/components/schemas/LoopA",
        REF_STATUS_KEY: "cycle",
        ORIGIN_KEY: "/components/schemas/LoopB",
        STATUS: "2XX",
        CONTENT_TYPE: "application/json",
    }


def test_depth_limit_keeps_references_and_31_sibling_keywords() -> None:
    parsed = parse_fixture(COMPACT)
    assert operation_markers(parsed, "depth_limit") == {
        ("listThings", "response", "/properties/data/items/properties/amount"),
        ("getThing", "response", "/properties/amount"),
    }
    amount = by_id(parsed)["getThing"].response_schema["properties"]["amount"]
    inlined_money = by_id(parsed)["createThing"].response_schema["properties"]["amount"]
    assert amount == {
        "$ref": "#/components/schemas/Money",
        "description": "Amount in cents.",
        REF_STATUS_KEY: "depth_limit",
        REF_DIGEST_KEY: inlined_money[REF_DIGEST_KEY],  # Money, kept here and inlined there
    }
    assert operation_markers(parsed, "node_budget") == set()


def test_inlined_targets_record_their_origin_pointer() -> None:
    ops = by_id(parse_fixture(COMPACT))
    listing = ops["listThings"].response_schema
    assert listing[ORIGIN_KEY] == "/components/schemas/ThingList"
    assert listing["properties"]["data"]["items"][ORIGIN_KEY] == "/components/schemas/Thing"
    created = ops["createThing"].response_schema
    assert created[ORIGIN_KEY] == "/components/schemas/Thing"
    # 3.1: the sibling description overrides the referenced schema's own description.
    assert without_digests(created["properties"]["amount"]) == {
        "type": "integer",
        "description": "Amount in cents.",
        ORIGIN_KEY: "/components/schemas/Money",
    }
    # Ref-to-ref chain ThingAlias -> Thing: the inlined content is Thing's.
    fetched_thing = ops["getThing"].response_schema
    assert fetched_thing[ORIGIN_KEY] == "/components/schemas/Thing"
    assert fetched_thing["required"] == ["id", "amount"]
    assert set(fetched_thing["properties"]) == {"id", "amount", "tags"}
    assert ops["createThing"].request_schema["properties"]["body"][ORIGIN_KEY] == (
        "/components/schemas/ThingInput"
    )


def test_path_level_parameters_merge_and_operation_parameters_override() -> None:
    ops = by_id(parse_fixture(COMPACT))
    get_thing = ops["getThing"].request_schema
    assert get_thing["properties"]["path"] == {
        "type": "object",
        "properties": {"thing_id": {"type": "string"}},
        "required": ["thing_id"],
    }
    assert get_thing["properties"]["query"] == {
        "type": "object",
        "properties": {"verbose": {"type": "string", "enum": ["yes", "no"]}},
        "required": ["verbose"],
    }
    assert get_thing["required"] == ["path", "query"]
    replace = ops["replaceThing"].request_schema
    assert replace["properties"]["query"] == {
        "type": "object",
        "properties": {"verbose": {"type": "boolean"}},
    }
    assert replace["required"] == ["body", "path"]
    assert ops["listThings"].request_schema == {
        "type": "object",
        "properties": {
            "query": {"type": "object", "properties": {"limit": {"type": "integer", "maximum": 100}}}
        },
    }


def test_request_body_content_type_preference() -> None:
    ops = by_id(parse_fixture(COMPACT))
    created = ops["createThing"].request_schema
    assert created[CONTENT_TYPE] == "application/json"  # listed last, still preferred
    assert created["required"] == ["body"]
    assert created["properties"]["body"]["required"] == ["name"]
    replaced = ops["replaceThing"].request_schema
    assert replaced[CONTENT_TYPE] == "application/x-www-form-urlencoded"  # form over multipart
    assert ops["getPair"].request_schema == {"type": "object", "properties": {}}


def test_success_response_selection() -> None:
    ops = by_id(parse_fixture(COMPACT))
    created = ops["createThing"].response_schema
    assert (created[STATUS], created[CONTENT_TYPE]) == ("201", "application/json")
    fetched_thing = ops["getThing"].response_schema
    assert (fetched_thing[STATUS], fetched_thing[CONTENT_TYPE]) == ("200", "application/hal+json")
    assert ops["deleteThing"].response_schema == {STATUS: "204"}  # not the default error body
    assert ops["getLoop"].response_schema[STATUS] == "2XX"


@pytest.mark.parametrize(
    ("responses", "expected_status"),
    [
        ({"200": {"description": "no body"}, "201": json_content({"type": "object"})}, "201"),
        ({"default": json_content({"type": "object"})}, "default"),
        ({"2XX": json_content({"type": "object"}), "default": json_content({"type": "string"})}, "2XX"),
    ],
)
def test_lowest_success_status_with_content_wins(responses: dict, expected_status: str) -> None:
    (op,) = parse_doc(single_operation_document(responses)).operations
    assert op.response_schema == {
        "type": "object",
        STATUS: expected_status,
        CONTENT_TYPE: "application/json",
    }


def test_no_success_response_gives_an_empty_schema() -> None:
    (op,) = parse_doc(single_operation_document({"404": json_content({"type": "object"})})).operations
    assert op.response_schema == {}


# Determinism and isolation ----------------------------------------------------------------


def test_repeated_parses_are_equal_and_callers_cannot_corrupt_the_cache() -> None:
    openapi._CACHE.clear()
    first = parse_fixture(COMPACT)  # computed
    pristine = copy.deepcopy(first)
    second = parse_fixture(COMPACT)  # restored from the cache
    assert second == first
    assert second.operations[0].response_schema is not first.operations[0].response_schema

    for result in (first, second):
        for op in result.operations:
            op.response_schema.clear()
            op.request_schema["properties"] = "mutated"
        result.operations.pop()
        result.warnings.append("mutated")
        result.unsupported_patterns.append("mutated")

    assert parse_fixture(COMPACT) == pristine
    openapi._CACHE.clear()
    assert parse_fixture(COMPACT) == pristine  # a cold parse agrees with the cached one


def test_returned_schemas_share_no_containers() -> None:
    parsed = parse_fixture(COMPACT)
    seen: dict[int, Any] = {}
    for op in parsed.operations:
        for schema in (op.request_schema, op.response_schema):
            for node in containers(schema):
                assert id(node) not in seen
                seen[id(node)] = node


def test_yaml_aliases_become_independent_copies() -> None:
    text = """
openapi: 3.0.3
info: {title: aliases, version: '1'}
paths:
  /a:
    get:
      operationId: a
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: &shared
                type: object
                properties:
                  amount: {type: number}
  /b:
    get:
      operationId: b
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: *shared
"""
    first, second = parse_openapi_document("aliases", fetched(text.encode())).operations
    assert first.response_schema == second.response_schema
    first.response_schema["properties"]["amount"]["type"] = "string"
    assert second.response_schema["properties"]["amount"]["type"] == "number"


# References that cannot be followed -----------------------------------------------------


def test_remote_and_missing_refs_are_reported_and_never_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the parser must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)

    parsed = parse_fixture("remote_refs.yaml")
    assert [op.operation_id for op in parsed.operations] == ["getPayment", "createPayment"]
    assert parsed.provenance.path_count == 3  # the remote /external path item yields nothing

    ops = by_id(parsed)
    money = {"$ref": "./common.yaml#/components/schemas/Money", REF_STATUS_KEY: "remote_unsupported"}
    properties = without_digests(ops["getPayment"].response_schema["properties"])
    assert properties == {
        "amount": money,
        "metadata": {"$ref": "https://example.com/schemas/x.json", REF_STATUS_KEY: "remote_unsupported"},
        "status": {"$ref": "#/components/schemas/DoesNotExist", REF_STATUS_KEY: "missing_target"},
        "currency": {"type": "string", ORIGIN_KEY: "/components/schemas/Currency"},
        "fee": money,
    }
    # The remote parameter's name and location are unknown, so nothing is invented for it.
    assert ops["getPayment"].request_schema["properties"] == {
        "path": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    }
    assert ops["createPayment"].request_schema == {
        "type": "object",
        "properties": {
            "body": {"$ref": "https://example.com/bodies/payment.json", REF_STATUS_KEY: "remote_unsupported"}
        },
    }
    assert ops["createPayment"].response_schema == {
        "$ref": "./common.yaml#/components/responses/Created",
        REF_STATUS_KEY: "remote_unsupported",
        STATUS: "201",
    }

    remote_refs = [
        "./common.yaml#/components/parameters/Expand",
        "./common.yaml#/components/responses/Created",
        "./common.yaml#/components/schemas/Money",
        "./paths.yaml#/external",
        "https://example.com/bodies/payment.json",
        "https://example.com/schemas/x.json",
    ]
    assert parsed.unsupported_patterns == sorted(
        [*(f"openapi:remote-ref {ref}" for ref in remote_refs), "openapi:unresolved-path-item /external"]
    )
    assert parsed.unresolved_paths == ["/external"]  # documented, operations unknown
    for ref in remote_refs:  # one warning per distinct reference, even when reused
        assert sum(repr(ref) in warning for warning in parsed.warnings) == 1
    assert any(
        "'#/components/schemas/DoesNotExist'" in warning and "does not resolve" in warning
        for warning in parsed.warnings
    )


def test_local_refs_use_json_pointer_unescaping_and_percent_decoding() -> None:
    document = single_operation_document(
        {
            "200": json_content(
                {
                    "type": "object",
                    "properties": {
                        "spaced": {"$ref": "#/components/schemas/Foo%20Bar"},
                        "slashed": {"$ref": "#/components/schemas/a~1b"},
                        "tilded": {"$ref": "#/components/schemas/t~0x"},
                        "missing": {"$ref": "#/components/schemas/a~1c"},
                    },
                }
            )
        }
    )
    document["components"] = {
        "schemas": {"Foo Bar": {"type": "string"}, "a/b": {"type": "integer"}, "t~x": {"type": "boolean"}}
    }
    properties = without_digests(parse_doc(document).operations[0].response_schema["properties"])
    assert properties["spaced"] == {"type": "string", ORIGIN_KEY: "/components/schemas/Foo Bar"}
    assert properties["slashed"] == {"type": "integer", ORIGIN_KEY: "/components/schemas/a~1b"}
    assert properties["tilded"] == {"type": "boolean", ORIGIN_KEY: "/components/schemas/t~0x"}
    assert properties["missing"] == {"$ref": "#/components/schemas/a~1c", REF_STATUS_KEY: "missing_target"}


@pytest.mark.parametrize(("version", "description"), [("3.1.0", "Overridden."), ("3.0.3", "Original.")])
def test_ref_siblings_overlay_in_31_and_are_ignored_in_30(version: str, description: str) -> None:
    document = single_operation_document(
        {"200": json_content({"$ref": "#/components/schemas/Money", "description": "Overridden."})}
    )
    document["openapi"] = version
    document["components"] = {"schemas": {"Money": {"type": "integer", "description": "Original."}}}
    schema = parse_doc(document).operations[0].response_schema
    assert schema["type"] == "integer"
    assert schema["description"] == description
    assert schema[ORIGIN_KEY] == "/components/schemas/Money"


def test_literal_keywords_and_extensions_are_data_and_reserved_keys_are_dropped() -> None:
    document = single_operation_document({"200": json_content({"$ref": "#/components/schemas/Thing"})})
    document["components"] = {
        "schemas": {
            "Thing": {
                "type": "object",
                "x-expansionResources": {"oneOf": [{"$ref": "#/components/schemas/Other"}]},
                "x-shockwave-ref": "cycle",
                "example": {"$ref": "#/components/schemas/Other"},
                "properties": {
                    "default": {"$ref": "#/components/schemas/Other"},
                    "kind": {"type": "string", "enum": [{"$ref": "#/not/a/schema"}]},
                    "$ref": {"type": "string"},
                },
            },
            "Other": {"type": "string"},
        }
    }
    parsed = parse_doc(document)
    schema = parsed.operations[0].response_schema
    assert schema["x-expansionResources"] == {"oneOf": [{"$ref": "#/components/schemas/Other"}]}
    assert schema["example"] == {"$ref": "#/components/schemas/Other"}
    assert REF_STATUS_KEY not in schema  # a document cannot forge an expansion marker
    assert without_digests(schema["properties"]) == {
        "default": {"type": "string", ORIGIN_KEY: "/components/schemas/Other"},
        "kind": {"type": "string", "enum": [{"$ref": "#/not/a/schema"}]},
        "$ref": {"type": "string"},
    }
    assert any("reserved" in warning for warning in parsed.warnings)


def test_extensions_webhooks_and_malformed_entries_are_skipped_with_warnings() -> None:
    document = single_operation_document(
        parameters=[
            {"name": 7, "in": "query"},
            {"name": "Accept", "in": "header", "schema": {"type": "string"}},  # ignored per spec
            {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
        ]
    )
    document["openapi"] = "3.1.0"
    document["paths"]["x-internal"] = {"get": {"operationId": "hidden", "responses": {}}}
    document["paths"]["/broken"] = {"get": "not an operation"}
    document["webhooks"] = {"chargeSucceeded": {"post": {"responses": {"200": {"description": "ok"}}}}}
    parsed = parse_doc(document)
    assert [op.operation_id for op in parsed.operations] == ["op"]
    assert parsed.provenance.path_count == 2  # the x- key is an extension, not a path
    assert parsed.operations[0].request_schema["properties"] == {
        "header": {"type": "object", "properties": {"X-Trace": {"type": "string"}}}
    }
    warnings = " | ".join(parsed.warnings)
    assert "no usable name/in" in warnings
    assert "not an operation object" in warnings
    assert "webhook" in warnings


# Dialects ------------------------------------------------------------------------------------


def test_openapi_30_nullable_parses_and_yaml_scalars_stay_json_shaped() -> None:
    parsed = parse_fixture("nullable_30.yaml")
    assert parsed.provenance.openapi_version == "3.0.0"
    assert parsed.provenance.info_version == "2024-01-01"  # an unquoted YAML date stays text
    (op,) = parsed.operations
    response = op.response_schema
    assert response[STATUS] == "200"  # unquoted YAML status key 200 becomes "200"
    assert response["properties"]["note"] == {"type": "string", "nullable": True}
    parent = response["properties"]["parent"]
    assert parent["nullable"] is True
    assert parent["allOf"][0][ORIGIN_KEY] == "/components/schemas/Widget"


def test_openapi_31_type_arrays_and_boolean_schemas_parse() -> None:
    parsed = parse_fixture("type_arrays_31.json")
    assert parsed.provenance.openapi_version == "3.1.0"
    properties = parsed.operations[0].response_schema["properties"]
    assert properties["count"] == {"type": ["integer", "null"]}
    assert properties["flag"] is True
    gadget = properties["items"]["items"]  # a property named "items" is still a property
    assert gadget["type"] == ["object", "null"]
    assert gadget["properties"]["name"]["examples"] == ["gizmo"]
    assert gadget[ORIGIN_KEY] == "/components/schemas/Gadget"


def test_media_type_selects_the_parser_and_sniffing_covers_the_rest() -> None:
    yaml_bytes = DEMO_V1.read_bytes()
    with pytest.raises(OpenAPIDocumentError, match="invalid JSON"):
        parse_openapi_document("demo", fetched(yaml_bytes, media_type="application/json"))
    json_bytes = json.dumps(yaml.safe_load(yaml_bytes)).encode()
    for media_type in (None, "application/vnd.oai.openapi+json", "application/yaml"):
        parsed = parse_openapi_document("demo", fetched(json_bytes, media_type=media_type))
        assert parsed.provenance.operation_count == 2


# Rejections ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "media_type", "message"),
    [
        (b'{"swagger": "2.0", "info": {"title": "x", "version": "1"}, "paths": {}}', None, "unsupported"),
        (b"openapi: 3.2.0\ninfo: {title: x, version: '1'}\npaths: {}\n", None, "unsupported"),
        (b"openapi: 3.1\npaths: {}\n", None, "unsupported"),  # a YAML float, not a version string
        (b"info: {title: x, version: '1'}\npaths: {}\n", None, "unsupported"),
        (b"[1, 2, 3]", "application/json", "mapping"),
        (b"- just\n- a list\n", None, "mapping"),
        (b"", None, "mapping"),
        (b'{"openapi": "3.0.0", "paths": {', None, "invalid JSON"),
        (b"openapi: 3.0.0\npaths: [unclosed\n", None, "invalid YAML"),
        (b"\xff\xfe\x00not utf-8", None, "UTF-8"),
    ],
)
def test_unsupported_or_malformed_documents_are_rejected(
    data: bytes, media_type: str | None, message: str
) -> None:
    with pytest.raises(OpenAPIDocumentError, match=message):
        parse_openapi_document("bad", fetched(data, media_type=media_type))


def test_yaml_alias_bomb_is_rejected_quickly() -> None:
    lines = ['a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for previous, name in itertools.pairwise("abcdefghi"):
        lines.append(f"{name}: &{name} [{', '.join(['*' + previous] * 9)}]")  # 72 aliases, 9**9 nodes
    data = ("openapi: 3.0.0\ninfo: {title: bomb, version: '1'}\npaths: {}\nx-bomb:\n  "
            + "\n  ".join(lines) + "\n").encode()
    start = time.perf_counter()
    with pytest.raises(OpenAPIDocumentError, match="alias"):
        parse_openapi_document("bomb", fetched(data))
    assert time.perf_counter() - start < 2.0


def test_yaml_alias_count_and_recursion_are_bounded() -> None:
    uses = "\n".join(f"  k{index}: *v" for index in range(openapi.MAX_YAML_ALIASES + 1))
    many = f"openapi: 3.0.0\ninfo: {{title: t, version: '1'}}\npaths: {{}}\nx-a: &v 1\nx-uses:\n{uses}\n"
    with pytest.raises(OpenAPIDocumentError, match="aliases"):
        parse_openapi_document("many-aliases", fetched(many.encode()))
    recursive = "openapi: 3.0.0\ninfo: {title: t, version: '1'}\npaths: {}\nx-loop: &a [1, *a]\n"
    with pytest.raises(OpenAPIDocumentError, match="recursive"):
        parse_openapi_document("recursive-alias", fetched(recursive.encode()))


def test_content_over_max_bytes_is_rejected_before_parsing() -> None:
    assert DEFAULT_MAX_BYTES == 32 * 1024 * 1024
    data = DEMO_V1.read_bytes()
    with pytest.raises(OpenAPIDocumentError, match="limit"):
        parse_openapi_document("demo", fetched(data), max_bytes=len(data) - 1)
    assert parse_openapi_document("demo", fetched(data), max_bytes=len(data)).provenance.byte_size == len(data)


def test_excessive_nesting_is_rejected() -> None:
    deep: list = []
    for _ in range(openapi.MAX_DOCUMENT_DEPTH + 5):
        deep = [deep]
    document = single_operation_document()
    document["x-deep"] = deep
    with pytest.raises(OpenAPIDocumentError, match="nesting"):
        parse_doc(document)


# Servers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("servers", "expected_path"),
    [
        (None, "/things"),
        ([{"url": "http://x.test/api/v2"}], "/api/v2/things"),
        ([{"url": "http://x.test/api/v2/"}], "/api/v2/things"),
        ([{"url": "https://api.example.test/"}], "/things"),
        ([{"url": "/relative/base"}], "/relative/base/things"),
        (
            [
                {
                    "url": "https://{region}.x.test/{basePath}",
                    "variables": {"region": {"default": "eu"}, "basePath": {"default": "v3"}},
                }
            ],
            "/v3/things",
        ),
    ],
)
def test_server_base_path_is_joined_to_documented_paths(servers: list | None, expected_path: str) -> None:
    document = single_operation_document()
    if servers is not None:
        document["servers"] = servers
    parsed = parse_doc(document)
    (op,) = parsed.operations
    assert op.path == expected_path
    assert op.documented_path == "/things"
    assert op.json_pointer == "/paths/~1things/get"
    assert parsed.provenance.server_urls == tuple(server["url"] for server in servers or [])
    assert parsed.warnings == []


def test_disagreeing_servers_use_the_first_base_path_and_warn() -> None:
    document = single_operation_document()
    document["servers"] = [{"url": "https://a.test/v1"}, {"url": "https://b.test/v2"}]
    parsed = parse_doc(document)
    assert parsed.operations[0].path == "/v1/things"
    assert any("different base paths" in warning for warning in parsed.warnings)


def test_path_and_operation_servers_override_the_document_servers() -> None:
    document = single_operation_document(servers=[{"url": "https://op.test/operation"}])
    document["servers"] = [{"url": "https://root.test/root"}]
    document["paths"]["/things"]["servers"] = [{"url": "https://item.test/item"}]
    document["paths"]["/things"]["post"] = {"operationId": "post", "responses": {"204": {"description": "ok"}}}
    paths = {op.operation_id: op.path for op in parse_doc(document).operations}
    assert paths == {"op": "/operation/things", "post": "/item/things"}


def test_server_variables_without_defaults_are_reported() -> None:
    document = single_operation_document()
    document["servers"] = [{"url": "https://x.test/{version}/api"}]
    parsed = parse_doc(document)
    assert parsed.operations[0].path == "/api/things"
    assert any("no default" in warning for warning in parsed.warnings)


# Demo Payments contract --------------------------------------------------------------------


def test_demo_payments_v1_contract_is_fully_inlined_with_default_limits() -> None:
    parsed = parse_openapi_document("demo-payments.v1.yaml", fetched(DEMO_V1.read_bytes()))
    assert parsed.provenance.operation_count == len(parsed.operations) == 2
    ops = {(op.method, op.path): op for op in parsed.operations}
    charge = ops[("GET", "/v1/charges/{id}")]
    assert charge.operation_id == "retrieveCharge"
    assert charge.json_pointer == "/paths/~1v1~1charges~1{id}/get"
    response = charge.response_schema
    assert response[ORIGIN_KEY] == "/components/schemas/Charge"
    assert response["properties"]["amount"]["type"] == "number"
    assert "amount" in response["required"]
    assert (response[STATUS], response[CONTENT_TYPE]) == ("200", "application/json")
    assert charge.request_schema["properties"]["path"] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    assert charge.request_schema["required"] == ["path"]
    for op in parsed.operations:
        assert markers(op.request_schema) == [] and markers(op.response_schema) == []
    assert parsed.warnings == [] and parsed.unsupported_patterns == []
    # The schema shape is the one analyzer.fieldpaths walks for change/impact analysis.
    assert field_schema(charge.request_schema, response, "response.amount")["type"] == "number"
    assert field_schema(charge.request_schema, response, "request.path.id") == {"type": "string"}


# Expansion limits -----------------------------------------------------------------------------


def test_tight_limits_preserve_refs_with_depth_and_budget_markers() -> None:
    parsed = parse_fixture("deep_refs.yaml", limits=ExpansionLimits(max_ref_depth=1, max_nodes=20))
    ops = by_id(parsed)
    chain = ops["getChain"].response_schema
    assert chain[ORIGIN_KEY] == "/components/schemas/Level1"
    assert without_digests(chain["properties"]["next"]) == {
        "$ref": "#/components/schemas/Level2",
        REF_STATUS_KEY: "depth_limit",
    }
    wide = without_digests(ops["getWide"].response_schema["properties"])
    # Breadth-first: the first reference still fits the 20-node budget, the rest are kept as refs.
    assert wide["p1"] == {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        ORIGIN_KEY: "/components/schemas/Leaf",
    }
    leaf_ref = {"$ref": "#/components/schemas/Leaf", REF_STATUS_KEY: "node_budget"}
    assert [wide[name] for name in ("p2", "p3", "p4", "p5")] == [leaf_ref] * 4
    assert any("depth limit" in warning for warning in parsed.warnings)
    assert any("node budget" in warning for warning in parsed.warnings)


def test_default_limits_expand_the_deep_fixture_to_two_hops() -> None:
    parsed = parse_fixture("deep_refs.yaml", limits=ExpansionLimits())
    assert operation_markers(parsed, "depth_limit") == {
        ("getChain", "response", "/properties/next/properties/next")
    }
    assert operation_markers(parsed, "node_budget") == set()
    wide = by_id(parsed)["getWide"].response_schema["properties"]
    assert all(wide[name][ORIGIN_KEY] == "/components/schemas/Leaf" for name in wide)
