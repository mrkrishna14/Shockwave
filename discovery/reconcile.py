"""Documented-versus-implemented reconciliation for one service snapshot.

``merge_documented`` folds the operations of every parsed OpenAPI document into one
entry per (METHOD, path). ``reconcile`` compares those entries with the routes a static
source scan found and builds contract ``Endpoint`` records with evidence and a
reconciliation status. Both are pure functions over records: nothing here fetches,
reads or executes anything, and inputs are never mutated.

The rules follow PRODUCT.md ("reconcile only sources for the same service, version and
route namespace; an incomplete scan is not proof of absence"):

* One service per call. Endpoint ids derive from (snapshot, service, METHOD, path), so
  identical paths of two services never merge: Stripe's ``/v1/charges/{charge}`` is not
  the fictional demo-payments ``/v1/charges/{id}``.
* The documented namespaces are the first segments of documented paths that carry a
  version segment (``v1`` in ``/v1/charges``, ``api`` in ``/api/v1/charges``) and of
  server base paths (``api`` for a server URL ending in ``/api``), or the whole path space
  when there are none. Documented paths include those whose path item could not be
  resolved. An unmatched source route outside the namespaces (an operational
  ``/healthz``, or a route served without the documented base path) is ``not_compared``,
  never ``implementation_only``: the documents never claimed to describe it.
* Routes match on (METHOD, path shape), ignoring parameter names, because specs and
  frameworks name the same template differently (``{charge}`` vs ``:id``). A name
  difference is reported, not hidden. Documented operations are matched against every
  scanned route, so a documented non-versioned route (``/healthz``) can still be found.
* Absence claims need a comparison that actually happened. A documented route the scan
  did not find is ``documented_not_found_in_scan`` (never "missing"), source that was not
  scanned leaves documented routes ``not_compared``, and when requested documents could
  not be read, unmatched source routes are ``not_compared`` rather than undocumented. So
  is an unmatched source route whose path shape is a documented path whose path item
  could not be resolved (a remote ``$ref``): its documented operations are unknown.
* Disagreement surfaces as ``conflict``: one route documented with different schemas,
  two documented templates with the same shape, or one route registered by different
  handlers.
* Evidence is built only from what was observed: an operation pointer in a document
  identified by its content hash, or a registration's file and lines (plus the raw source
  path when normalization changed it). Document URIs are shown through ``display_uri``,
  without userinfo, query or fragment, because consumers' Risks copy this evidence.
* A source-only endpoint has no documented schema, so both of its schemas carry an
  explicit unknown note; its request schema lists only the path parameters its route
  declares, with their source tokens. Untrusted text reaches warnings and excerpts only
  through ``discovery.text``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from discovery.contracts import Endpoint, Evidence
from discovery.ids import endpoint_id, evidence_id
from discovery.records import (
    HTTP_METHODS_LOWER,
    ORIGIN_KEY,
    DocumentedOperation,
    DocumentProvenance,
    ParsedDocument,
    SourceRoute,
    SourceScan,
    normalize_path,
    path_shape,
)
from discovery.text import clean_text, clip_text

__all__ = [
    "IMPLEMENTATION_ONLY_SCHEMA",
    "SCHEMA_NOTE_KEY",
    "SOURCE_ONLY_SCHEMA",
    "SOURCE_TOKEN_KEY",
    "UNKNOWN_SOURCE_REVISION",
    "UNPARSEABLE_URI",
    "DocumentedEndpoint",
    "Reconciliation",
    "display_uri",
    "documented_namespaces",
    "in_namespace",
    "merge_documented",
    "openapi_evidence",
    "reconcile",
    "route_sort_key",
    "source_evidence",
]

UNKNOWN_SOURCE_REVISION = "unknown-source-revision"
# Key on both schemas of a route that has no documented schema: unknown, not empty.
SCHEMA_NOTE_KEY = "x-shockwave-schema"
IMPLEMENTATION_ONLY_SCHEMA = "unknown: implementation-only route has no documented schema"
SOURCE_ONLY_SCHEMA = "unknown: source-only route has no documented schema"
# Key on a path parameter of a source-only request schema: the parameter's source token.
SOURCE_TOKEN_KEY = "x-shockwave-source-token"
# Shown instead of a document URI that cannot be parsed (it might hold credentials).
UNPARSEABLE_URI = "(unparseable URI)"
MAX_EXCERPT_CHARS = 300
MAX_LISTED_ROUTES = 10  # routes named in one aggregated warning before "and N more"
_VERSION_SEGMENT = re.compile(r"v[0-9]+")
_CONTRACT_METHODS = frozenset(method.upper() for method in HTTP_METHODS_LOWER)

_Key = tuple[str, str]  # (METHOD, path shape)
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class DocumentedEndpoint:
    """One documented (METHOD, path) after merging every parsed document."""

    operation: DocumentedOperation  # the first document's operation: operation id and schemas
    evidence: tuple[Evidence, ...]  # openapi evidence from every operation documenting it
    schemas_conflict: bool = False  # a later document documents different schemas


@dataclass
class Reconciliation:
    endpoints: list[Endpoint] = field(default_factory=list)  # sorted by (path, method)
    warnings: list[str] = field(default_factory=list)


# Text and evidence --------------------------------------------------------------------


def display_uri(value: str) -> str:
    """``value`` without userinfo, query or fragment when it is a URL with a host.

    Document references and source URIs reach evidence that consumers' Risks copy, as
    well as warnings and coverage entries, so ``https://user:token@host/spec.yaml?key=...``
    is shown as ``https://host/spec.yaml``: scheme, host and port, and path are kept.
    Values without a host (a repository-relative path, ``stripe/openapi:...``) and URLs
    with nothing to drop are returned unchanged. The content hash, not the URI, pins
    which bytes were read.
    """
    try:
        parts = urlsplit(value)
    except ValueError:  # e.g. an unbalanced "[" in the host: it cannot be shown safely
        return UNPARSEABLE_URI
    if not parts.netloc or ("@" not in parts.netloc and not parts.query and not parts.fragment):
        return value
    host = parts.netloc.rpartition("@")[2]  # where urllib itself splits off userinfo
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def openapi_evidence(operation: DocumentedOperation, provenance: DocumentProvenance) -> Evidence:
    """Operation-level evidence: the operation's JSON pointer in a content-addressed document."""
    summary = clean_text(operation.summary or "")
    excerpt = f"{operation.method} {operation.documented_path}" + (
        f" - {summary}" if summary else ""
    )
    return Evidence(
        id=evidence_id("openapi", provenance.sha256, operation.json_pointer),
        kind="openapi",
        visibility=provenance.visibility,
        source_uri=display_uri(provenance.source_uri or provenance.document_ref),
        revision=f"sha256:{provenance.sha256}",
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=operation.json_pointer,
        excerpt=clip_text(excerpt, MAX_EXCERPT_CHARS),
    )


