"""Contract change detection: deterministic structural diffs plus explicit declared changes.

``detect_changes`` compares two discovery snapshots of one service and folds in the
changes a provider declared. The two sources are held to different standards of proof:

* Structural changes come only from structural JSON-schema keywords (type, nullable,
  format, enum/const, required, property presence, items, preserved ``$ref`` targets and
  their fingerprints) of documented endpoints matched by canonical identity (service,
  METHOD, path); endpoint record ids are snapshot-specific and never used for matching.
  Descriptions, examples, titles and other ``x-shockwave-*`` annotations never produce a
  change. Breaking-ness is judged from the consumer's side: a response is read, so values
  it could not carry before break readers; a request is sent, so values that stop being
  accepted break senders. Schemas are walked with ``analyzer.fieldpaths``' child
  helpers, the same ones declared-change validation looks fields up with.
* Semantic kinds (``units_changed``, ``behavior_changed``) are never inferred, neither
  from an unchanged schema nor from description text. They exist only as declared
  changes with explicit evidence. Invalid declarations are rejected as a whole
  (``InvalidDeclaredChange``) instead of being dropped or repaired, a submitted
  ``structural_breaking`` is recomputed rather than trusted, and evidence may not use the
  reserved ``shockwave-scenario://`` scheme (only impact analysis adds scenario pointers).

shockwave.v1 has no ``endpoint_removed`` kind, so a documented endpoint missing from the
proposed contract is reported as one ``behavior_changed`` on the endpoint-wide path ``*``.
That is claimed only when the proposed contract was read: if its coverage reports that
the endpoint's operations could not be read (an unread or unparseable document, no
document at all, or an unresolved path item), ``detect_changes`` raises
``ProposedContractIncomplete`` instead, because unknown is not absent.

Document text (enum values, formats, ``$ref`` targets) reaches change semantics and
evidence excerpts only through ``discovery.text``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

from analyzer.fieldpaths import (
    COMPOSITION_KEYWORDS,
    ENDPOINT_WIDE,
    ITEMS,
    MAX_FIELD_DEPTH,
    direct_properties,
    field_schema,
    is_api_field_path,
    is_behind_unexpanded_ref,
    is_unknown_schema,
    members,
    normalize_field_path,
    subschema,
)
from discovery.contracts import APIChange, DiscoveryResult, Endpoint, Evidence
from discovery.ids import change_id, evidence_id
from discovery.records import (
    ORIGIN_KEY,
    PATTERN_UNRESOLVED_PATH_ITEM,
    REF_DIGEST_KEY,
    REF_STATUS_KEY,
    contract_gaps,
    escape_pointer_token,
    operation_from_pointer,
)
from discovery.text import clean_text, clip_text
from verification.scenarios.catalog import SCENARIO_URI_PREFIX, is_scenario_pointer

__all__ = [
    "InvalidDeclaredChange",
    "ProposedContractIncomplete",
    "detect_changes",
    "validate_declared_changes",
]

Role = Literal["response", "request"]
Side = Literal["old", "new"]
Identity = tuple[str, str, str]
Located = tuple[Side, str | None, dict]  # (snapshot side, JSON pointer if known, schema node)

SEMANTIC_KINDS = frozenset({"units_changed", "behavior_changed"})
EXPLICIT_EVIDENCE_KINDS = frozenset({"declared_change", "docs", "source"})
ENDPOINT_REMOVED_OLD = "endpoint documented in the current contract"
ENDPOINT_REMOVED_NEW = "endpoint absent from the proposed contract"
REFERENCE_NOT_COMPARED = "the referenced schema changed and was not structurally compared"
PARTIALLY_EXPANDED = "part of the schema is behind an unexpanded $ref and was not compared"
MAX_EXCERPT_CHARS = 300
ENUM_DISPLAY_LIMIT = 12
LISTING_LIMIT = 5  # endpoints or coverage gaps named in one message
MAX_LABEL_CHARS = 200

_TYPE_ORDER = ("object", "array", "string", "number", "integer", "boolean", "null")
_MAX_COMPOSITION_DEPTH = 8
_MAX_EXACT_INTEGER = 2**53  # integral floats up to here are compared as the integer they equal


class InvalidDeclaredChange(ValueError):
    """Declared changes rejected as a whole; ``problems`` lists every reason found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems: list[str] = list(problems)
        super().__init__("invalid declared change(s): " + "; ".join(self.problems))


class ProposedContractIncomplete(ValueError):
    """The proposed contract was not fully read, so endpoint removals cannot be established.

    ``endpoints`` ("METHOD path", sorted by path, then method) are documented in the current
    contract but missing from the proposed snapshot, whose coverage reports that their
    operations could not be read: unknown there, not absent. ``gaps`` lists those coverage
    entries. Re-run discovery of the proposed contract with every document read.
    """

    def __init__(self, snapshot_id: object, endpoints: list[str], gaps: list[str]) -> None:
        self.snapshot_id = str(snapshot_id)
        self.endpoints: list[str] = list(endpoints)
        self.gaps: list[str] = list(gaps)
        super().__init__(
            f"proposed contract snapshot {self.snapshot_id} was not fully read "
            f"({_listing_text(self.gaps)}); the removal of {len(self.endpoints)} documented "
            f"endpoint(s) cannot be established because their operations are unknown, not "
            f"absent: {_listing_text(self.endpoints)}"
        )


# Public API -------------------------------------------------------------------------


def validate_declared_changes(
    old: DiscoveryResult, new: DiscoveryResult, declared_changes: list[APIChange]
) -> list[str]:
    """Every reason the declared changes cannot be accepted for ``old`` -> ``new``; [] if none.

    Raises ValueError when ``old`` and ``new`` are not comparable (different services or a
    duplicated endpoint identity), which is a caller error rather than a declaration problem.
    """
    return _declared_problems(_Comparison(old, new), declared_changes)


