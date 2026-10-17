"""Internal (non-contract) records shared by the discovery submodules.

These are the frozen seams between OpenAPI parsing (``discovery.openapi``), source
route extraction (``discovery.source_fastapi`` / ``discovery.source_express``) and
reconciliation (``discovery.reconcile`` / ``discovery.service``). Contract models
(Endpoint, Evidence, DiscoveryResult, ...) come from ``discovery.contracts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HTTP_METHODS_LOWER = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Marker key placed on a schema node that still holds a ``$ref`` after expansion.
REF_STATUS_KEY = "x-shockwave-ref"
RefStatus = Literal["cycle", "depth_limit", "node_budget", "remote_unsupported", "missing_target"]
# Marker key recording where an inlined local ``$ref`` target came from (a JSON pointer).
ORIGIN_KEY = "x-shockwave-origin"
# Fingerprint of a ``$ref`` target's transitive local closure, stamped next to ORIGIN_KEY
# and on preserved markers, so two snapshots that both stop at the same target can still
# tell whether that target changed (equal digests: unchanged; different: not compared).
REF_DIGEST_KEY = "x-shockwave-ref-digest"

# Coverage-gap vocabulary (``DiscoveryResult.coverage.unsupported_patterns`` entries).
# Producers: discovery.openapi / discovery.service. Consumer: analyzer.changes, which must
# not claim an endpoint was removed when the proposed contract was not fully read.
PATTERN_UNPARSEABLE = "openapi:unparseable"  # + " <document ref>"
PATTERN_UNREAD_DOCUMENT = "openapi:unread-document"  # + " <document ref>": requested, never read
PATTERN_NO_DOCUMENTS = "openapi:no-documents"  # the input requested no OpenAPI document at all
PATTERN_UNRESOLVED_PATH_ITEM = "openapi:unresolved-path-item"  # + " <normalized path>"


class DiscoveryError(ValueError):
    """Base class for discovery failures that are reported, never silently ignored."""


class OpenAPIDocumentError(DiscoveryError):
    """The bytes are not a supported, parseable OpenAPI 3.x document (or exceed bounds)."""


@dataclass(frozen=True)
class FetchedDocument:
    """Trusted, already-fetched bytes for one OpenAPI document (supplied by the backend)."""

    content: bytes
    source_uri: str
    media_type: str | None = None  # "application/json" | "application/yaml" | None -> sniff
    revision_label: str | None = None  # e.g. "stripe/openapi@969d8c30"; content hash is always computed
    visibility: Literal["public", "provider_owned"] = "provider_owned"


@dataclass(frozen=True)
class ExpansionLimits:
    """Bounds for inlining local ``$ref`` targets into per-endpoint schemas.

    Beyond these bounds the ``$ref`` is preserved with ``x-shockwave-ref`` set to the
    reason, so large schema graphs (Stripe) stay bounded instead of exploding.
    """

    max_ref_depth: int = 2
    max_nodes: int = 6000  # per request/response schema
    max_document_nodes: int = 2_000_000  # all inlined nodes across one document (Stripe: ~0.66M)
    max_operations: int = 20_000  # operations read from one document (Stripe: 644)


@dataclass(frozen=True)
class DocumentProvenance:
    document_ref: str  # the DiscoveryInput.openapi_documents entry
    source_uri: str
    sha256: str
    byte_size: int
    openapi_version: str
    info_title: str | None
    info_version: str | None  # API version declared by the document, e.g. "2026-09-30.endive"
    revision_label: str | None
    visibility: Literal["public", "provider_owned"]
    path_count: int
    operation_count: int
    component_schema_count: int
    server_urls: tuple[str, ...]


@dataclass(frozen=True)
class DocumentedOperation:
    """One method+path operation from an OpenAPI document, with provenance."""

    method: str  # uppercase
    path: str  # server base path joined with the documented path, normalized
    documented_path: str  # the key under /paths exactly as written
    operation_id: str | None
    summary: str | None
    deprecated: bool
    request_schema: dict
    response_schema: dict
    json_pointer: str  # e.g. "/paths/~1v1~1charges~1{id}/get"
    document_ref: str


@dataclass
class ParsedDocument:
    provenance: DocumentProvenance
    operations: list[DocumentedOperation]
    warnings: list[str] = field(default_factory=list)
    unsupported_patterns: list[str] = field(default_factory=list)
    # Documented paths (server base joined, normalized) whose path item could not be
    # resolved (remote/missing/cyclic $ref): their operations are unknown, not absent.
    unresolved_paths: list[str] = field(default_factory=list)
    warnings_omitted: int = 0  # distinct warnings past the parser's cap (not in ``warnings``)
    patterns_omitted: int = 0  # distinct patterns past the parser's cap


@dataclass(frozen=True)
class SourceRoute:
    """A route registration found by static analysis of provider source (never executed)."""

    framework: Literal["fastapi", "express"]
    method: str  # uppercase
    path: str  # normalized, "{param}" syntax, leading "/", no trailing "/"
    raw_path: str  # full path as composed from source literals before normalization
    param_mapping: tuple[tuple[str, str], ...]  # (normalized name, source token), e.g. ("id", ":id")
    file: str  # source-root-relative POSIX path
    line_start: int
    line_end: int
    handler: str | None
    excerpt: str
    confidence: Literal["direct", "inferred"]
    include_in_schema: bool | None = None  # FastAPI include_in_schema when literal


@dataclass
class SourceScan:
    routes: list[SourceRoute] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
    unsupported_patterns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def operation_pointer(documented_path: str, method: str) -> str:
    return f"/paths/{escape_pointer_token(documented_path)}/{method.lower()}"


def operation_from_pointer(json_pointer: str | None) -> tuple[str, str] | None:
    """Parse ``/paths/<escaped path>/<method>`` into (METHOD, documented path)."""
    if not json_pointer or not json_pointer.startswith("/paths/"):
        return None
    tokens = json_pointer.split("/")
    if len(tokens) != 4 or tokens[3] not in HTTP_METHODS_LOWER:
        return None
    return tokens[3].upper(), unescape_pointer_token(tokens[2])


def normalize_path(path: str) -> str:
    """Leading "/", collapsed "//", no trailing "/" (except root). Parameter names untouched."""
    collapsed = "/" + "/".join(segment for segment in path.split("/") if segment)
    return collapsed


@dataclass(frozen=True)
class ContractGaps:
    """What a DiscoveryResult's coverage says it could not read (see the PATTERN_* vocabulary)."""

    incomplete_documents: tuple[str, ...]  # gap patterns naming whole documents not read
    unresolved_path_shapes: frozenset[str]  # ``path_shape`` of documented paths with unknown operations

    @property
    def documents_incomplete(self) -> bool:
        return bool(self.incomplete_documents)

    def covers(self, path: str) -> bool:
        """True when ``path``'s operations may exist in the contract but could not be read."""
        return self.documents_incomplete or path_shape(path) in self.unresolved_path_shapes


def contract_gaps(unsupported_patterns: list[str] | tuple[str, ...]) -> ContractGaps:
    """Parse the coverage-gap patterns of one DiscoveryResult."""
    whole: list[str] = []
    shapes: set[str] = set()
    for pattern in unsupported_patterns:
        head, _, rest = pattern.partition(" ")
        if head in (PATTERN_UNPARSEABLE, PATTERN_UNREAD_DOCUMENT, PATTERN_NO_DOCUMENTS):
            whole.append(pattern)
        elif head == PATTERN_UNRESOLVED_PATH_ITEM and rest.strip():
            shapes.add(path_shape(rest.strip()))
    return ContractGaps(tuple(whole), frozenset(shapes))


def path_shape(path: str) -> str:
    """Path with every "{param}" segment replaced by "{}" for parameter-name-agnostic matching."""
    segments = []
    for segment in normalize_path(path).split("/"):
        segments.append("{}" if segment.startswith("{") and segment.endswith("}") else segment)
    return "/".join(segments)