def source_evidence(route: SourceRoute, source_snapshot_id: str | UUID | None) -> Evidence:
    """Registration evidence: file and lines inside the (ideally pinned) source snapshot.

    The excerpt starts with the raw source path when normalization changed it (an
    Express ``:id`` or a FastAPI converter), so the parameter mapping stays visible.
    """
    snapshot = str(source_snapshot_id) if source_snapshot_id else None  # ids may be UUIDs
    if snapshot:
        source_uri = f"source-snapshot://{snapshot}/{route.file}"
        revision = snapshot
    else:
        source_uri = f"source-root://{route.file}"
        revision = UNKNOWN_SOURCE_REVISION
    note = f"raw path {route.raw_path}" if route.raw_path != route.path else ""
    excerpt = clip_text(
        "; ".join(text for text in (note, route.excerpt) if text.strip()), MAX_EXCERPT_CHARS
    )
    return Evidence(
        id=evidence_id(
            "source",
            snapshot or "unknown",
            route.file,
            route.line_start,
            route.method,
            route.path,
        ),
        kind="source",
        visibility="provider_owned",
        source_uri=source_uri,
        revision=revision,
        file=route.file,
        line_start=route.line_start,
        line_end=route.line_end,
        json_pointer=None,
        excerpt=excerpt or None,
    )


def route_sort_key(route: SourceRoute) -> tuple[str, str, str, int, str, str]:
    return (
        route.path,
        route.method,
        route.file,
        route.line_start,
        route.framework,
        route.handler or "",
    )


# Documents ------------------------------------------------------------------------------