def detect_changes(
    old: DiscoveryResult,
    new: DiscoveryResult,
    declared_changes: list[APIChange],
    *,
    api_project_id: str | None = None,
) -> list[APIChange]:
    """Structural changes from ``old`` to ``new`` merged with validated declared changes.

    ``api_project_id`` is an additive keyword (the shockwave.v1 signature lacks it) naming
    the project of structural changes. Without it the declared changes' shared project id
    is used; structural changes with no project id at all raise ValueError. Raises
    ProposedContractIncomplete when a documented endpoint is missing from a proposed
    snapshot that reports it could not read that part of the contract, and
    InvalidDeclaredChange for any invalid declaration. Sorted by (path, method,
    field_path, kind); inputs are never mutated.
    """
    comparison = _Comparison(old, new)
    unread = [endpoint for endpoint in comparison.documented_old() if comparison.unread(endpoint)]
    if unread:
        labels = [_label(endpoint) for endpoint in unread]
        raise ProposedContractIncomplete(new.snapshot_id, labels, comparison.gap_reasons(unread))
    problems = _declared_problems(comparison, declared_changes)
    project_id, mismatches = _resolve_project_id(api_project_id, declared_changes)
    problems.extend(mismatches)
    if problems:
        raise InvalidDeclaredChange(problems)

    ranked: list[tuple[tuple[str, ...], APIChange]] = []
    declared_keys: set[tuple[str, str, str]] = set()
    for declared in declared_changes:
        endpoint = comparison.old_by_id[_canonical_uuid(declared.endpoint_id)]
        output = _declared_output(comparison, endpoint, declared)
        declared_keys.add((str(endpoint.id), output.field_path, output.kind))
        ranked.append((_rank(endpoint, output), output))

    structural = [
        (endpoint, finding)
        for endpoint in comparison.documented_old()
        for finding in comparison.findings(endpoint)
        if (str(endpoint.id), finding.field_path, finding.kind) not in declared_keys
    ]
    if structural:
        if project_id is None:
            raise ValueError(
                "api_project_id required: structural changes were found, but no project id was "
                "given as the api_project_id keyword (detect_changes(old, new, declared_changes, "
                "api_project_id=<APIProject id>)) and no declared change supplies one"
            )
        for endpoint, finding in structural:
            output = _structural_change(comparison, endpoint, finding, project_id)
            ranked.append((_rank(endpoint, output), output))
    ranked.sort(key=lambda item: item[0])
    return [change for _, change in ranked]


# Snapshot pairing -----------------------------------------------------------------------


class _Comparison:
    """Two snapshots of one service indexed by canonical identity, with cached diffs."""

    def __init__(self, old: DiscoveryResult, new: DiscoveryResult) -> None:
        if old.service_id != new.service_id:
            raise ValueError(
                f"cannot compare different services: {old.service_id!r} vs {new.service_id!r}"
            )
        self.old = old
        self.new = new
        self.old_index = _index(old)
        self.new_index = _index(new)
        self.old_by_id = {
            _canonical_uuid(endpoint.id): endpoint for endpoint in self.old_index.values()
        }
        self.gaps = contract_gaps(_unsupported_patterns(new))
        self._findings: dict[str, list[_Finding]] = {}

    def counterpart(self, endpoint: Endpoint) -> Endpoint | None:
        return self.new_index.get(_identity(endpoint))

    def documented_old(self) -> list[Endpoint]:
        documented = [endpoint for endpoint in self.old_index.values() if _is_documented(endpoint)]
        return sorted(documented, key=lambda endpoint: (endpoint.path, endpoint.method))

    def unread(self, endpoint: Endpoint) -> bool:
        """Whether a documented old endpoint lacks a documented counterpart only because the
        proposed contract was not fully read there (its operations are unknown, not absent)."""
        return (
            _is_documented(endpoint)
            and not _is_documented(self.counterpart(endpoint))
            and self.gaps.covers(endpoint.path)
        )

    def gap_reasons(self, endpoints: list[Endpoint]) -> list[str]:
        """The proposed snapshot's coverage gaps that leave ``endpoints`` unread."""
        if self.gaps.documents_incomplete:
            return list(self.gaps.incomplete_documents)
        reasons = (f"{PATTERN_UNRESOLVED_PATH_ITEM} {endpoint.path}" for endpoint in endpoints)
        return list(dict.fromkeys(reasons))

    def findings(self, endpoint: Endpoint) -> list[_Finding]:
        """Structural findings for an old endpoint; [] when it has no documented schema."""
        if not _is_documented(endpoint):
            return []
        key = str(endpoint.id)
        if key not in self._findings:
            self._findings[key] = _endpoint_findings(endpoint, self.counterpart(endpoint))
        return self._findings[key]


def _identity(endpoint: Endpoint) -> Identity:
    return (endpoint.service_id, endpoint.method, endpoint.path)


def _label(endpoint: Endpoint) -> str:
    return f"{endpoint.method} {endpoint.path}"


def _unsupported_patterns(result: DiscoveryResult) -> list[str]:
    """``coverage.unsupported_patterns``, whether the contract models coverage or a dict."""
    coverage = result.model_dump(mode="json", include={"coverage"}).get("coverage") or {}
    return [item for item in coverage.get("unsupported_patterns") or [] if isinstance(item, str)]


def _index(result: DiscoveryResult) -> dict[Identity, Endpoint]:
    index: dict[Identity, Endpoint] = {}
    for endpoint in result.endpoints:
        if endpoint.service_id != result.service_id:
            continue  # never merge services, even when paths match
        identity = _identity(endpoint)
        if identity in index:
            raise ValueError(
                f"snapshot {result.snapshot_id} lists {endpoint.method} {endpoint.path} more than "
                "once; endpoint identity must be unique"
            )
        index[identity] = endpoint
    return index


