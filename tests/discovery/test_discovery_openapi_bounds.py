"""Bounds of ``discovery.openapi``: document-wide expansion budget, operation cap, output
depth, YAML size/node/alias caps, duplicate keys and non-finite numbers.

Documents are built in memory (data only). Expected sizes are computed from the documents
themselves, so the tests follow the module's limits rather than hardcoding Stripe-sized
numbers.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from discovery import openapi
from discovery.contracts import DiscoveryInput, DiscoveryResult
from discovery.openapi import MAX_OUTPUT_DEPTH, parse_openapi_document
from discovery.records import (
    ORIGIN_KEY,
    REF_DIGEST_KEY,
    REF_STATUS_KEY,
    ExpansionLimits,
    FetchedDocument,
    OpenAPIDocumentError,
    ParsedDocument,
)
from discovery.service import discover

STATUS = "x-shockwave-status"
CONTENT_TYPE = "x-shockwave-content-type"
SNAPSHOT = "11111111-1111-4111-8111-111111111111"
DOCUMENT_BUDGET_WARNING = "document-wide"


def fetched(data: bytes, name: str = "inline", media_type: str | None = None) -> FetchedDocument:
    return FetchedDocument(content=data, source_uri=f"fixture://{name}", media_type=media_type)


def parse_doc(document: dict, name: str = "inline", **kwargs: Any) -> ParsedDocument:
    return parse_openapi_document(name, fetched(json.dumps(document).encode(), name), **kwargs)


def nodes(value: Any) -> int:
    count, stack = 0, [value]
    while stack:
        current = stack.pop()
        count += 1
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return count


def levels(value: Any) -> int:
    """Container nesting of ``value`` (a dict of scalars has 1 level)."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        current, level = stack.pop()
        children = current.values() if isinstance(current, dict) else current if isinstance(current, list) else ()
        if isinstance(current, (dict, list)):
            deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in children)
    return deepest


def markers(value: Any, status: str) -> list[dict]:
    found, stack = [], [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if current.get(REF_STATUS_KEY) == status:
                found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def output_nodes(parsed: ParsedDocument) -> int:
    return sum(nodes(op.request_schema) + nodes(op.response_schema) for op in parsed.operations)


def wide(properties: int) -> dict:
    return {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(properties)}}


def ok(schema: Any) -> dict:
    return {"200": {"description": "ok", "content": {"application/json": {"schema": schema}}}}


def document(paths: dict, components: dict | None = None, version: str = "3.0.3") -> dict:
    return {"openapi": version, "info": {"title": "bounds", "version": "1"}, "paths": paths,
            "components": components or {}}


# Document-wide expansion budget ------------------------------------------------------------


BIG = wide(60)  # 123 nodes as written
BUDGET = 1000
LIMITS = ExpansionLimits(max_ref_depth=2, max_nodes=6000, max_document_nodes=BUDGET)


def assert_bounded(parsed: ParsedDocument, operations: int) -> None:
    """Output stays near the budget however many operations share the big schema."""
    assert len(parsed.operations) == operations
    # Overshoot is bounded by one operation's own inline structure plus marker overhead.
    assert output_nodes(parsed) <= BUDGET + nodes(BIG) + 12 * operations
    summary = [warning for warning in parsed.warnings if DOCUMENT_BUDGET_WARNING in warning]
    assert len(summary) == 1 and f"{BUDGET}-node" in summary[0]
    assert "6000-node budget" not in " ".join(parsed.warnings)  # not the per-schema limit


def test_component_ref_fan_out_stops_at_the_document_budget() -> None:
    paths = {f"/r{i}": {"get": {"responses": ok({"$ref": "#/components/schemas/Big"})}} for i in range(40)}
    parsed = parse_doc(document(paths, {"schemas": {"Big": BIG}}), limits=LIMITS)
    assert_bounded(parsed, 40)
    first, last = parsed.operations[0].response_schema, parsed.operations[-1].response_schema
    assert first[ORIGIN_KEY] == "/components/schemas/Big" and len(first["properties"]) == 60
    # Past the budget the reference is kept, naming its target, with the target's fingerprint.
    assert {key: last[key] for key in ("$ref", REF_STATUS_KEY)} == {
        "$ref": "#/components/schemas/Big",
        REF_STATUS_KEY: "node_budget",
    }
    assert last[REF_DIGEST_KEY] == first[REF_DIGEST_KEY]
    assert (last[STATUS], last[CONTENT_TYPE]) == ("200", "application/json")