def merge_documented(
    documents: Sequence[ParsedDocument],
) -> tuple[list[DocumentedEndpoint], list[str]]:
    """One entry per documented (METHOD, path), in document order.

    The first operation wins; later ones only add their evidence, and differing request
    or response schemas mark the entry as a conflict. A byte-identical document yields the
    same evidence ids and therefore adds nothing.
    """
    # Operations are keyed by (METHOD, path) here, not by shape: two documented templates
    # that differ only in parameter names stay separate and are flagged during reconcile.
    entries: dict[tuple[str, str], DocumentedEndpoint] = {}
    warnings: list[str] = []
    first_by_hash: dict[str, str] = {}
    for parsed in documents:
        provenance = parsed.provenance
        original = first_by_hash.setdefault(provenance.sha256, provenance.document_ref)
        if original != provenance.document_ref:
            warnings.append(
                f"openapi document {_document_name(provenance.document_ref)} has the same"
                f" content (sha256) as {_document_name(original)}; it adds no new evidence"
            )
        for operation in parsed.operations:
            evidence = openapi_evidence(operation, provenance)
            key = (operation.method, operation.path)
            entry = entries.get(key)
            if entry is None:
                entries[key] = DocumentedEndpoint(operation, (evidence,))
            elif all(item.id != evidence.id for item in entry.evidence):
                entries[key] = _merge_duplicate(entry, operation, evidence, warnings)
    return list(entries.values()), warnings


def _merge_duplicate(
    entry: DocumentedEndpoint,
    operation: DocumentedOperation,
    evidence: Evidence,
    warnings: list[str],
) -> DocumentedEndpoint:
    first = entry.operation
    differ = not (
        _same_schema(operation.request_schema, first.request_schema)
        and _same_schema(operation.response_schema, first.response_schema)
    )
    outcome = (
        "their schemas differ, so the endpoint is marked conflict"
        if differ
        else "their schemas match"
    )
    warnings.append(
        f"{first.method} {clip_text(first.path)} is documented more than once"
        f" ({_document_location(first)}; {_document_location(operation)}); the first is kept"
        f" and {outcome}"
    )
    return DocumentedEndpoint(first, (*entry.evidence, evidence), entry.schemas_conflict or differ)


def _document_name(document_ref: str) -> str:
    return clip_text(display_uri(document_ref))


def _document_location(operation: DocumentedOperation) -> str:
    return f"{_document_name(operation.document_ref)} {clip_text(operation.json_pointer)}"


def _same_schema(left: object, right: object) -> bool:
    """Equal as JSON, ignoring only which component an inlined schema came from.

    Two documents may name the same schema differently (``Charge`` vs ``ChargeV1``); that
    is not a disagreement about the contract. Any other difference is, including text, and
    JSON types are compared strictly (``true`` is not ``1``). Iterative, because inlined
    schemas can be deep.
    """
    pending = [(left, right)]
    while pending:
        a, b = pending.pop()
        if isinstance(a, dict) and isinstance(b, dict):
            keys = a.keys() - {ORIGIN_KEY}
            if keys != b.keys() - {ORIGIN_KEY}:
                return False
            pending.extend((a[key], b[key]) for key in keys)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return False
            pending.extend(zip(a, b, strict=True))
        elif type(a) is not type(b) or a != b:
            return False
    return True


# Namespaces -----------------------------------------------------------------------------


def _first_segment(path: str) -> str:
    return normalize_path(path).split("/")[1]  # "" for the root path "/"


def documented_namespaces(paths: Iterable[str]) -> frozenset[str]:
    """First segments of the versioned documented paths; empty means the whole path space.

    A path is versioned when one of its segments is a version: ``/v1/charges`` gives
    ``v1``, and ``/api/v1/charges`` (a server base path or a prefix written in the
    document) gives ``api``.
    """
    namespaces: set[str] = set()
    for path in paths:
        segments = normalize_path(path).split("/")[1:]
        if any(_VERSION_SEGMENT.fullmatch(segment) for segment in segments):
            namespaces.add(segments[0])
    return frozenset(namespaces)


def _base_path_namespaces(documented: Iterable[DocumentedEndpoint]) -> frozenset[str]:
    """First segments of server base paths (``/api``), versioned or not.

    An operation's path is its server base path joined with the path it is documented
    under; the documents then describe only what is under that base path.
    """
    namespaces: set[str] = set()
    for entry in documented:
        path, written = entry.operation.path, normalize_path(entry.operation.documented_path)
        if written == "/":
            base = "" if path == "/" else path
        else:
            base = path[: -len(written)] if path.endswith(written) else ""
        if base:
            namespaces.add(_first_segment(base))
    return frozenset(namespaces)