def _is_documented(endpoint: Endpoint | None) -> bool:
    return endpoint is not None and any(item.kind == "openapi" for item in endpoint.evidence)


def _endpoint_findings(old: Endpoint, new: Endpoint | None) -> list[_Finding]:
    findings = _Findings()
    if new is None or not _is_documented(new):
        findings.add(
            ENDPOINT_WIDE,
            "behavior_changed",
            ENDPOINT_REMOVED_OLD,
            ENDPOINT_REMOVED_NEW,
            breaking=True,
        )
        return findings.ordered()
    _Differ("response", findings).compare(
        old.response_schema,
        new.response_schema,
        "response",
        _origin_pointer(old.response_schema),
        _origin_pointer(new.response_schema),
        0,
    )
    _compare_request(old.request_schema, new.request_schema, _Differ("request", findings))
    return findings.ordered()


# Findings -------------------------------------------------------------------------------


@dataclass
class _Finding:
    """One structural change of an endpoint pair; observations sharing a key are merged."""

    field_path: str
    kind: str
    breaking: bool = False
    semantics: list[tuple[str, str]] = field(default_factory=list)
    locations: list[tuple[Side, str, str]] = field(default_factory=list)  # side, pointer, excerpt

    @property
    def old_semantics(self) -> str:
        return "; ".join(old for old, _ in self.semantics)

    @property
    def new_semantics(self) -> str:
        return "; ".join(new for _, new in self.semantics)