def test_shared_response_with_an_inline_schema_stops_at_the_document_budget() -> None:
    paths = {f"/r{i}": {"get": {"responses": {"200": {"$ref": "#/components/responses/R"}}}} for i in range(40)}
    parsed = parse_doc(document(paths, {"responses": {"R": ok(BIG)["200"]}}), limits=LIMITS)
    assert_bounded(parsed, 40)
    assert len(parsed.operations[0].response_schema["properties"]) == 60
    kept = parsed.operations[-1].response_schema
    # The inline schema has no component name: the reference names where it is written,
    # the way DocumentedOperation.json_pointer does (through the response reference).
    assert kept["$ref"] == "#/paths/~1r39/get/responses/200/content/application~1json/schema"
    assert kept[REF_STATUS_KEY] == "node_budget"
    assert kept[REF_DIGEST_KEY].startswith("sha256:") and len(kept[REF_DIGEST_KEY]) == 39
    assert len({op.response_schema.get(REF_DIGEST_KEY) for op in parsed.operations[-5:]}) == 1


def test_path_item_ref_fan_out_stops_at_the_document_budget() -> None:
    shared = {
        "get": {"responses": ok(BIG)},
        "post": {
            "parameters": [{"name": "filter", "in": "query", "required": True, "schema": BIG}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": BIG}}},
            "responses": ok({"$ref": "#/components/schemas/Big"}),
        },
    }
    paths = {"/p0": shared, **{f"/p{i}": {"$ref": "#/paths/~1p0"} for i in range(1, 15)}}
    parsed = parse_doc(document(paths, {"schemas": {"Big": BIG}}), limits=LIMITS)
    assert_bounded(parsed, 30)
    last = parsed.operations[-1]
    assert last.method == "POST" and last.path == "/p14"
    request = last.request_schema
    # Requiredness is known even when the schema itself is kept unexpanded.
    assert request["required"] == ["body", "query"]
    assert request["properties"]["body"][REF_STATUS_KEY] == "node_budget"
    assert request["properties"]["body"]["$ref"] == "#/paths/~1p14/post/requestBody/content/application~1json/schema"
    assert request["properties"]["query"]["properties"]["filter"] == {
        "$ref": "#/paths/~1p14/post/parameters/0/schema",
        REF_STATUS_KEY: "node_budget",
        REF_DIGEST_KEY: request["properties"]["body"][REF_DIGEST_KEY],  # the same schema content
    }


def test_default_document_budget_bounds_a_large_fan_out() -> None:
    big = wide(2900)  # 5,803 nodes, under the 6,000-node per-schema budget
    paths = {f"/r{i}": {"get": {"responses": ok({"$ref": "#/components/schemas/Big"})}} for i in range(600)}
    parsed = parse_doc(document(paths, {"schemas": {"Big": big}}), name="fan-out-default")
    limit = ExpansionLimits().max_document_nodes
    assert output_nodes(parsed) <= limit + nodes(big) + 12 * len(paths)
    kept = sum(len(markers(op.response_schema, "node_budget")) for op in parsed.operations)
    assert 0 < kept < 600
    assert any(f"document-wide {limit}-node" in warning for warning in parsed.warnings)


def test_documents_with_more_operations_than_the_cap_are_refused() -> None:
    paths = {f"/r{i}": {"get": {"responses": {"204": {"description": "ok"}}}} for i in range(4)}
    limits = ExpansionLimits(max_operations=4)
    assert len(parse_doc(document(paths), limits=limits).operations) == 4
    paths["/r4"] = {"get": {"responses": {"204": {"description": "ok"}}}}
    # Reading only some operations would make the rest look removed: refuse instead.
    with pytest.raises(OpenAPIDocumentError, match="more than 4 operations"):
        parse_doc(document(paths), limits=limits)


# Output depth ------------------------------------------------------------------------------


def items_chain(steps: int, leaf: Any) -> Any:
    node = leaf
    for _ in range(steps):
        node = {"type": "array", "items": node}
    return node


def deep_document(inline: int, a: int, b: int, *, siblings: bool = False) -> dict:
    """An inline response chain ending in $ref A, A ending in $ref B: each as deep as given."""
    to_a: dict = {"$ref": "#/components/schemas/A"}
    if siblings:  # 3.1: constraining siblings put the reference two levels lower
        to_a["minItems"] = 1
    return document(
        {
            "/deep": {"get": {"responses": ok(items_chain(inline, to_a))}},
            "/flat": {"get": {"responses": ok({"type": "string"})}},
        },
        {"schemas": {
            "A": items_chain(a, {"$ref": "#/components/schemas/B"}),
            "B": items_chain(b, {"type": "string"}),
        }},
        version="3.1.0" if siblings else "3.0.3",
    )


def discovered(raw: dict) -> DiscoveryResult:
    request = DiscoveryInput(service_id="deep", snapshot_id=SNAPSHOT, openapi_documents=["deep.json"],
                             source_snapshot_id=None, frameworks=[])
    return discover(request, {"deep.json": json.dumps(raw).encode()}, None).result


@pytest.mark.parametrize(
    ("inline", "a", "b", "siblings"),
    [
        (0, 30, 30, False),
        (0, 124, 124, False),  # A inlined at the root, B too deep below it
        (30, 124, 124, False),
        (60, 90, 30, False),
        (119, 124, 124, False),  # the deepest inline chain the document depth allows
        (119, 2, 2, False),
        (100, 120, 120, True),
        (117, 124, 124, True),
    ],
)
def test_deepest_admissible_documents_survive_a_json_round_trip(
    inline: int, a: int, b: int, siblings: bool
) -> None:
    raw = deep_document(inline, a, b, siblings=siblings)
    result = discovered(raw)
    assert not [w for w in result.warnings if "could not be parsed" in w], result.warnings
    schemas = [schema for endpoint in result.endpoints for schema in (endpoint.request_schema, endpoint.response_schema)]
    assert max(levels(schema) for schema in schemas) <= MAX_OUTPUT_DEPTH
    # What the backend persists: Pydantic's JSON form, read back into an equal result.
    text = result.model_dump_json()
    result.model_dump(mode="json")
    assert DiscoveryResult.model_validate_json(text) == result


def test_targets_that_would_nest_too_deep_are_kept_with_a_distinct_warning() -> None:
    parsed = parse_doc(deep_document(119, 124, 124), name="too-deep")
    deep = parsed.operations[0].response_schema
    (marker,) = markers(deep, "depth_limit")
    assert marker["$ref"] == "#/components/schemas/A" and marker[REF_DIGEST_KEY].startswith("sha256:")
    assert levels(deep) <= MAX_OUTPUT_DEPTH
    assert f"1 schema $ref(s) were kept unexpanded at the {MAX_OUTPUT_DEPTH}-level output depth limit" in (
        " ".join(parsed.warnings)
    )
    assert not any("hop depth limit" in warning for warning in parsed.warnings)  # not the hop limit
    # A shallow position still inlines the same target.
    shallow = parse_doc(deep_document(10, 124, 2), name="shallow").operations[0].response_schema
    assert markers(shallow, "depth_limit") == [] and levels(shallow) > 124


# YAML -----------------------------------------------------------------------------------------


def yaml_operations(count: int) -> str:
    lines = [
        "openapi: 3.0.3",
        "info: {title: anchors, version: '1'}",
        "x-shared:",
        "  unauthorized: &unauthorized {description: no, content: {application/json: {schema: {type: object}}}}",
        "  forbidden: &forbidden {description: no, content: {application/json: {schema: {type: string}}}}",
        "  error: &error {description: no, content: {application/json: {schema: {type: integer}}}}",
        "paths:",
    ]
    for index in range(count):
        lines += [
            f"  /r{index}:",
            "    get:",
            "      responses:",
            "        '200': {description: ok, content: {application/json: {schema: {type: boolean}}}}",
            "        '401': *unauthorized",
            "        '403': *forbidden",
            "        default: *error",
        ]
    return "\n".join(lines) + "\n"


def test_small_specs_that_reuse_anchored_responses_parse() -> None:
    text = yaml_operations(34)  # 102 aliases: refused by the old 100-alias cap
    parsed = parse_openapi_document("anchors", fetched(text.encode()))
    assert len(parsed.operations) == 34
    assert {op.response_schema["type"] for op in parsed.operations} == {"boolean"}


def test_yaml_alias_cap_still_counts_aliases() -> None:
    uses = "\n".join(f"  k{index}: *v" for index in range(openapi.MAX_YAML_ALIASES + 1))
    many = f"openapi: 3.0.0\ninfo: {{title: t, version: '1'}}\npaths: {{}}\nx-a: &v 1\nx-uses:\n{uses}\n"
    start = time.perf_counter()
    with pytest.raises(OpenAPIDocumentError, match="aliases"):
        parse_openapi_document("alias-flood", fetched(many.encode()))
    assert time.perf_counter() - start < 5.0


def test_yaml_is_bounded_by_size_and_node_count(monkeypatch: pytest.MonkeyPatch) -> None:
    head = "openapi: 3.0.0\ninfo: {title: t, version: '1'}\npaths: {}\nx-items:\n"
    monkeypatch.setattr(openapi, "MAX_YAML_NODES", 200)
    with pytest.raises(OpenAPIDocumentError, match="more than 200 nodes"):
        parse_openapi_document("yaml-nodes", fetched((head + "- a\n" * 300).encode()))
    assert parse_openapi_document("yaml-nodes-ok", fetched((head + "- a\n" * 100).encode())).operations == []

    monkeypatch.setattr(openapi, "MAX_YAML_BYTES", 1024)
    large = (head + "- " + "a" * 2000 + "\n").encode()
    with pytest.raises(OpenAPIDocumentError, match="1024-byte limit for YAML"):
        parse_openapi_document("yaml-bytes", fetched(large))
    # The same content as JSON is not subject to the YAML-only limit.
    as_json = json.dumps({"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {},
                          "x-items": ["a" * 2000]}).encode()
    assert parse_openapi_document("json-bytes", fetched(as_json)).operations == []


def test_default_yaml_size_limit_refuses_before_parsing() -> None:
    oversized = b"openapi: 3.0.0\nx: '" + b"a" * openapi.MAX_YAML_BYTES + b"'\n"
    start = time.perf_counter()
    with pytest.raises(OpenAPIDocumentError, match="limit for YAML"):
        parse_openapi_document("yaml-oversized", fetched(oversized))
    assert time.perf_counter() - start < 1.0  # refused before any parsing


# Duplicate keys ---------------------------------------------------------------------------


GET = {"responses": {"204": {"description": "ok"}}}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"openapi": "3.0.3", "paths": {"/v1/a": {"get": {}}, "/v1/a": {"post": {}}}}',
         "invalid JSON: duplicate key '/v1/a'"),
        ('{"openapi": "3.0.3", "paths": {}, "paths": {"/v1/c": {}}}', "duplicate key 'paths'"),
        ("openapi: 3.0.3\npaths:\n  /v1/a:\n    get: {}\n  /v1/a:\n    post: {}\n",
         r"invalid YAML: found duplicate key '/v1/a' \(line 5, column 3\)"),
        ("openapi: 3.0.3\npaths:\n  /v1/a:\n    get: {operationId: one}\n    get: {operationId: two}\n",
         "found duplicate key 'get'"),
        # 200 and '200' are one JSON key: the second would silently replace the first.
        (
            (
                "openapi: 3.0.3\npaths:\n  /v1/a:\n    get:\n      responses:\n"
                "        200: {description: a}\n        '200': {description: b}\n"
            ),
            "found duplicate key '200'",
        ),
    ],
)
def test_duplicate_keys_are_refused_rather_than_silently_dropping_operations(text: str, message: str) -> None:
    with pytest.raises(OpenAPIDocumentError, match=message):
        parse_openapi_document("duplicates", fetched(text.encode()))


