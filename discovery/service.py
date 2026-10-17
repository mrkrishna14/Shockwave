"""Discovery orchestration: fetched OpenAPI documents plus optional provider source in,
one service's endpoint inventory (``DiscoveryResult``) out.

``discover`` is the contract entry point ``discover_endpoints`` plus the records a
dashboard needs (document provenance and every scanned route). The backend resolves
everything before calling: ``fetched_inputs`` maps each requested document reference to
bytes it already fetched under its own allowlist and size bounds, and ``source_root`` is
an authorized checkout. This module never fetches a URL, never opens a path it was handed
as a document, and never imports or runs scanned code: a request is not a fetch command,
and analyzed sources and documents are data.

Every gap is reported instead of papered over. A requested document that was not fetched
or could not be parsed becomes a warning and a coverage-gap entry
(``openapi:unread-document`` or ``openapi:unparseable``; ``openapi:no-documents`` when
none was requested; ``openapi:unresolved-path-item`` for each documented path whose path
item could not be resolved), an unsupported framework becomes a ``framework:unsupported``
entry, and source that was not scanned leaves documented operations ``not_compared``:
unknown, which is not the same as safe. ``analyzer.changes`` relies on the coverage-gap
entries never to claim the removal of an endpoint the proposed contract could not
describe, so no output cap drops one, and every "N more" count includes what the parser
itself left out. Document references are shown through ``display_uri`` (no userinfo,
query or fragment); the raw reference is only the lookup key into ``fetched_inputs``.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from discovery._source_files import checked_source_root
from discovery.contracts import SCHEMA_VERSION, DiscoveryInput, DiscoveryResult
from discovery.openapi import parse_openapi_document
from discovery.reconcile import (
    UNKNOWN_SOURCE_REVISION,
    display_uri,
    merge_documented,
    reconcile,
    route_sort_key,
)
from discovery.records import (
    PATTERN_NO_DOCUMENTS,
    PATTERN_UNPARSEABLE,
    PATTERN_UNREAD_DOCUMENT,
    PATTERN_UNRESOLVED_PATH_ITEM,
    DocumentProvenance,
    ExpansionLimits,
    FetchedDocument,
    OpenAPIDocumentError,
    ParsedDocument,
    SourceRoute,
    SourceScan,
)
from discovery.source_express import scan_express_routes
from discovery.source_fastapi import scan_fastapi_routes
from discovery.text import clean_text, clip_text

__all__ = ["SOURCE_SCANNERS", "DiscoveryOutcome", "discover", "discover_endpoints"]

MAX_WARNINGS = 300  # including the final "... more warning(s) not shown" line
MAX_DOCUMENT_WARNINGS = 50  # parser warnings shown per document
MAX_UNSUPPORTED_PATTERNS = 200  # other patterns; coverage-gap entries are never dropped
# Literal-route scanners by lowercased framework name, in the order they run.
SOURCE_SCANNERS: Mapping[str, Callable[[Path], SourceScan]] = {
    "fastapi": scan_fastapi_routes,
    "express": scan_express_routes,
}
_DEFAULT_LIMITS = ExpansionLimits()  # frozen, so one shared default instance is safe


@dataclass(frozen=True)
class DiscoveryOutcome:
    """The contract result plus the records it was computed from."""

    result: DiscoveryResult
    documents: tuple[DocumentProvenance, ...]  # parsed documents, in request order
    source_routes: tuple[SourceRoute, ...]  # every route the scanners found, sorted


def discover(
    request: DiscoveryInput,
    fetched_inputs: Mapping[str, FetchedDocument | bytes],
    source_root: Path | None,
    *,
    limits: ExpansionLimits = _DEFAULT_LIMITS,
) -> DiscoveryOutcome:
    """Discover one service's endpoints from fetched documents and optional source.

    Raises ``DiscoveryError`` only when ``source_root`` is given and is not a directory;
    documents that are missing or unparseable are reported as warnings.
    """
    root = checked_source_root(Path(source_root)) if source_root is not None else None
    log = _WarningLog(MAX_WARNINGS)
    patterns = _Patterns()
    documents, documentation_gap = _read_documents(
        request.openapi_documents, fetched_inputs or {}, limits, log, patterns
    )
    scan = _scan_source(request, root, log, patterns)
    documented, merge_warnings = merge_documented(documents)
    reconciliation = reconcile(
        documented,
        scan,
        service_id=request.service_id,
        snapshot_id=request.snapshot_id,
        source_snapshot_id=request.source_snapshot_id,
        documentation_gap=documentation_gap,
        unresolved_paths=[path for document in documents for path in document.unresolved_paths],
    )
    log.extend(merge_warnings)
    log.extend(reconciliation.warnings)
    unsupported_patterns = patterns.bounded(MAX_UNSUPPORTED_PATTERNS, log)
    result = DiscoveryResult(
        schema_version=SCHEMA_VERSION,
        service_id=request.service_id,
        snapshot_id=request.snapshot_id,
        endpoints=reconciliation.endpoints,
        coverage={
            "files_scanned": scan.files_scanned if scan is not None else 0,
            "files_skipped": scan.files_skipped if scan is not None else 0,
            "unsupported_patterns": unsupported_patterns,
        },
        warnings=log.messages(),
    )
    return DiscoveryOutcome(
        result=result,
        documents=tuple(document.provenance for document in documents),
        source_routes=tuple(scan.routes) if scan is not None else (),
    )


def discover_endpoints(
    request: DiscoveryInput, fetched_inputs: dict, source_root: Path | None
) -> DiscoveryResult:
    """Contract entry point (CONTRACTS.md): the ``DiscoveryResult`` of ``discover``."""
    return discover(request, fetched_inputs, source_root).result


# Documents ------------------------------------------------------------------------------


def _read_documents(
    refs: Sequence[str],
    fetched_inputs: Mapping[str, object],
    limits: ExpansionLimits,
    log: _WarningLog,
    patterns: _Patterns,
) -> tuple[list[ParsedDocument], str | None]:
    """Parse every requested document that was fetched; returns (parsed, documentation gap)."""
    parsed: list[ParsedDocument] = []
    unread = 0
    listed = Counter(refs)
    for ref in listed:  # request order, each reference once
        if listed[ref] > 1:
            log.add(f"openapi document {_shown(ref)} is listed more than once; it was read once")
        document = _parse_document(ref, fetched_inputs, limits, log, patterns)
        if document is None:
            unread += 1
        else:
            parsed.append(document)
    for key in sorted((key for key in fetched_inputs if key not in listed), key=str):
        log.add(f"fetched input {_shown(str(key))} is not listed in openapi_documents; ignored")
    if not refs:
        patterns.gap(PATTERN_NO_DOCUMENTS)
        return parsed, "no OpenAPI document was requested"
    if unread:
        return parsed, f"{unread} requested OpenAPI document(s) could not be read"
    return parsed, None


def _parse_document(
    ref: str,
    fetched_inputs: Mapping[str, object],
    limits: ExpansionLimits,
    log: _WarningLog,
    patterns: _Patterns,
) -> ParsedDocument | None:
    shown = _shown(ref)
    fetched = None
    if not ref.strip():
        log.add("an empty openapi document reference was ignored")
    else:
        fetched = _as_fetched(ref, fetched_inputs.get(ref), log)
    if fetched is None:
        patterns.gap(f"{PATTERN_UNREAD_DOCUMENT} {shown}")
        return None
    try:
        document = parse_openapi_document(ref, fetched, limits=limits)
    except OpenAPIDocumentError as error:
        log.add(
            f"openapi document {shown} could not be parsed ({clip_text(error)});"
            " its operations are unknown"
        )
        patterns.gap(f"{PATTERN_UNPARSEABLE} {shown}")
        return None
    warnings = document.warnings
    log.extend(
        f"openapi document {shown}: {warning.removeprefix('openapi: ')}"
        for warning in warnings[:MAX_DOCUMENT_WARNINGS]
    )
    # One count for everything not shown: past this cap, and past the parser's own.
    hidden = max(0, len(warnings) - MAX_DOCUMENT_WARNINGS) + document.warnings_omitted
    if hidden:
        log.add(
            f"openapi document {shown}: {hidden} more parser warning(s) not shown",
            stands_for=hidden,
        )
    patterns.document(shown, document, log)
    return document


def _as_fetched(ref: str, value: object, log: _WarningLog) -> FetchedDocument | None:
    """Bytes the backend already fetched; anything else is reported, never opened or fetched."""
    if isinstance(value, FetchedDocument):
        source_uri = display_uri(value.source_uri or ref)
        if source_uri == value.source_uri:
            return value
        return dataclasses.replace(value, source_uri=source_uri)
    if isinstance(value, (bytes, bytearray)):
        return FetchedDocument(content=bytes(value), source_uri=display_uri(ref))
    if value is None:
        log.add(f"openapi document {_shown(ref)} was not fetched; its operations are unknown")
    else:
        log.add(
            f"openapi document {_shown(ref)} was supplied as {type(value).__name__}, not as"
            " fetched bytes; it was not read and its operations are unknown"
        )
    return None


def _shown(ref: str) -> str:
    """A document reference as warnings and coverage entries show it: no credentials."""
    return clip_text(display_uri(ref)) or "(blank)"


# Source ---------------------------------------------------------------------------------


def _scan_source(
    request: DiscoveryInput, root: Path | None, log: _WarningLog, patterns: _Patterns
) -> SourceScan | None:
    """Run the requested scanners; None when the source was not scanned."""
    frameworks = _supported_frameworks(request.frameworks, log, patterns)
    if root is None:
        if request.frameworks:
            log.add("frameworks were requested but no source root was supplied; source not scanned")
        elif request.source_snapshot_id is not None:
            log.add(
                "a source snapshot was named but no source root was supplied; source not scanned"
            )
        return None
    if not request.frameworks:
        log.add("source provided but no frameworks requested; source not scanned")
        return None
    if not frameworks:
        log.add("source provided but no requested framework is supported; source not scanned")
        return None
    if request.source_snapshot_id is None:
        log.add(
            "source was scanned without a source_snapshot_id; source evidence revision is"
            f" {UNKNOWN_SOURCE_REVISION!r}"
        )
    scan = _merge_scans([SOURCE_SCANNERS[name](root) for name in frameworks])
    log.extend(scan.warnings)
    patterns.extend(scan.unsupported_patterns)
    return scan


def _supported_frameworks(
    requested: Iterable[str], log: _WarningLog, patterns: _Patterns
) -> list[str]:
    """Lowercased supported names in scanner order; unknown names are reported."""
    wanted: set[str] = set()
    for name in requested:
        normalized = name.strip().lower()
        if normalized in SOURCE_SCANNERS:
            wanted.add(normalized)
            continue
        shown = clip_text(normalized) or "(blank)"
        log.add(f"framework {shown} is not supported; its routes were not scanned")
        patterns.add(f"framework:unsupported {shown}")
    return [name for name in SOURCE_SCANNERS if name in wanted]


def _merge_scans(scans: Sequence[SourceScan]) -> SourceScan:
    """Each scanner reads only its own file types, so counts add up without overlap."""
    return SourceScan(
        routes=sorted((route for scan in scans for route in scan.routes), key=route_sort_key),
        files_scanned=sum(scan.files_scanned for scan in scans),
        files_skipped=sum(scan.files_skipped for scan in scans),
        unsupported_patterns=sorted(
            {pattern for scan in scans for pattern in scan.unsupported_patterns}
        ),
        warnings=[warning for scan in scans for warning in scan.warnings],
    )


# Output bounds --------------------------------------------------------------------------


class _Patterns:
    """Unsupported patterns for ``DiscoveryResult.coverage``, bounded in one place.

    Coverage-gap entries (the ``PATTERN_*`` vocabulary of ``discovery.records``) say which
    parts of the contract could not be read, so no cap drops one; other patterns share
    the room the cap leaves, and the "... and N more" line also counts the patterns the
    parser left out of its own list.
    """

    def __init__(self) -> None:
        self._gaps: set[str] = set()
        self._unresolved: dict[str, set[str]] = {}  # shown document ref -> path-item entries
        self._other: set[str] = set()
        self._omitted = 0

    def gap(self, entry: str) -> None:
        self._gaps.add(entry)

    def add(self, pattern: str) -> None:
        self._other.add(clean_text(pattern))

    def extend(self, patterns: Iterable[str]) -> None:
        for pattern in patterns:
            self.add(pattern)

    def document(self, shown: str, document: ParsedDocument, log: _WarningLog) -> None:
        """A parsed document's patterns; every unresolved path item is a gap entry."""
        listed: list[str] = []  # paths of the parser's own path-item entries
        for pattern in document.unsupported_patterns:
            if _head(pattern) == PATTERN_UNRESOLVED_PATH_ITEM:
                listed.append(pattern.partition(" ")[2])
            else:
                self.add(pattern)
        # Path-item entries are rebuilt from ``unresolved_paths``, so those the parser's
        # own pattern cap left out are listed here rather than counted as hidden.
        unlisted = max(0, len(set(document.unresolved_paths)) - len(listed))
        self._omitted += max(0, document.patterns_omitted - unlisted)
        paths = {*document.unresolved_paths, *listed}
        if all(path and clip_text(path) == path for path in paths):
            if paths:
                self._unresolved.setdefault(shown, set()).update(
                    f"{PATTERN_UNRESOLVED_PATH_ITEM} {path}" for path in paths
                )
            return
        # A path that cannot be shown verbatim would not match its endpoint: the whole
        # document is recorded as not read instead (its operations count as unknown).
        self.gap(f"{PATTERN_UNREAD_DOCUMENT} {shown}")
        log.add(
            f"openapi document {shown}: a documented path whose path item could not be"
            " resolved cannot be listed verbatim, so the document is recorded as"
            f" {PATTERN_UNREAD_DOCUMENT}"
        )

    def bounded(self, limit: int, log: _WarningLog) -> list[str]:
        """Every gap entry and as many other patterns as fit, sorted; then "... and N more"."""
        gaps = set(self._gaps)
        unresolved = set().union(*self._unresolved.values())
        if len(gaps) + len(unresolved) <= limit:
            gaps |= unresolved
        else:  # too many to list: each such document is recorded as not read instead
            for shown, entries in sorted(self._unresolved.items()):
                gaps.add(f"{PATTERN_UNREAD_DOCUMENT} {shown}")
                log.add(
                    f"openapi document {shown}: its {len(entries)} unresolved path item(s) are"
                    f" recorded as one {PATTERN_UNREAD_DOCUMENT} entry; coverage lists at most"
                    f" {limit} pattern(s)"
                )
        others = sorted(self._other - gaps)
        kept = others[: max(0, limit - len(gaps))]
        hidden = len(others) - len(kept) + self._omitted
        listed = sorted(gaps.union(kept))
        return [*listed, f"... and {hidden} more"] if hidden else listed


def _head(pattern: str) -> str:
    return pattern.partition(" ")[0]


class _WarningLog:
    """Deterministic, de-duplicated, bounded warnings in pipeline order.

    Every message is cleaned with ``discovery.text`` whoever produced it. A message can
    stand for several warnings (a "N more parser warning(s) not shown" line), so the
    final "... N more" count stays exact whichever messages the cap drops.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._messages: list[str] = []
        self._stands_for: list[int] = []  # warnings each kept message accounts for
        self._seen: set[str] = set()
        self._omitted = 0

    def add(self, message: str, *, stands_for: int = 1) -> None:
        message = clean_text(message)
        if message in self._seen:
            return
        self._seen.add(message)
        if len(self._messages) < self._limit:
            self._messages.append(message)
            self._stands_for.append(stands_for)
        else:
            self._omitted += stands_for

    def extend(self, messages: Iterable[str]) -> None:
        for message in messages:
            self.add(message)

    def messages(self) -> list[str]:
        """At most ``limit`` entries; the last one counts every warning that did not fit."""
        if not self._omitted:
            return list(self._messages)
        kept = self._limit - 1
        hidden = self._omitted + sum(self._stands_for[kept:])
        return [*self._messages[:kept], f"... {hidden} more warning(s) not shown"]