class _Findings:
    """The findings of one endpoint pair, keyed by (field_path, kind)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], _Finding] = {}

    def add(
        self,
        field_path: str,
        kind: str,
        old: str,
        new: str,
        *,
        breaking: bool,
        located: Iterable[Located] = (),
    ) -> None:
        finding = self._by_key.setdefault((field_path, kind), _Finding(field_path, kind))
        finding.breaking = finding.breaking or breaking
        semantics = (clean_text(old), clean_text(new))  # may quote document text
        if semantics not in finding.semantics:
            finding.semantics.append(semantics)
        for side, pointer, node in located:
            if pointer is None:
                continue
            location = (side, pointer, _excerpt(node))
            if location not in finding.locations:
                finding.locations.append(location)

    def ordered(self) -> list[_Finding]:
        return [self._by_key[key] for key in sorted(self._by_key)]


# Structural comparison ------------------------------------------------------------------


@dataclass(frozen=True)
class _Child:
    """A property or items schema plus the JSON pointer of its declaration, when known."""

    schema: dict
    declared_at: str | None

    @property
    def defined_at(self) -> str | None:
        # An inlined $ref target is defined at its origin, not where it is referenced.
        return _origin_pointer(self.schema) or self.declared_at


@dataclass(frozen=True)
class _View:
    """The structural keywords of one schema node, with composition members folded in."""

    types: frozenset[str] | None  # None: unconstrained
    format: str | None
    enum: tuple[str, ...] | None  # canonical JSON encodings in document order; None: no enum
    properties: dict[str, _Child]
    required: frozenset[str]
    items: _Child | None
    unexpanded: frozenset[str]  # $ref targets of composition members that were not expanded
    digests: dict[str, str]  # fingerprints of those targets, where discovery recorded one


class _Differ:
    """Recursive structural comparison for one role (response or request) of an endpoint."""

    def __init__(self, role: Role, findings: _Findings) -> None:
        self.role = role
        self.findings = findings

    def compare(
        self,
        old: dict,
        new: dict,
        path: str,
        old_at: str | None,
        new_at: str | None,
        depth: int,
    ) -> None:
        if depth > MAX_FIELD_DEPTH:
            return
        if REF_STATUS_KEY in old or REF_STATUS_KEY in new:
            self._references(old, new, path, old_at, new_at)
            return
        old_view, new_view = _view(old, old_at), _view(new, new_at)
        located: tuple[Located, ...] = (("old", old_at, old), ("new", new_at, new))
        self._types(old_view, new_view, path, located)
        self._enums(old_view, new_view, path, located)
        self._unexpanded(old_view, new_view, path, located)
        self._properties(old_view, new_view, path, depth)
        self._items(old_view, new_view, path, depth)

    def _references(
        self, old: dict, new: dict, path: str, old_at: str | None, new_at: str | None
    ) -> None:
        old_target, new_target = _reference_target(old), _reference_target(new)
        if old_target is not None and old_target == new_target and not _digests_differ(old, new):
            # Same referenced schema, not known to have changed: not compared deeper, no
            # change claimed. Differing fingerprints mean it changed somewhere unexpanded.
            return
        self.findings.add(
            path,
            "behavior_changed",
            _reference_text(old),
            f"{_reference_text(new)}; {REFERENCE_NOT_COMPARED}",
            breaking=False,
            located=(("old", old_at, old), ("new", new_at, new)),
        )

    def _types(self, old: _View, new: _View, path: str, located: tuple[Located, ...]) -> None:
        gained = _uncovered_types(old.types, new.types)
        lost = _uncovered_types(new.types, old.types)
        format_changed = old.format != new.format
        if not (gained or lost or format_changed):
            return
        if self.role == "response":
            breaking = bool(gained) or (format_changed and old.format is not None)
        else:
            breaking = bool(lost) or (format_changed and new.format is not None)
        self.findings.add(
            path,
            "type_changed",
            _type_text(old.types, old.format),
            _type_text(new.types, new.format),
            breaking=breaking,
            located=located,
        )

    def _enums(self, old: _View, new: _View, path: str, located: tuple[Located, ...]) -> None:
        gained = _uncovered_values(old.enum, new.enum)
        lost = _uncovered_values(new.enum, old.enum)
        if not (gained or lost):
            return
        old_text, new_text = _enum_texts(old.enum, new.enum)
        breaking = bool(gained) if self.role == "response" else bool(lost)
        self.findings.add(
            path, "enum_changed", old_text, new_text, breaking=breaking, located=located
        )

    def _unexpanded(
        self, old: _View, new: _View, path: str, located: tuple[Located, ...]
    ) -> None:
        changed = any(
            _changed_digest(digest, new.digests.get(target))
            for target, digest in old.digests.items()
        )
        if old.unexpanded == new.unexpanded and not changed:
            return
        self.findings.add(
            path,
            "behavior_changed",
            _targets_text(old.unexpanded),
            f"{_targets_text(new.unexpanded)}; {REFERENCE_NOT_COMPARED}",
            breaking=False,
            located=located,
        )

    def _properties(self, old: _View, new: _View, path: str, depth: int) -> None:
        for name in sorted(old.properties):
            child_path = f"{path}.{name}"
            old_child, new_child = old.properties[name], new.properties.get(name)
            if new_child is None:
                self._missing(old_child, new, child_path)
                continue
            self._presence(
                name in old.required, name in new.required, old_child, new_child, child_path
            )
            self.compare(
                old_child.schema,
                new_child.schema,
                child_path,
                old_child.defined_at,
                new_child.defined_at,
                depth + 1,
            )
        if self.role == "request":
            for name in sorted(set(new.properties) - set(old.properties)):
                if name in new.required:
                    self._new_required(new.properties[name], old, f"{path}.{name}")

    def _missing(self, old_child: _Child, new: _View, path: str) -> None:
        located: tuple[Located, ...] = (("old", old_child.declared_at, old_child.schema),)
        documented = f"documented ({_schema_text(old_child.schema)})"
        if new.unexpanded:  # it may live inside a composition member that was not expanded
            self.findings.add(
                path,
                "behavior_changed",
                documented,
                f"not found in the expanded proposed schema; {PARTIALLY_EXPANDED}",
                breaking=False,
                located=located,
            )
            return
        self.findings.add(
            path,
            "field_removed",
            documented,
            "absent from the proposed contract",
            breaking=True,
            located=located,
        )

    def _presence(
        self,
        was_required: bool,
        is_required: bool,
        old_child: _Child,
        new_child: _Child,
        path: str,
    ) -> None:
        located: tuple[Located, ...] = (
            ("old", old_child.declared_at, old_child.schema),
            ("new", new_child.declared_at, new_child.schema),
        )
        if self.role == "response" and was_required and not is_required:
            self.findings.add(
                path,
                "behavior_changed",
                "always present (required)",
                "may be absent (optional)",
                breaking=True,
                located=located,
            )
        elif self.role == "request" and is_required and not was_required:
            self.findings.add(
                path, "behavior_changed", "optional", "required", breaking=True, located=located
            )

    def _new_required(self, new_child: _Child, old: _View, path: str) -> None:
        located: tuple[Located, ...] = (("new", new_child.declared_at, new_child.schema),)
        if old.unexpanded:
            self.findings.add(
                path,
                "behavior_changed",
                f"not found in the expanded current schema; {PARTIALLY_EXPANDED}",
                "required",
                breaking=False,
                located=located,
            )
            return
        self.findings.add(
            path,
            "behavior_changed",
            "not part of the request",
            "required",
            breaking=True,
            located=located,
        )

    def _items(self, old: _View, new: _View, path: str, depth: int) -> None:
        if old.items is None and new.items is None:
            return
        if (old.items is None or new.items is None) and not (
            _allows_array(old.types) and _allows_array(new.types)
        ):
            return  # the type change already reports that it stopped (or started) being an array
        old_items = old.items or _Child({}, None)
        new_items = new.items or _Child({}, None)
        self.compare(
            old_items.schema,
            new_items.schema,
            path + ITEMS,
            old_items.defined_at,
            new_items.defined_at,
            depth + 1,
        )


def _compare_request(old: dict, new: dict, differ: _Differ) -> None:
    """Compare request schemas: synthetic location containers (path, query, ...) plus body."""
    old_locations, new_locations = direct_properties(old), direct_properties(new)
    old_required, new_required = _names(old.get("required")), _names(new.get("required"))
    for location in sorted(set(old_locations) | set(new_locations)):
        old_node, new_node = old_locations.get(location), new_locations.get(location)
        if location == "body":
            _compare_body(
                old_node, new_node, location in old_required, location in new_required, differ
            )
            continue
        # A parameter container's own "required" flag is derived from its parameters, so
        # only parameter-level changes are reported (never a duplicate on the container).
        differ.compare(
            old_node if old_node is not None else {"type": "object"},
            new_node if new_node is not None else {"type": "object"},
            f"request.{location}",
            None,
            None,
            1,
        )


def _compare_body(
    old: dict | None,
    new: dict | None,
    was_required: bool,
    is_required: bool,
    differ: _Differ,
) -> None:
    path = "request.body"
    if old is not None and new is not None:
        if is_required and not was_required:
            differ.findings.add(
                path,
                "behavior_changed",
                "optional request body",
                "required request body",
                breaking=True,
            )
        differ.compare(old, new, path, _origin_pointer(old), _origin_pointer(new), 1)
    elif old is not None:
        differ.findings.add(
            path,
            "field_removed",
            f"request body accepted ({_schema_text(old)})",
            "no request body in the proposed contract",
            breaking=True,
            located=(("old", _origin_pointer(old), old),),
        )
    elif new is not None and is_required:
        differ.findings.add(
            path,
            "behavior_changed",
            "no request body",
            "required request body",
            breaking=True,
            located=(("new", _origin_pointer(new), new),),
        )


# Schema views ---------------------------------------------------------------------------


def _view(node: dict, pointer: str | None) -> _View:
    properties: dict[str, _Child] = {}
    items: _Child | None = None
    for variant, base in _expanded_variants(node, pointer):
        for name, child in direct_properties(variant).items():
            properties.setdefault(name, _Child(child, _join(base, "properties", name)))
        if items is None and (item_schema := subschema(variant.get("items"))) is not None:
            items = _Child(item_schema, _join(base, "items"))
    schema_format = node.get("format")
    unexpanded = _unexpanded_members(node)
    return _View(
        types=_type_set(node, 0),
        format=schema_format if isinstance(schema_format, str) else None,
        enum=_enum_values(node),
        properties=properties,
        required=_required(node),
        items=items,
        unexpanded=frozenset(_reference_label(member) for member in unexpanded),
        digests={
            _reference_label(member): digest
            for member in unexpanded
            if (digest := _digest(member)) is not None
        },
    )


def _expanded_variants(node: dict, pointer: str | None) -> Iterator[tuple[dict, str | None]]:
    """The node, then its expanded allOf/anyOf/oneOf members, each with a pointer base.

    A member's pointer is known only when it is an inlined $ref target (its origin);
    positional pointers into composition lists are not guessed.
    """
    yield node, pointer
    for keyword in COMPOSITION_KEYWORDS:
        for member in members(node, keyword):
            yield member, _origin_pointer(member)


def _unexpanded_members(node: dict) -> list[dict]:
    """The node's allOf/anyOf/oneOf members that are preserved (unexpanded) ``$ref``s."""
    return [
        member
        for keyword in COMPOSITION_KEYWORDS
        for member in members(node, keyword, expanded_only=False)
        if REF_STATUS_KEY in member
    ]