def in_namespace(path: str, namespaces: frozenset[str]) -> bool:
    return not namespaces or _first_segment(path) in namespaces


# Reconciliation -------------------------------------------------------------------------


def reconcile(
    documented: Sequence[DocumentedEndpoint],
    scan: SourceScan | None,
    *,
    service_id: str,
    snapshot_id: str | UUID,
    source_snapshot_id: str | UUID | None,
    documentation_gap: str | None = None,
    unresolved_paths: Iterable[str] = (),
) -> Reconciliation:
    """Classify documented operations and scanned routes into contract endpoints.

    ``scan`` is None when no source was scanned. ``documentation_gap`` says why the
    documents may not describe the whole service (none were requested, or some could not
    be read); unmatched source routes are then ``not_compared`` instead of
    ``implementation_only``. ``unresolved_paths`` are documented paths (server base path
    joined, as ``ParsedDocument.unresolved_paths``) whose path item could not be resolved:
    an unmatched source route with the same path shape is ``not_compared``, any method.
    Ids may be strings or UUID objects.
    """
    reconciler = _Reconciler(
        service_id, snapshot_id, source_snapshot_id, documentation_gap, unresolved_paths
    )
    return reconciler.run(documented, scan)


class _Reconciler:
    """Holds the identity of one reconciliation and the warnings it produces."""

    def __init__(
        self,
        service_id: str,
        snapshot_id: str | UUID,
        source_snapshot_id: str | UUID | None,
        documentation_gap: str | None,
        unresolved_paths: Iterable[str],
    ) -> None:
        self._service_id = service_id
        self._snapshot_id = str(snapshot_id)
        self._source_snapshot_id = None if source_snapshot_id is None else str(source_snapshot_id)
        self._documentation_gap = documentation_gap
        self._unresolved_paths = frozenset(unresolved_paths)
        self._unresolved_shapes = frozenset(path_shape(path) for path in self._unresolved_paths)
        self._summary: list[str] = []  # aggregated statements, listed first
        self._details: list[str] = []  # per-endpoint notes, listed after

    def run(
        self, documented: Sequence[DocumentedEndpoint], scan: SourceScan | None
    ) -> Reconciliation:
        routes = self._comparable_routes(scan)
        namespaces = documented_namespaces(
            [*(entry.operation.path for entry in documented), *self._unresolved_paths]
        ) | _base_path_namespaces(documented)
        documented_groups = _group(
            documented, lambda entry: _key(entry.operation.method, entry.operation.path)
        )
        route_groups = _group(
            sorted(routes or [], key=route_sort_key), lambda route: _key(route.method, route.path)
        )

        endpoints: list[Endpoint] = []
        for key in sorted(documented_groups, key=_key_order):
            endpoints.extend(
                self._documented(
                    documented_groups[key], route_groups.get(key, []), routes is not None
                )
            )

        outside: list[SourceRoute] = []
        uncompared: list[SourceRoute] = []
        unresolved: list[SourceRoute] = []
        for key in sorted(route_groups.keys() - documented_groups.keys(), key=_key_order):
            group = route_groups[key]
            endpoint = self._source_only(group, namespaces)
            endpoints.append(endpoint)
            if endpoint.reconciliation == "not_compared":
                if key[1] in self._unresolved_shapes:
                    bucket = unresolved
                elif in_namespace(group[0].path, namespaces):
                    bucket = uncompared
                else:
                    bucket = outside
                bucket.append(group[0])

        self._summarize(namespaces, outside, uncompared, unresolved, endpoints, scan)
        endpoints.sort(key=lambda endpoint: (endpoint.path, endpoint.method))
        return Reconciliation(endpoints=endpoints, warnings=[*self._summary, *self._details])

    # Inputs ---------------------------------------------------------------------------

    def _comparable_routes(self, scan: SourceScan | None) -> list[SourceRoute] | None:
        """Routes to compare with, or None when no comparison with source is possible."""
        if scan is None:
            return None
        if scan.files_scanned == 0 and not scan.routes:
            self._summary.append(
                "the source scan read no files, so documented operations were not compared"
                " with source"
            )
            return None
        usable = []
        for route in scan.routes:
            if route.method in _CONTRACT_METHODS and route.path.startswith("/"):
                usable.append(route)
            else:
                self._summary.append(
                    f"source route {clip_text(route.method)} {clip_text(route.path)}"
                    f" ({_location(route)}) has no contract representation and was ignored"
                )
        return usable

    # Classification -------------------------------------------------------------------

    def _documented(
        self, entries: list[DocumentedEndpoint], matched: list[SourceRoute], scanned: bool
    ) -> list[Endpoint]:
        ambiguous = len(entries) > 1
        if ambiguous:
            paths = ", ".join(clip_text(entry.operation.path) for entry in entries)
            self._summary.append(
                f"{entries[0].operation.method} {paths} are documented as separate operations"
                " with the same path template (only parameter names differ); each is marked"
                " conflict"
            )
        source_conflict = self._note_registrations(matched)
        endpoints = []
        for entry in entries:
            operation = entry.operation
            for route in matched:
                self._note_parameter_names(operation, route)
            if entry.schemas_conflict or ambiguous or source_conflict:
                status = "conflict"
            elif not scanned:
                status = "not_compared"
            else:
                status = "documented_and_found" if matched else "documented_not_found_in_scan"
            endpoints.append(
                self._endpoint(
                    operation.method,
                    operation.path,
                    operation_id=operation.operation_id,
                    request_schema=operation.request_schema,
                    response_schema=operation.response_schema,
                    evidence=[*entry.evidence, *self._source_evidence(matched)],
                    reconciliation=status,
                    confidence="direct",
                )
            )
        return endpoints

    def _source_only(self, group: list[SourceRoute], namespaces: frozenset[str]) -> Endpoint:
        first = group[0]
        if self._note_registrations(group):
            status = "conflict"
        elif (
            not in_namespace(first.path, namespaces)
            or self._documentation_gap
            or path_shape(first.path) in self._unresolved_shapes
        ):
            status = "not_compared"
        else:
            status = "implementation_only"
            for route in group:
                if route.include_in_schema is False:
                    self._details.append(
                        f"{route.method} {clip_text(route.path)} ({_location(route)}) is"
                        " registered with include_in_schema=False: hidden from generated docs"
                        " but still served"
                    )
        note = IMPLEMENTATION_ONLY_SCHEMA if status == "implementation_only" else SOURCE_ONLY_SCHEMA
        return self._endpoint(
            first.method,
            first.path,
            operation_id=None,
            request_schema=_source_request_schema(first, note),
            response_schema={SCHEMA_NOTE_KEY: note},
            evidence=self._source_evidence(group),
            reconciliation=status,
            confidence=_weakest_confidence(group),
        )

    def _note_registrations(self, routes: list[SourceRoute]) -> bool:
        """Note repeated registrations of one (METHOD, shape); True when they conflict."""
        if len(routes) < 2:
            return False
        registrations = "; ".join(_registration(route) for route in routes[:MAX_LISTED_ROUTES])
        if len(routes) > MAX_LISTED_ROUTES:
            registrations += f"; and {len(routes) - MAX_LISTED_ROUTES} more"
        first = routes[0]
        if len({_implementation(route) for route in routes}) > 1:
            self._summary.append(
                f"{first.method} {clip_text(first.path)} is registered by more than one"
                f" handler in source ({registrations}); marked conflict"
            )
            return True
        self._details.append(
            f"{first.method} {clip_text(first.path)} is registered more than once by the same"
            f" handler ({registrations}); every registration is kept as evidence"
        )
        return False

    def _note_parameter_names(self, operation: DocumentedOperation, route: SourceRoute) -> None:
        pairs = [
            (documented, source)
            for documented, source in zip(
                normalize_path(operation.path).split("/"),
                normalize_path(route.path).split("/"),
                strict=True,
            )
            if documented != source  # equal shapes: only parameter names can differ
        ]
        if pairs:
            documented_names = ", ".join(clip_text(name) for name, _ in pairs)
            source_names = ", ".join(clip_text(name) for _, name in pairs)
            self._details.append(
                f"{operation.method} {clip_text(operation.path)}: parameter names differ:"
                f" documented {documented_names} vs source {source_names} ({_location(route)})"
            )

    def _summarize(
        self,
        namespaces: frozenset[str],
        outside: list[SourceRoute],
        uncompared: list[SourceRoute],
        unresolved: list[SourceRoute],
        endpoints: list[Endpoint],
        scan: SourceScan | None,
    ) -> None:
        if outside:
            scope = ", ".join(f"/{segment}" for segment in sorted(namespaces))
            self._summary.append(
                f"{len(outside)} source route(s) are outside the documented namespace ({scope})"
                f" and were not compared with documentation: {_route_list(outside)}"
            )
        if uncompared:
            self._summary.append(
                f"{len(uncompared)} source route(s) with no documented match were not compared"
                f" because {self._documentation_gap}; they are not classified as"
                f" implementation-only: {_route_list(uncompared)}"
            )
        if unresolved:
            self._summary.append(
                f"{len(unresolved)} source route(s) match a documented path whose path item"
                " could not be resolved (its operations are unknown), so they were not compared"
                f" and are not classified as implementation-only: {_route_list(unresolved)}"
            )
        not_found = sum(
            1 for endpoint in endpoints if endpoint.reconciliation == "documented_not_found_in_scan"
        )
        if not_found and scan is not None and (scan.unsupported_patterns or scan.files_skipped):
            self._summary.append(
                f"{not_found} documented operation(s) were not found in the scanned source; the"
                f" scan reported {len(scan.unsupported_patterns)} unsupported pattern(s) and"
                f" skipped {scan.files_skipped} file(s), which may explain unmatched documented"
                " routes (not found in scan is not proof of absence)"
            )

    # Construction ---------------------------------------------------------------------

    def _source_evidence(self, routes: list[SourceRoute]) -> list[Evidence]:
        return [source_evidence(route, self._source_snapshot_id) for route in routes]

    def _endpoint(
        self,
        method: str,
        path: str,
        *,
        operation_id: str | None,
        request_schema: dict,
        response_schema: dict,
        evidence: list[Evidence],
        reconciliation: str,
        confidence: str,
    ) -> Endpoint:
        return Endpoint(
            id=endpoint_id(self._snapshot_id, self._service_id, method, path),
            service_id=self._service_id,
            method=method,
            path=path,
            snapshot_id=self._snapshot_id,
            operation_id=operation_id,
            request_schema=request_schema,
            response_schema=response_schema,
            evidence=evidence,
            reconciliation=reconciliation,
            confidence=confidence,
        )