def test_yaml_merge_keys_may_still_be_overridden() -> None:
    text = """
openapi: 3.0.3
info: {title: merges, version: '1'}
x-base: &base
  description: base response
  content: {application/json: {schema: {type: string}}}
paths:
  /v1/a:
    get:
      responses:
        '200':
          <<: *base
          description: overridden description
"""
    (op,) = parse_openapi_document("merges", fetched(text.encode())).operations
    assert op.response_schema == {"type": "string", STATUS: "200", CONTENT_TYPE: "application/json"}


# Non-finite numbers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "media_type"),
    [
        (".nan", "application/yaml"),
        (".inf", "application/yaml"),
        ("-.inf", "application/yaml"),
        ("1.0e+400", "application/yaml"),
        ("NaN", "application/json"),
        ("Infinity", "application/json"),
        ("-Infinity", "application/json"),
        ("1e400", "application/json"),
    ],
)
def test_non_finite_numbers_are_refused(value: str, media_type: str) -> None:
    if media_type == "application/json":
        text = '{"openapi": "3.0.3", "paths": {}, "x-r": {"maximum": VALUE}}'.replace("VALUE", value)
    else:
        text = f"openapi: 3.0.3\npaths: {{}}\ncomponents:\n  schemas:\n    R: {{maximum: {value}}}\n"
    with pytest.raises(OpenAPIDocumentError, match="non-finite number"):
        parse_openapi_document("non-finite", fetched(text.encode(), media_type=media_type))


def test_finite_extremes_still_parse() -> None:
    text = '{"openapi": "3.0.3", "paths": {}, "x-range": [1e308, -1e308, 5e-324, 0.0, -0.0]}'
    assert parse_openapi_document("finite", fetched(text.encode())).operations == []