def _names(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))


def _required(node: dict) -> frozenset[str]:
    """Always-present names: own/allOf ``required`` plus those every anyOf/oneOf member requires."""
    names = set(_names(node.get("required")))
    for member in members(node, "allOf"):
        names |= _names(member.get("required"))
    for keyword in ("anyOf", "oneOf"):
        if listed := members(node, keyword):
            names |= frozenset.intersection(*(_names(member.get("required")) for member in listed))
    return frozenset(names)


def _type_set(node: dict, depth: int) -> frozenset[str] | None:
    """JSON types the node allows; None when unconstrained. 3.0 ``nullable`` adds "null"."""
    types = _declared_types(node)
    if types is None and depth < _MAX_COMPOSITION_DEPTH:
        types = _composed_types(node, depth)
    if types is not None and node.get("nullable") is True:
        types = types | {"null"}
    return types


def _declared_types(node: dict) -> frozenset[str] | None:
    declared = node.get("type")
    if isinstance(declared, str):
        return frozenset({declared})
    if isinstance(declared, list) and declared and all(isinstance(item, str) for item in declared):
        return frozenset(declared)
    if isinstance(node.get("properties"), dict):
        return frozenset({"object"})
    if subschema(node.get("items")) is not None:
        return frozenset({"array"})
    return None


def _composed_types(node: dict, depth: int) -> frozenset[str] | None:
    """anyOf/oneOf allow the union of their members' types; allOf the intersection."""
    constraints: list[frozenset[str]] = []
    for keyword in ("anyOf", "oneOf"):
        member_types = [
            _type_set(member, depth + 1) for member in members(node, keyword, expanded_only=False)
        ]
        known = [types for types in member_types if types is not None]
        if member_types and len(known) == len(member_types):
            constraints.append(frozenset().union(*known))
    for member in members(node, "allOf", expanded_only=False):
        types = _type_set(member, depth + 1)
        if types is not None:
            constraints.append(types)
    if not constraints:
        return None
    result = constraints[0]
    for other in constraints[1:]:
        result = _intersect(result, other)
    return result


def _covers(types: frozenset[str] | None, value: str) -> bool:
    """Whether ``types`` admits every value of JSON type ``value`` (integer is a number)."""
    if types is None:
        return True
    return value in types or (value == "integer" and "number" in types)


def _uncovered_types(base: frozenset[str] | None, other: frozenset[str] | None) -> frozenset[str]:
    """Types ``other`` allows that ``base`` does not."""
    if base is None:
        return frozenset()
    if other is None:
        return frozenset({"any"})
    return frozenset(value for value in other if not _covers(base, value))


def _intersect(left: frozenset[str], right: frozenset[str]) -> frozenset[str]:
    return frozenset(value for value in left if _covers(right, value)) | frozenset(
        value for value in right if _covers(left, value)
    )


def _allows_array(types: frozenset[str] | None) -> bool:
    return types is None or "array" in types


def _enum_values(node: dict) -> tuple[str, ...] | None:
    values: tuple[str, ...] | None = None
    if isinstance(node.get("enum"), list):
        values = tuple(dict.fromkeys(_encode(value) for value in node["enum"]))
    if "const" in node:
        const = _encode(node["const"])
        values = (const,) if values is None or const in values else ()
    return values


def _encode(value: object) -> str:
    """Canonical JSON of an enum/const value, so equal JSON values encode equally."""
    return json.dumps(_canonical_numbers(value), sort_keys=True, ensure_ascii=False, default=str)


def _canonical_numbers(value: object) -> object:
    """JSON numbers compare by value: an integral float (``1.0``) becomes the integer it
    equals. Booleans are not numbers here, so ``true`` stays distinct from ``1``."""
    if isinstance(value, float) and value.is_integer() and abs(value) <= _MAX_EXACT_INTEGER:
        return int(value)
    if isinstance(value, list):
        return [_canonical_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_numbers(item) for key, item in value.items()}
    return value