# Helpers --------------------------------------------------------------------------------


def _key(method: str, path: str) -> _Key:
    return (method, path_shape(path))


def _key_order(key: _Key) -> tuple[str, str]:
    return (key[1], key[0])


def _group(items: Iterable[_Item], key: Callable[[_Item], _Key]) -> dict[_Key, list[_Item]]:
    groups: dict[_Key, list[_Item]] = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return groups


def _weakest_confidence(routes: list[SourceRoute]) -> str:
    return "inferred" if any(route.confidence == "inferred" for route in routes) else "direct"


def _implementation(route: SourceRoute) -> tuple[object, ...]:
    """What serves a registration: its handler in its file (each unnamed one is distinct)."""
    if route.handler is None:
        return (route.framework, route.file, None, route.line_start)
    return (route.framework, route.file, route.handler)


def _location(route: SourceRoute) -> str:
    return f"{clip_text(route.file)}:{route.line_start}"


def _registration(route: SourceRoute) -> str:
    return f"{_location(route)} {clip_text(route.handler or '<unnamed handler>')}"


def _route_list(routes: list[SourceRoute]) -> str:
    listed = ", ".join(
        f"{route.method} {clip_text(route.path)} ({_location(route)})"
        for route in routes[:MAX_LISTED_ROUTES]
    )
    if len(routes) > MAX_LISTED_ROUTES:
        listed += f", and {len(routes) - MAX_LISTED_ROUTES} more"
    return listed


def _source_request_schema(route: SourceRoute, note: str) -> dict:
    """The unknown request schema of a source-only route, listing only its path parameters.

    Path parameters are what the route's own path declares (``SourceRoute.param_mapping``),
    each with its source token (``:id``, ``{item_id:int}``) and no invented type. Every
    call returns a new dict, so endpoints never share schema state.
    """
    schema: dict = {"type": "object", "properties": {}, SCHEMA_NOTE_KEY: note}
    tokens: dict[str, str] = {}
    for name, token in route.param_mapping:
        tokens.setdefault(name, token)
    if tokens:
        schema["properties"]["path"] = {
            "type": "object",
            "properties": {
                name: {SOURCE_TOKEN_KEY: clip_text(token)} for name, token in tokens.items()
            },
            "required": sorted(tokens),
        }
        schema["required"] = ["path"]
    return schema
