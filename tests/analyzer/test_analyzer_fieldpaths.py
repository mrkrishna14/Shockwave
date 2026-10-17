"""Tests for analyzer.fieldpaths: the field-path grammar and the one schema-children walker.

Change detection and declared-change validation both resolve child schemas through these
helpers, so a field the structural diff reports is always one ``field_schema`` finds.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from analyzer.changes import detect_changes
from analyzer.fieldpaths import (
    direct_properties,
    field_schema,
    is_behind_unexpanded_ref,
    is_unknown_schema,
    members,
    normalize_field_path,
    relation,
    split_field_path,
    subschema,
)
from discovery.contracts import DiscoveryResult, Endpoint, Evidence
from discovery.ids import endpoint_id, evidence_id, fixture_uuid
from discovery.records import operation_pointer

SERVICE_ID = "fieldpaths-test"
PATH = "/things/{id}"
OLD = fixture_uuid("test.analyzer-fieldpaths.old")
NEW = fixture_uuid("test.analyzer-fieldpaths.new")
PROJECT_ID = fixture_uuid("test.analyzer-fieldpaths.project")
MARKER = {"$ref": "#/components/schemas/Base", "x-shockwave-ref": "depth_limit"}


def thing() -> dict:
    """A 3.1-style response schema mixing dict and boolean subschemas."""
    return {
        "type": "object",
        "properties": {
            "plain": {"type": "string"},
            "anything": True,  # any value
            "nothing": False,  # no value: never a field
            "tags": {"type": "array", "items": True},
            "nested": {"allOf": [{"type": "object", "properties": {"inner": True}}]},
            "hidden": {"allOf": [{"type": "object", "properties": {"seen": True}}, MARKER]},
        },
    }


def result(snapshot: str, response_schema: dict) -> DiscoveryResult:
    pointer = operation_pointer(PATH, "GET")
    endpoint = Endpoint(
        id=endpoint_id(snapshot, SERVICE_ID, "GET", PATH),
        service_id=SERVICE_ID,
        method="GET",
        path=PATH,
        snapshot_id=snapshot,
        operation_id=None,
        request_schema={},
        response_schema=response_schema,
        evidence=[
            Evidence(
                id=evidence_id("openapi", snapshot, pointer),
                kind="openapi",
                visibility="provider_owned",
                source_uri="things.yaml",
                revision=f"rev-{snapshot}",
                file=None,
                line_start=None,
                line_end=None,
                json_pointer=pointer,
                excerpt=None,
            )
        ],
        reconciliation="not_compared",
        confidence="direct",
    )
    return DiscoveryResult(
        schema_version="shockwave.v1",
        service_id=SERVICE_ID,
        snapshot_id=snapshot,
        endpoints=[endpoint],
        coverage={"files_scanned": 0, "files_skipped": 0, "unsupported_patterns": []},
        warnings=[],
    )


# Grammar ----------------------------------------------------------------------------------


def test_field_path_grammar() -> None:
    assert normalize_field_path(" response.data[0].amount ") == "response.data[].amount"
    assert split_field_path("response.data[*].amount") == ["response", "data", "[]", "amount"]
    assert relation("response.data", "response.data[].amount") == "ancestor"
    assert relation("*", "response.amount") == "ancestor"
    assert relation("response.amount", "response.currency") == "unrelated"


# Schema children --------------------------------------------------------------------------


def test_boolean_true_is_a_schema_that_accepts_anything_and_false_is_absent() -> None:
    node = {"type": "object"}
    assert subschema(node) is node
    assert subschema(True) == {}
    assert [subschema(value) for value in (False, None, "string", ["a"])] == [None] * 4


def test_direct_properties_and_members_resolve_children_alike() -> None:
    schema = thing()
    assert sorted(direct_properties(schema)) == ["anything", "hidden", "nested", "plain", "tags"]
    assert direct_properties(schema)["anything"] == {}
    assert direct_properties({"properties": ["not", "a", "map"]}) == {}

    composed = {"anyOf": [{"type": "string"}, True, False, MARKER]}
    assert members(composed, "anyOf") == [{"type": "string"}, {}]
    assert members(composed, "anyOf", expanded_only=False) == [{"type": "string"}, {}, MARKER]
    assert members({"anyOf": "not a list"}, "anyOf") == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("response.plain", {"type": "string"}),
        ("response.anything", {}),
        ("response.nothing", None),
        ("response.tags[]", {}),
        ("response.nested.inner", {}),
        ("response.hidden.seen", {}),
        ("response.hidden.unseen", None),
        ("response.missing", None),
    ],
)
def test_field_schema_finds_boolean_and_composed_fields(path: str, expected: dict | None) -> None:
    assert field_schema({}, thing(), path) == expected


def test_only_a_missing_field_next_to_an_unexpanded_ref_is_behind_it() -> None:
    schema = thing()
    assert is_behind_unexpanded_ref({}, schema, "response.hidden.unseen") is True
    assert is_behind_unexpanded_ref({}, schema, "response.missing") is False
    assert is_behind_unexpanded_ref({}, {"properties": {"base": MARKER}}, "response.base.id")


def test_request_location_lookup_uses_the_same_children() -> None:
    request = {"type": "object", "properties": {"query": {"properties": {"expand": True}}}}
    assert field_schema(request, {}, "request.query.expand") == {}
    assert field_schema(request, {}, "request.query.limit") is None


# Agreement with the structural diff -------------------------------------------------------


def without(*steps: str) -> Callable[[dict], None]:
    """Remove the property at ``steps`` (through allOf members) from a ``thing`` schema."""

    def edit(schema: dict) -> None:
        node = schema
        for step in steps[:-1]:
            child = node["properties"][step]
            node = child["allOf"][0] if "allOf" in child else child
        del node["properties"][steps[-1]]

    return edit


@pytest.mark.parametrize(
    "steps",
    [("plain",), ("anything",), ("nested", "inner"), ("hidden", "seen")],
    ids=["dict", "boolean", "boolean-in-allOf", "next-to-unexpanded-ref"],
)
def test_a_field_the_diff_reports_is_one_field_schema_finds(steps: tuple[str, ...]) -> None:
    old_schema, new_schema = thing(), thing()
    without(*steps)(new_schema)
    path = "response." + ".".join(steps)

    changes = detect_changes(
        result(OLD, copy.deepcopy(old_schema)),
        result(NEW, copy.deepcopy(new_schema)),
        [],
        api_project_id=PROJECT_ID,
    )

    assert [change.field_path for change in changes] == [path]
    assert field_schema({}, old_schema, path) is not None
    assert field_schema({}, new_schema, path) is None


def test_fields_of_a_schema_discovery_marked_unknown_are_unknown_not_absent() -> None:
    note = {"x-shockwave-schema": "unknown: source-only route has no documented schema"}
    request = {"type": "object", "properties": {"path": {"type": "object", "properties": {"id": {}}}}, **note}
    assert is_unknown_schema(request, note, "response.amount")
    assert is_unknown_schema(request, note, "response")
    assert is_unknown_schema(request, note, "request.query.limit")
    assert not is_unknown_schema({}, {"type": "object", "properties": {}}, "response.amount")
    assert not is_unknown_schema(request, note, "amount")  # not an API field path