def _uncovered_values(
    base: tuple[str, ...] | None, other: tuple[str, ...] | None
) -> frozenset[str]:
    """Enum values ``other`` allows that ``base`` does not (None: no enum, any value)."""
    if base is None:
        return frozenset()
    if other is None:
        return frozenset({"<any value>"})
    return frozenset(other) - frozenset(base)


# Pointers and text ----------------------------------------------------------------------


def _origin_pointer(node: dict) -> str | None:
    origin = node.get(ORIGIN_KEY)
    if not isinstance(origin, str):
        return None
    pointer = origin.removeprefix("#")
    return pointer if pointer.startswith("/") else None


def _join(base: str | None, *tokens: str) -> str | None:
    if base is None:
        return None
    return base + "".join("/" + escape_pointer_token(token) for token in tokens)


def _reference_target(node: dict) -> str | None:
    """The schema a node stands for: a preserved $ref, or the origin of an inlined one."""
    if REF_STATUS_KEY in node:
        ref = node.get("$ref")
        return ref if isinstance(ref, str) else None
    origin = _origin_pointer(node)
    return f"#{origin}" if origin is not None else None


def _digest(node: dict) -> str | None:
    """Discovery's fingerprint of the ``$ref`` target a node stands for, if recorded."""
    digest = node.get(REF_DIGEST_KEY)
    return digest if isinstance(digest, str) else None


def _changed_digest(old: str | None, new: str | None) -> bool:
    """Both fingerprints recorded and different; a missing one proves nothing either way."""
    return old is not None and new is not None and old != new


def _digests_differ(old: dict, new: dict) -> bool:
    return _changed_digest(_digest(old), _digest(new))


def _reference_label(node: dict) -> str:
    ref = node.get("$ref")
    return ref if isinstance(ref, str) else "an unknown target"


def _reference_text(node: dict) -> str:
    if REF_STATUS_KEY in node:
        return f"references {_reference_label(node)} (not expanded: {node.get(REF_STATUS_KEY)})"
    origin = _origin_pointer(node)
    shape = _schema_text(node)
    return f"inline expansion of #{origin} ({shape})" if origin else f"inline schema ({shape})"


def _targets_text(targets: frozenset[str]) -> str:
    if not targets:
        return "no unexpanded references"
    return "references " + ", ".join(sorted(targets)) + " (not expanded)"


def _type_text(types: frozenset[str] | None, schema_format: str | None) -> str:
    if types is None:
        text = "type: any"
    elif not types:
        text = "type: none (no value is valid)"
    else:
        text = "type: " + " | ".join(sorted(types, key=_type_rank))
    return f"{text}, format: {schema_format}" if schema_format is not None else text


def _type_rank(value: str) -> tuple[int, str]:
    order = _TYPE_ORDER.index(value) if value in _TYPE_ORDER else len(_TYPE_ORDER)
    return order, value


def _schema_text(node: dict) -> str:
    if REF_STATUS_KEY in node:
        return _reference_text(node)
    schema_format = node.get("format")
    return _type_text(
        _type_set(node, 0), schema_format if isinstance(schema_format, str) else None
    )


def _display(encoded: str) -> str:
    value = json.loads(encoded)
    return value if isinstance(value, str) else encoded


def _listing(values: Iterable[str]) -> str:
    shown = list(values)
    text = ", ".join(_display(value) for value in shown[:ENUM_DISPLAY_LIMIT])
    return f"[{text}, ...]" if len(shown) > ENUM_DISPLAY_LIMIT else f"[{text}]"


def _enum_text(values: tuple[str, ...] | None) -> str:
    if values is None:
        return "enum: unconstrained"
    if len(values) > ENUM_DISPLAY_LIMIT:
        return f"enum: {len(values)} values"
    return f"enum: {_listing(values)}"


def _enum_texts(old: tuple[str, ...] | None, new: tuple[str, ...] | None) -> tuple[str, str]:
    old_text, new_text = _enum_text(old), _enum_text(new)
    if old is not None and new is not None and max(len(old), len(new)) > ENUM_DISPLAY_LIMIT:
        added = [value for value in new if value not in old]
        removed = [value for value in old if value not in new]
        new_text += f" (added: {_listing(added)}; removed: {_listing(removed)})"
    return old_text, new_text


def _excerpt(node: dict) -> str:
    """A short summary of a schema node's own keywords, so one pointer yields one excerpt."""
    text = _schema_text(node)
    if REF_STATUS_KEY not in node and (values := _enum_values(node)) is not None:
        text = f"{text}; {_enum_text(values)}"
    return clip_text(text, MAX_EXCERPT_CHARS)


def _listing_text(items: list[str]) -> str:
    """At most LISTING_LIMIT items, each a clean bounded line, plus how many were left out."""
    shown = [clip_text(item, MAX_LABEL_CHARS) for item in items[:LISTING_LIMIT]]
    hidden = len(items) - len(shown)
    return ", ".join(shown) + (f" and {hidden} more" if hidden > 0 else "")


# Evidence and output --------------------------------------------------------------------


def _operation_evidence(endpoint: Endpoint | None) -> list[Evidence]:
    """Operation-level openapi evidence (``/paths/<path>/<method>``), else any openapi evidence."""
    if endpoint is None:
        return []
    openapi = [item for item in endpoint.evidence if item.kind == "openapi"]
    operations = [item for item in openapi if operation_from_pointer(item.json_pointer) is not None]
    return operations or openapi


def _field_evidence(base: Evidence, pointer: str, excerpt: str) -> Evidence:
    return Evidence(
        id=evidence_id("openapi-field", base.revision, pointer),
        kind="openapi",
        visibility=base.visibility,
        source_uri=base.source_uri,
        revision=base.revision,
        file=base.file,
        line_start=None,
        line_end=None,
        json_pointer=pointer,
        excerpt=clip_text(excerpt, MAX_EXCERPT_CHARS),
    )


