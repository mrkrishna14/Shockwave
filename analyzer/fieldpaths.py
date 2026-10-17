"""Field paths over endpoint schemas, shared by change detection and impact matching.

Grammar: ``response`` | ``response.<prop>(.<prop>)*`` | ``request.<location>(.<prop>)*``
with location in path|query|header|cookie|body. Array elements are written as a
``[]`` suffix on the property (``response.data[].amount``); ``[*]`` and ``[<n>]``
normalize to ``[]``. Internally ``data[]`` is two steps: the property, then its items.

This module is also the single definition of a schema node's children (``subschema``,
``direct_properties``, ``members``), JSON Schema boolean subschemas included: change
detection walks schemas with the same helpers ``field_schema`` uses, so a field the
structural diff reports is one ``field_schema`` finds, and declared-change validation
cannot contradict the diff.
"""

from __future__ import annotations

import re
from typing import Literal

from discovery.reconcile import SCHEMA_NOTE_KEY
from discovery.records import REF_STATUS_KEY

REQUEST_LOCATIONS = ("path", "query", "header", "cookie", "body")
ENDPOINT_WIDE = "*"
ITEMS = "[]"
MAX_FIELD_DEPTH = 32
COMPOSITION_KEYWORDS = ("allOf", "anyOf", "oneOf")

_INDEX = re.compile(r"\[(?:\*|\d+)?\]")

Relation = Literal["equal", "ancestor", "descendant", "unrelated"]


def normalize_field_path(path: str) -> str:
    """Canonical spelling: trimmed, indices as ``[]``, no empty segments."""
    if path.strip() == ENDPOINT_WIDE:
        return ENDPOINT_WIDE
    normalized = _INDEX.sub(ITEMS, path.strip())
    return ".".join(segment for segment in normalized.split(".") if segment)


def split_field_path(path: str) -> list[str]:
    """``response.data[].amount`` -> ``["response", "data", "[]", "amount"]``."""
    steps: list[str] = []
    for segment in normalize_field_path(path).split("."):
        name = segment
        array_depth = 0
        while name.endswith(ITEMS):
            name = name[: -len(ITEMS)]
            array_depth += 1
        if name:
            steps.append(name)
        steps.extend([ITEMS] * array_depth)
    return steps


def is_api_field_path(path: str) -> bool:
    """True for response fields and located request fields; False for local names."""
    steps = split_field_path(path)
    if not steps:
        return False
    if steps[0] == "response":
        return True
    return steps[0] == "request" and len(steps) >= 2 and steps[1] in REQUEST_LOCATIONS


def relation(path: str, other: str) -> Relation:
    """How ``path`` relates to ``other``: an ancestor contains ``other``.

    ``*`` (endpoint-wide: an endpoint removal when structurally breaking, otherwise an
    endpoint-wide behavior change) contains every field path.
    """
    if normalize_field_path(other) == ENDPOINT_WIDE:
        return "equal" if normalize_field_path(path) == ENDPOINT_WIDE else "descendant"
    if normalize_field_path(path) == ENDPOINT_WIDE:
        return "ancestor"
    mine, theirs = split_field_path(path), split_field_path(other)
    shared = min(len(mine), len(theirs))
    if mine[:shared] != theirs[:shared]:
        return "unrelated"
    if len(mine) == len(theirs):
        return "equal"
    return "ancestor" if len(mine) < len(theirs) else "descendant"


# Schema children ------------------------------------------------------------------------


def subschema(value: object) -> dict | None:
    """A child schema as a dict. JSON Schema's boolean ``true`` allows any value ({});
    ``false`` (nothing allowed) and non-schemas are treated as absent."""
    if isinstance(value, dict):
        return value
    return {} if value is True else None


def direct_properties(node: dict) -> dict[str, dict]:
    """The node's own ``properties``, by name, as child schemas (composition not folded in)."""
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return {}
    children: dict[str, dict] = {}
    for name, child in properties.items():
        schema = subschema(child)
        if isinstance(name, str) and schema is not None:
            children[name] = schema
    return children


def members(node: dict, keyword: str, *, expanded_only: bool = True) -> list[dict]:
    """The child schemas listed under ``keyword`` (allOf/anyOf/oneOf), as ``subschema`` reads them.

    With ``expanded_only`` (the default), preserved ``$ref`` markers are left out: their
    structure was not expanded, so nothing below them can be looked up.
    """
    listed = node.get(keyword)
    if not isinstance(listed, list):
        return []
    found: list[dict] = []
    for member in listed:
        schema = subschema(member)
        if schema is not None and not (expanded_only and REF_STATUS_KEY in schema):
            found.append(schema)
    return found


def _step(schema: dict, step: str) -> dict | None:
    """The child at one path step: the first match in the node, then its expanded members."""
    if REF_STATUS_KEY in schema:  # preserved $ref: structure beyond it was not expanded
        return None
    variants = [schema]
    for keyword in COMPOSITION_KEYWORDS:
        variants.extend(members(schema, keyword))
    for variant in variants:
        if step == ITEMS:
            child = subschema(variant.get("items"))
        else:
            properties = variant.get("properties")
            child = subschema(properties.get(step)) if isinstance(properties, dict) else None
        if child is not None:
            return child
    return None


def field_schema(request_schema: dict, response_schema: dict, path: str) -> dict | None:
    """The schema node addressed by ``path`` in an Endpoint's schemas, or None if unknown.

    None covers both "absent" and "behind an unexpanded $ref"; callers that need to tell
    those apart should check ``is_behind_unexpanded_ref``. A field declared with the
    boolean schema ``true`` is found and returned as ``{}`` (any value), so compare the
    result with None rather than testing its truth.
    """
    steps = split_field_path(path)
    if not steps:
        return None
    if steps[0] == "response":
        node: dict | None = response_schema
        rest = steps[1:]
    elif steps[0] == "request" and len(steps) >= 2 and steps[1] in REQUEST_LOCATIONS:
        node = _step(request_schema, steps[1])
        rest = steps[2:]
    else:
        return None
    for step in rest:
        if node is None:
            return None
        node = _step(node, step)
    return node


def is_behind_unexpanded_ref(request_schema: dict, response_schema: dict, path: str) -> bool:
    """True when walking ``path`` stops at a preserved ``$ref`` (existence unknown)."""
    steps = split_field_path(path)
    if not steps or steps[0] not in ("response", "request"):
        return False
    node: dict | None = response_schema if steps[0] == "response" else request_schema
    rest = steps[1:]
    for step in rest:
        if node is None:
            return False
        if REF_STATUS_KEY in node or any(
            REF_STATUS_KEY in member
            for keyword in COMPOSITION_KEYWORDS
            for member in members(node, keyword, expanded_only=False)
        ):
            return True
        node = _step(node, step)
    return False


def is_unknown_schema(request_schema: dict, response_schema: dict, path: str) -> bool:
    """True when walking ``path`` meets a schema discovery marked unknown (``x-shockwave-schema``).

    Discovery puts that note on both schemas of a source-only endpoint: nothing is
    documented there, so a field the walk cannot find is unknown, not absent.
    """
    steps = split_field_path(path)
    if not steps or steps[0] not in ("response", "request"):
        return False
    node: dict | None = response_schema if steps[0] == "response" else request_schema
    for step in steps[1:]:
        if node is None:
            return False
        if SCHEMA_NOTE_KEY in node:
            return True
        node = _step(node, step)
    return node is not None and SCHEMA_NOTE_KEY in node