def _union(items: Iterable[Evidence]) -> list[Evidence]:
    """Evidence de-duplicated by id, first occurrence kept, order preserved."""
    seen: set[str] = set()
    unique: list[Evidence] = []
    for item in items:
        if str(item.id) not in seen:
            seen.add(str(item.id))
            unique.append(item)
    return unique


def _structural_evidence(
    comparison: _Comparison, endpoint: Endpoint, finding: _Finding
) -> list[Evidence]:
    old_operations = _operation_evidence(endpoint)
    counterpart = comparison.counterpart(endpoint)
    new_operations = _operation_evidence(counterpart) if _is_documented(counterpart) else []
    bases = {"old": old_operations[:1], "new": new_operations[:1]}
    field_items = [
        _field_evidence(bases[side][0], pointer, excerpt)
        for side, pointer, excerpt in finding.locations
        if bases[side]
    ]
    copies = [item.model_copy() for item in (*old_operations, *new_operations)]
    return _union([*copies, *field_items])


def _structural_change(
    comparison: _Comparison, endpoint: Endpoint, finding: _Finding, project_id: str
) -> APIChange:
    old, new = comparison.old, comparison.new
    return APIChange(
        id=change_id(
            str(old.snapshot_id),
            str(new.snapshot_id),
            old.service_id,
            endpoint.method,
            endpoint.path,
            finding.field_path,
            finding.kind,
        ),
        api_project_id=project_id,
        service_id=old.service_id,
        endpoint_id=endpoint.id,
        old_snapshot_id=old.snapshot_id,
        new_snapshot_id=new.snapshot_id,
        field_path=finding.field_path,
        kind=finding.kind,
        old_semantics=finding.old_semantics,
        new_semantics=finding.new_semantics,
        structural_breaking=finding.breaking,
        evidence=_structural_evidence(comparison, endpoint, finding),
    )


def _declared_output(
    comparison: _Comparison, endpoint: Endpoint, declared: APIChange
) -> APIChange:
    """The declared change as published.

    Its id, kind, semantics and evidence are kept; ``structural_breaking`` is recomputed for
    the exact field; the old endpoint's operation-level openapi evidence is appended, or the
    whole structural evidence when a structural finding of the same kind merges into it.
    A merged finding's observation stays in the semantics (``<declared>; structural:
    <observed>``, or the observation alone when nothing was declared), so the published
    change still says what its ``structural_breaking`` rests on.
    """
    field_path = normalize_field_path(declared.field_path)
    at_field = [
        finding for finding in comparison.findings(endpoint) if finding.field_path == field_path
    ]
    merged = next((finding for finding in at_field if finding.kind == declared.kind), None)
    old_semantics, new_semantics = declared.old_semantics, declared.new_semantics
    if merged is None:
        extra = [item.model_copy() for item in _operation_evidence(endpoint)]
    else:
        extra = _structural_evidence(comparison, endpoint, merged)
        old_semantics = _with_observation(old_semantics, merged.old_semantics)
        new_semantics = _with_observation(new_semantics, merged.new_semantics)
    evidence = _union([*(item.model_copy() for item in declared.evidence), *extra])
    return declared.model_copy(
        update={
            "field_path": field_path,
            "old_semantics": old_semantics,
            "new_semantics": new_semantics,
            "structural_breaking": any(finding.breaking for finding in at_field),
            "evidence": evidence,
        }
    )


def _with_observation(declared: str, observed: str) -> str:
    if not declared.strip():
        return observed
    if observed in declared:
        return declared
    return f"{declared}; structural: {observed}"


def _rank(endpoint: Endpoint, change: APIChange) -> tuple[str, ...]:
    return (endpoint.path, endpoint.method, change.field_path, change.kind, str(change.id))


def _canonical_uuid(value: object) -> str:
    """Canonical text of a UUID-valued id, so ids compare equal regardless of spelling."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _resolve_project_id(
    explicit: str | None, declared: list[APIChange]
) -> tuple[str | None, list[str]]:
    """The project id for structural changes, plus declared changes that contradict it."""
    if explicit is None:
        project_ids = {_canonical_uuid(change.api_project_id) for change in declared}
        # Disagreement among declared changes is reported by _declared_problems.
        return (project_ids.pop() if len(project_ids) == 1 else None), []
    project_id = _canonical_uuid(explicit)
    mismatches = [
        f"declared change {position} ({change.id}): api_project_id {change.api_project_id} "
        f"does not match the assessed project {project_id}"
        for position, change in enumerate(declared)
        if _canonical_uuid(change.api_project_id) != project_id
    ]
    return project_id, mismatches


# Declared change validation -------------------------------------------------------------


def _declared_problems(comparison: _Comparison, declared: list[APIChange]) -> list[str]:
    problems: list[str] = []
    project_ids = sorted({_canonical_uuid(change.api_project_id) for change in declared})
    if len(project_ids) > 1:
        problems.append("declared changes disagree on api_project_id: " + ", ".join(project_ids))
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    for position, change in enumerate(declared):
        messages = list(_change_problems(comparison, change))
        change_key = _canonical_uuid(change.id)
        key = (
            _canonical_uuid(change.endpoint_id),
            normalize_field_path(change.field_path),
            change.kind,
        )
        if change_key in seen_ids:
            messages.append("reuses the id of an earlier declared change")
        if key in seen_keys:
            messages.append("repeats an earlier declared change for the same endpoint, field, kind")
        seen_ids.add(change_key)
        seen_keys.add(key)
        prefix = f"declared change {position} ({change.id})"
        problems.extend(f"{prefix}: {message}" for message in messages)
    return problems


def _change_problems(comparison: _Comparison, change: APIChange) -> Iterator[str]:
    old, new = comparison.old, comparison.new
    if change.service_id != old.service_id:
        yield (
            f"service_id {change.service_id!r} does not match the compared service "
            f"{old.service_id!r}"
        )
    if _canonical_uuid(change.old_snapshot_id) != _canonical_uuid(old.snapshot_id):
        yield (
            f"old_snapshot_id {change.old_snapshot_id} is not the current contract snapshot "
            f"{old.snapshot_id}"
        )
    if _canonical_uuid(change.new_snapshot_id) != _canonical_uuid(new.snapshot_id):
        yield (
            f"new_snapshot_id {change.new_snapshot_id} is not the proposed contract snapshot "
            f"{new.snapshot_id}"
        )
    yield from _evidence_problems(change)
    yield from _semantics_problems(change)
    endpoint = comparison.old_by_id.get(_canonical_uuid(change.endpoint_id))
    if endpoint is None:
        yield (
            f"endpoint_id {change.endpoint_id} is not an endpoint of the current contract "
            f"snapshot {old.snapshot_id}"
        )
        return
    field_problems = list(_field_problems(comparison, endpoint, change))
    yield from field_problems
    if not field_problems:
        yield from _support_problems(comparison, endpoint, change)


def _explicit_evidence(change: APIChange) -> list[Evidence]:
    return [
        item
        for item in change.evidence
        if item.kind in EXPLICIT_EVIDENCE_KINDS and (item.source_uri or "").strip()
    ]


def _evidence_problems(change: APIChange) -> Iterator[str]:
    if change.kind in SEMANTIC_KINDS and not _explicit_evidence(change):
        yield (
            f"{change.kind} requires explicit evidence (declared_change, docs or source with a "
            "source_uri); schema comparison alone cannot establish a semantic change"
        )
    contents: dict[str, dict] = {}
    for item in change.evidence:
        dumped = item.model_dump(mode="json")
        if contents.setdefault(str(item.id), dumped) != dumped:
            yield f"evidence id {item.id} is used for different evidence"
        if is_scenario_pointer(item):
            yield (
                f"evidence {item.id} uses the reserved {SCENARIO_URI_PREFIX} scheme; scenario "
                "pointers are added by impact analysis, never declared"
            )


def _semantics_problems(change: APIChange) -> Iterator[str]:
    if change.kind not in SEMANTIC_KINDS:
        return
    old_text, new_text = change.old_semantics.strip(), change.new_semantics.strip()
    if not old_text or not new_text:
        yield f"{change.kind} requires non-empty old_semantics and new_semantics"
    elif old_text == new_text:
        yield f"{change.kind} requires old_semantics and new_semantics to differ"


def _field_problems(
    comparison: _Comparison, endpoint: Endpoint, change: APIChange
) -> Iterator[str]:
    field_path = normalize_field_path(change.field_path)
    unread = comparison.unread(endpoint)
    if unread:  # nothing about the proposed side of this endpoint can be checked
        yield (
            f"{clip_text(_label(endpoint), MAX_LABEL_CHARS)} cannot be checked against the "
            f"proposed contract snapshot {comparison.new.snapshot_id}, which was not fully read "
            f"({_listing_text(comparison.gap_reasons([endpoint]))}); its operations there are "
            "unknown, not absent"
        )
    if field_path == ENDPOINT_WIDE:
        if change.kind != "behavior_changed":
            yield "field_path '*' (endpoint-wide) is only valid for behavior_changed"
        return
    counterpart = comparison.counterpart(endpoint)
    if counterpart is None and not unread:
        yield (
            f"{clip_text(_label(endpoint), MAX_LABEL_CHARS)} is absent from the proposed contract "
            f"snapshot {comparison.new.snapshot_id}; declare an endpoint removal as "
            "behavior_changed on '*'"
        )
    if not is_api_field_path(field_path):
        yield (
            f"field_path {change.field_path!r} is not an API field path "
            "(expected response.<field> or request.<location>.<field>)"
        )
        return
    yield from _unresolved_field(endpoint, field_path, "current")
    if counterpart is None or unread:
        return
    if change.kind != "field_removed":
        yield from _unresolved_field(counterpart, field_path, "proposed")
    elif _field_node(counterpart, field_path) is not None:
        yield f"field_removed declared for {field_path}, but the proposed contract documents it"


def _field_node(endpoint: Endpoint, field_path: str) -> dict | None:
    return field_schema(endpoint.request_schema, endpoint.response_schema, field_path)


def _unresolved_field(endpoint: Endpoint, field_path: str, contract: str) -> Iterator[str]:
    if _field_node(endpoint, field_path) is not None:
        return
    if is_behind_unexpanded_ref(endpoint.request_schema, endpoint.response_schema, field_path):
        yield (
            f"cannot verify field: schema not expanded ({field_path} lies behind an unexpanded "
            f"$ref in the {contract} contract)"
        )
    elif is_unknown_schema(endpoint.request_schema, endpoint.response_schema, field_path):
        yield (
            f"cannot verify field: {clip_text(_label(endpoint), MAX_LABEL_CHARS)} has no documented "
            f"schema in the {contract} contract (source-only), so {field_path} is unknown, not absent"
        )
    else:
        yield (
            f"field {field_path} is not in the {contract} contract schema of "
            f"{endpoint.method} {endpoint.path}"
        )


def _support_problems(
    comparison: _Comparison, endpoint: Endpoint, change: APIChange
) -> Iterator[str]:
    """A declared structural kind needs a matching structural finding or explicit evidence."""
    if change.kind in SEMANTIC_KINDS or _explicit_evidence(change):
        return
    field_path = normalize_field_path(change.field_path)
    if not any(
        finding.field_path == field_path and finding.kind == change.kind
        for finding in comparison.findings(endpoint)
    ):
        yield (
            f"{change.kind} at {field_path} is neither found by structural comparison nor backed "
            "by explicit evidence (declared_change, docs or source)"
        )
