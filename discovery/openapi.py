"""OpenAPI 3.0/3.1 parsing for provider endpoint discovery (Stage A).

``parse_openapi_document`` turns trusted, already-fetched bytes (JSON or YAML) into
``DocumentedOperation`` records: method/path, operation ID, request/response schemas,
JSON-pointer provenance, and counts computed from the document itself. It never
fetches anything, never executes anything, and treats every string in a document
(descriptions included) as data, never as instructions.

Why it is shaped this way:

* Bounds come first. Oversized bytes are refused before parsing, and YAML, whose
  pure-Python loader costs seconds per MiB, has a smaller byte cap and a node cap.
  YAML goes through a SafeLoader subclass that caps alias use and alias expansion
  before any Python objects are built ("billion laughs"). Nesting deeper than
  ``MAX_DOCUMENT_DEPTH`` is refused, and no ``$ref`` target is inlined past
  ``MAX_OUTPUT_DEPTH`` container levels, so every result survives a Pydantic JSON
  round trip (which stops near 200 levels), JSON encoders and marshal.
* Values a JSON result cannot carry faithfully are refused rather than altered:
  duplicate mapping keys (the last one would silently win and drop operations) and
  non-finite numbers (NaN and Infinity have no JSON form).
* Local ``$ref`` targets are inlined as fresh copies, breadth-first through an
  explicit queue. Breadth-first means that when a schema's node budget runs out, the
  deepest structure is what stays unexpanded; the queue means huge or cyclic graphs
  (Stripe) cannot exhaust the stack. A reference that is not inlined is kept as
  ``{"$ref": ..., REF_STATUS_KEY: <reason>}`` -- never dropped, never invented -- and
  remote references are never followed.
* The node budget is reserved per inlined target from its size as written, so one
  schema can never grow past ``max_nodes`` plus the operation's own inline structure.
  A document-wide budget (``max_document_nodes``) bounds everything copied, so shared
  targets, responses or path items cannot fan out across thousands of operations;
  past it, schemas are kept as ``node_budget`` references. A document with more than
  ``max_operations`` operations is refused instead: operations left unread would later
  look removed. For the same reason a ``paths`` that is not a mapping, or is itself a
  ``$ref`` (a whole split-out Paths Object), refuses the document.
* Every kept or inlined local reference carries ``REF_DIGEST_KEY``, a fingerprint of
  its target and everything that target reaches through further local ``$ref``s
  (annotation-only keywords left out), so a consumer comparing two snapshots can tell
  whether a target it did not see expanded changed. Fingerprinting has its own walk
  budget; past it references carry no fingerprint (unknown) and a warning says so.
* OpenAPI 3.1: annotation keywords next to ``$ref`` override the target's own; any
  other sibling applies in addition to the target (JSON Schema 2020-12), so the node
  becomes ``{<siblings>, "allOf": [{"$ref": ...}, ...]}`` -- nothing is merged and
  nothing is lost. OpenAPI 3.0 ignores ``$ref`` siblings.
* Vendor extensions (``x-*``) and literal keywords (``example``, ``examples``,
  ``default``, ``enum``, ``const``) hold data, not schemas, so they are copied
  verbatim and any ``$ref`` inside them stays as written (Stripe's
  ``x-expansionResources`` would otherwise double the catalog). Document keys in the
  reserved ``x-shockwave-`` namespace are dropped so a document cannot forge markers.
* Non-schema references (path items, parameters, request bodies, responses) are
  followed structurally and do not count as schema ``$ref`` hops. A documented path
  whose path item cannot be resolved is recorded in ``unresolved_paths`` (and as an
  ``openapi:unresolved-path-item`` pattern): its operations are unknown, not absent.
* Warnings and patterns are de-duplicated and capped; what a cap leaves out is
  returned as counts (``warnings_omitted`` / ``patterns_omitted``), never as text.
  Document text reaches a warning only through ``discovery.text.clip_text``.
* Results are cached in a small in-process LRU as marshal snapshots keyed by the
  content hash and every other input that affects the result, so each call returns
  deep-independent objects that callers may mutate freely.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import marshal
import math
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

import yaml

from discovery.reconcile import display_uri
from discovery.records import (
    HTTP_METHODS_LOWER,
    ORIGIN_KEY,
    PATTERN_UNRESOLVED_PATH_ITEM,
    REF_DIGEST_KEY,
    REF_STATUS_KEY,
    DocumentedOperation,
    DocumentProvenance,
    ExpansionLimits,
    FetchedDocument,
    OpenAPIDocumentError,
    ParsedDocument,
    escape_pointer_token,
    normalize_path,
    operation_pointer,
    unescape_pointer_token,
)
from discovery.text import clip_text

__all__ = ["CONTENT_TYPE_KEY", "DEFAULT_MAX_BYTES", "STATUS_KEY", "parse_openapi_document"]

DEFAULT_MAX_BYTES = 32 * 1024 * 1024

# Keys this module sets on the root of returned request/response schemas.
STATUS_KEY = "x-shockwave-status"
CONTENT_TYPE_KEY = "x-shockwave-content-type"
_RESERVED_PREFIX = "x-shockwave-"

MAX_WARNINGS = 200
MAX_UNSUPPORTED_PATTERNS = 500
# The pure-Python YAML loader needs ~0.2 s per MiB of long scalars and ~10 us per node,
# so YAML is bounded by size and by node count (Stripe as YAML: 7 MiB, ~312k nodes).
MAX_YAML_BYTES = 8 * 1024 * 1024
MAX_YAML_NODES = 500_000
MAX_YAML_ALIASES = 10_000  # alias events; the expansion cap below is what stops bombs
MAX_YAML_ALIAS_EXPANSION = 100_000  # extra nodes that aliases may add once expanded
MAX_DOCUMENT_DEPTH = 128
# Container levels a returned schema may reach once $ref targets are inlined. A
# DiscoveryResult survives Pydantic's JSON round trip only up to ~197 levels per schema;
# inline structure alone stays within MAX_DOCUMENT_DEPTH (plus _REWRITE_LEVELS).
MAX_OUTPUT_DEPTH = 160
_REWRITE_LEVELS = 2  # a 3.1 $ref-with-siblings rewrite puts the $ref two levels lower
_MAX_COMPONENT_CHAIN = 32  # hops through path item / parameter / body / response refs
_MAX_DEDUPE_KEYS = 10_000
_MAX_FINGERPRINT_NODES = 2_000_000  # values walked to fingerprint $ref targets (Stripe: ~72k)
_CACHE_ENTRIES = 16
_CACHE_MAX_BYTES = 256 * 1024 * 1024

_PARAMETER_LOCATIONS = ("path", "query", "header", "cookie")
# OpenAPI: header parameters with these names SHALL be ignored.
_IGNORED_HEADER_PARAMETERS = frozenset({"accept", "content-type", "authorization"})
_FORM_MEDIA_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
_SUCCESS_STATUS = re.compile(r"2[0-9][0-9]")
_SERVER_VARIABLE = re.compile(r"\{([^{}]*)\}")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]{0,17}")
_LEADING_SPACE = re.compile(rb"(?:\xef\xbb\xbf)?[ \t\r\n]*")
_NON_FINITE = "non-finite number (NaN or Infinity) has no JSON equivalent"
# YAML keys that are not data: merge ("<<") and the rarely used value ("=") key.
_SPECIAL_YAML_KEY_TAGS = frozenset({"tag:yaml.org,2002:merge", "tag:yaml.org,2002:value"})

# How the expander treats a container: a schema (``$ref`` is resolved), a
# name -> schema map (``properties``), or literal data copied verbatim.
_SCHEMA, _NAMES, _LITERAL = 0, 1, 2
_LITERAL_KEYWORDS = frozenset({"example", "examples", "default", "enum", "const"})
_NAME_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
# Keywords that only document a schema: left out of REF_DIGEST_KEY fingerprints.
_ANNOTATION_KEYWORDS = frozenset(
    {"description", "title", "summary", "example", "examples", "default", "externalDocs",
     "deprecated", "$comment"}
)
# 3.1 keywords next to $ref that may override the target's own (everything else applies
# in addition to the target).
_OVERRIDE_KEYWORDS = _ANNOTATION_KEYWORDS | {"readOnly", "writeOnly"}
_MISSING = object()

_OPERATION_FIELDS = tuple(field.name for field in dataclasses.fields(DocumentedOperation))
_DEFAULT_LIMITS = ExpansionLimits()  # frozen, so one shared default instance is safe


def parse_openapi_document(
    document_ref: str,
    fetched: FetchedDocument,
    *,
    limits: ExpansionLimits = _DEFAULT_LIMITS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ParsedDocument:
    """Parse one OpenAPI 3.0/3.1 document into operations with provenance.

    Raises ``OpenAPIDocumentError`` for oversized, unparseable or unsupported input.
    Every call returns objects that share no mutable state with the cache, with the
    parsed document, or with the results of other calls.
    """
    content = fetched.content
    if not isinstance(content, (bytes, bytearray)):
        raise OpenAPIDocumentError("document content must be bytes")
    if len(content) > max_bytes:
        raise OpenAPIDocumentError(
            f"document is {len(content)} bytes, over the {max_bytes}-byte limit"
        )
    content = bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    key = (
        digest,
        document_ref,
        fetched.source_uri,
        fetched.media_type,
        fetched.revision_label,
        fetched.visibility,
        limits,
        max_bytes,
    )
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    parsed = _parse(document_ref, fetched, content, digest, limits)
    _CACHE.put(key, parsed)
    return parsed


def _parse(
    document_ref: str,
    fetched: FetchedDocument,
    content: bytes,
    digest: str,
    limits: ExpansionLimits,
) -> ParsedDocument:
    document = _load_document(content, fetched.media_type)
    version = _openapi_version(document)
    notes = _Notes()
    reader = _OperationReader(document, version, limits, notes, document_ref)
    operations = reader.read()
    paths = document.get("paths")
    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    provenance = DocumentProvenance(
        document_ref=document_ref,
        source_uri=fetched.source_uri,
        sha256=digest,
        byte_size=len(content),
        openapi_version=version,
        info_title=_text(info.get("title")),
        info_version=_info_version(info.get("version"), notes),
        revision_label=fetched.revision_label,
        visibility=fetched.visibility,
        path_count=sum(1 for key in paths if not key.startswith("x-")) if isinstance(paths, dict) else 0,
        operation_count=len(operations),
        component_schema_count=len(schemas) if isinstance(schemas, dict) else 0,
        server_urls=_server_urls(document.get("servers")),
    )
    warnings, patterns, warnings_omitted, patterns_omitted = notes.finish()
    return ParsedDocument(
        provenance=provenance,
        operations=operations,
        warnings=warnings,
        unsupported_patterns=patterns,
        unresolved_paths=reader.unresolved_paths,
        warnings_omitted=warnings_omitted,
        patterns_omitted=patterns_omitted,
    )


# Loading ---------------------------------------------------------------------------


def _load_document(content: bytes, media_type: str | None) -> dict:
    as_json = _is_json(content, media_type)
    if not as_json and len(content) > MAX_YAML_BYTES:
        raise OpenAPIDocumentError(
            f"YAML document is {len(content)} bytes, over the {MAX_YAML_BYTES}-byte limit"
            " for YAML (supply larger documents as JSON)"
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OpenAPIDocumentError("document is not valid UTF-8") from None
    if as_json:
        try:
            document = json.loads(text, object_pairs_hook=_unique_keys)
        except OpenAPIDocumentError:  # a ValueError too: keep its own message
            raise
        except json.JSONDecodeError as exc:
            raise OpenAPIDocumentError(
                f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
            ) from None
        except RecursionError:
            raise OpenAPIDocumentError(_too_deep()) from None
        except ValueError:  # e.g. an integer literal beyond Python's digit limit
            raise OpenAPIDocumentError("invalid JSON value") from None
        if isinstance(document, dict):
            _check_json_tree(document)
    else:
        document = _load_yaml(text)
        if isinstance(document, dict):
            document = _json_compatible(document)
    if not isinstance(document, dict):
        kind = "an empty document" if document is None else type(document).__name__
        raise OpenAPIDocumentError(f"document root must be a mapping, got {kind}")
    return document


def _is_json(content: bytes, media_type: str | None) -> bool:
    lowered = (media_type or "").lower()
    if "json" in lowered:
        return True
    if "yaml" in lowered or "yml" in lowered:
        return False
    start = _LEADING_SPACE.match(content).end()
    return content[start : start + 1] == b"{"


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` object hook: a repeated key would silently replace the earlier value."""
    mapping = dict(pairs)
    if len(mapping) != len(pairs):
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                raise OpenAPIDocumentError(f"invalid JSON: duplicate key {clip_text(key, 80)!r}")
            seen.add(key)
    return mapping


class _YAMLBoundsError(yaml.YAMLError):
    """Raised by the bounded loader with a short, content-free reason."""


class _BoundedSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses oversized documents and alias bombs before any Python
    objects are built, and mappings that repeat a key.

    PyYAML shares aliased nodes, so a bomb is cheap to compose but explodes when the
    result is walked as a tree; both the number of aliases and the extra nodes they
    add once expanded are therefore capped at composition time, as is the node count.
    Duplicate keys are checked as each mapping is composed, before ``<<`` merges are
    flattened, so a merge that an explicit key overrides is still allowed.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._alias_events = 0
        self._node_events = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        self._node_events += 1
        if self._node_events > MAX_YAML_NODES:
            raise _YAMLBoundsError(f"the document has more than {MAX_YAML_NODES} nodes")
        if self.check_event(yaml.AliasEvent):
            self._alias_events += 1
            if self._alias_events > MAX_YAML_ALIASES:
                raise _YAMLBoundsError(f"the document uses more than {MAX_YAML_ALIASES} aliases")
        return super().compose_node(parent, index)

    def compose_mapping_node(self, anchor: Any) -> Any:
        node = super().compose_mapping_node(anchor)
        seen: set[str] = set()
        for key_node, _value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag in _SPECIAL_YAML_KEY_TAGS:
                continue  # complex keys are refused later; merges may be overridden
            name = _key_name(self.construct_object(key_node))
            if name is None:
                continue
            if name in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {clip_text(name, 80)!r}",
                    key_node.start_mark,
                )
            seen.add(name)
        return node

    def get_single_data(self) -> Any:
        node = self.get_single_node()
        if node is None:
            return None
        if self._alias_events:
            _check_alias_expansion(node)
        return self.construct_document(node)


# JSON has no date type: keep YAML timestamps as the strings they were written as.
_BoundedSafeLoader.add_constructor(
    "tag:yaml.org,2002:timestamp", yaml.SafeLoader.construct_yaml_str
)


def _load_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_BoundedSafeLoader)  # bounded SafeLoader subclass
    except _YAMLBoundsError as exc:
        raise OpenAPIDocumentError(f"rejected YAML: {exc}") from None
    except yaml.MarkedYAMLError as exc:
        raise OpenAPIDocumentError(_yaml_problem(exc)) from None
    except yaml.YAMLError:
        raise OpenAPIDocumentError("invalid YAML") from None
    except RecursionError:
        raise OpenAPIDocumentError(_too_deep()) from None
    except ValueError:  # e.g. an integer scalar beyond Python's digit limit
        raise OpenAPIDocumentError("invalid YAML scalar value") from None


def _yaml_problem(exc: yaml.MarkedYAMLError) -> str:
    problem = clip_text(str(exc.problem or exc.context or "syntax error"), 120)
    mark = exc.problem_mark or exc.context_mark
    where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
    return f"invalid YAML: {problem}{where}"


def _yaml_children(node: yaml.Node) -> Iterator[yaml.Node]:
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            yield key_node
            yield value_node
    elif isinstance(node, yaml.SequenceNode):
        yield from node.value


def _check_alias_expansion(root: yaml.Node) -> None:
    """Refuse recursive aliases and aliases that expand the tree too much."""
    sizes: dict[int, int] = {}
    active: set[int] = set()  # nodes on the current depth-first path
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, finished = stack.pop()
        node_id = id(node)
        if finished:
            active.discard(node_id)
            sizes[node_id] = 1 + sum(sizes[id(child)] for child in _yaml_children(node))
            continue
        if node_id in sizes:
            continue
        active.add(node_id)
        stack.append((node, True))
        for child in _yaml_children(node):
            if id(child) in active:
                raise _YAMLBoundsError("the document contains a recursive alias")
            if id(child) not in sizes:
                stack.append((child, False))
    if sizes[id(root)] - len(sizes) > MAX_YAML_ALIAS_EXPANSION:
        raise _YAMLBoundsError("aliases expand the document beyond the allowed size")


def _json_compatible(data: dict) -> dict:
    """Copy YAML-loaded data into a JSON-shaped tree: string keys, no shared nodes."""
    holder: list[Any] = [None]
    stack: list[tuple[Any, Any, Any, int]] = [(data, holder, 0, 1)]
    while stack:
        node, parent, key, depth = stack.pop()
        if isinstance(node, dict):
            if depth > MAX_DOCUMENT_DEPTH:
                raise OpenAPIDocumentError(_too_deep())
            mapping: dict[str, Any] = {}
            parent[key] = mapping
            for raw_key, value in node.items():
                name = _json_key(raw_key)
                if isinstance(value, (dict, list, tuple)):
                    mapping[name] = None
                    stack.append((value, mapping, name, depth + 1))
                else:
                    mapping[name] = _json_scalar(value)
        elif isinstance(node, (list, tuple)):
            if depth > MAX_DOCUMENT_DEPTH:
                raise OpenAPIDocumentError(_too_deep())
            items: list[Any] = [None] * len(node)
            parent[key] = items
            for index, value in enumerate(node):
                if isinstance(value, (dict, list, tuple)):
                    stack.append((value, items, index, depth + 1))
                else:
                    items[index] = _json_scalar(value)
        else:
            parent[key] = _json_scalar(node)
    return holder[0]


def _key_name(key: Any) -> str | None:
    """A YAML mapping key the way ``json.dumps`` writes it (e.g. ``200:``), or None."""
    if isinstance(key, str):
        return key
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return "null"
    if isinstance(key, (int, float)):
        return str(key)
    return None


def _json_key(key: Any) -> str:
    name = _key_name(key)
    if name is None:
        raise OpenAPIDocumentError(f"unsupported YAML mapping key of type {type(key).__name__}")
    return name


def _json_scalar(value: Any) -> Any:
    if type(value) is float and not math.isfinite(value):  # .nan, .inf, 1e400
        raise OpenAPIDocumentError(_NON_FINITE)
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise OpenAPIDocumentError(f"unsupported YAML value of type {type(value).__name__}")


def _check_json_tree(document: dict) -> None:
    """Refuse nesting past MAX_DOCUMENT_DEPTH and numbers JSON cannot carry (NaN, 1e400)."""
    stack: list[tuple[Any, int]] = [(document, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DOCUMENT_DEPTH:
            raise OpenAPIDocumentError(_too_deep())
        for child in node.values() if type(node) is dict else node:
            if type(child) is dict or type(child) is list:
                stack.append((child, depth + 1))
            elif type(child) is float and not math.isfinite(child):
                raise OpenAPIDocumentError(_NON_FINITE)


def _too_deep() -> str:
    return f"document nesting exceeds {MAX_DOCUMENT_DEPTH} levels"


def _openapi_version(document: dict) -> str:
    version = document.get("openapi")
    if isinstance(version, str) and _is_supported_version(version):
        return version
    if "swagger" in document:
        raise OpenAPIDocumentError(
            "unsupported Swagger/OpenAPI 2.0 document; only OpenAPI 3.0 and 3.1 are supported"
        )
    if version is None:
        raise OpenAPIDocumentError("unsupported document: no 'openapi' version field")
    if not isinstance(version, str):
        raise OpenAPIDocumentError(
            "unsupported 'openapi' field: expected a version string such as '3.1.0'"
        )
    raise OpenAPIDocumentError(
        f"unsupported OpenAPI version {clip_text(version, 40)!r}; only 3.0.x and 3.1.x are supported"
    )


def _is_supported_version(version: str) -> bool:
    return version[:3] in ("3.0", "3.1") and (len(version) == 3 or version[3] in ".-+")


# Warnings ----------------------------------------------------------------------------


class _Notes:
    """Deterministic, bounded warnings and unsupported patterns for one parse.

    Past the caps, entries are only counted. Whole-document summaries (a fixed handful)
    are never capped and come first, so per-item warnings cannot crowd them out.
    Coverage-gap patterns (unresolved path items) are never capped either: they are
    bounded by the document's own paths, and a consumer must see every one to avoid
    treating unknown operations as absent.
    """

    def __init__(self) -> None:
        self._summaries: list[str] = []
        self._warnings: list[str] = []
        self._seen: set[str] = set()
        self._omitted = 0
        self._patterns: set[str] = set()
        self._gaps: set[str] = set()
        self._patterns_omitted = 0
        self._reported: set[tuple[str, str]] = set()

    def warn(self, message: str) -> None:
        if message in self._seen:
            return
        if len(self._seen) < _MAX_DEDUPE_KEYS:
            self._seen.add(message)
        if len(self._warnings) < MAX_WARNINGS:
            self._warnings.append(message)
        else:
            self._omitted += 1

    def summary(self, message: str) -> None:
        self._summaries.append(message)

    def pattern(self, value: str) -> None:
        if value in self._patterns:
            return
        if len(self._patterns) < MAX_UNSUPPORTED_PATTERNS:
            self._patterns.add(value)
        else:
            self._patterns_omitted += 1

    def gap(self, value: str) -> None:
        self._gaps.add(value)

    def remote_ref(self, ref: str, where: str) -> None:
        # A remote ref may embed credentials (https://user:token@host/x.yaml?key=...): show it
        # like a document URI, keeping the fragment, which is a JSON pointer, not a secret.
        base, hash_mark, fragment = ref.partition("#")
        shown = clip_text(display_uri(base) + hash_mark + fragment)
        if self._first(("remote", shown)):
            self.pattern(f"openapi:remote-ref {shown}")
            self.warn(
                f"openapi: remote $ref {shown!r} (first at {clip_text(where)}) was not followed;"
                " that part of the contract is unknown"
            )

    def missing_ref(self, ref: str, where: str) -> None:
        shown = clip_text(ref)
        if self._first(("missing", shown)):
            self.warn(f"openapi: $ref {shown!r} (first at {clip_text(where)}) does not resolve")

    def finish(self) -> tuple[list[str], list[str], int, int]:
        """(warnings, patterns, warnings past the cap, patterns past the cap)."""
        warnings = [*self._summaries, *self._warnings]
        return warnings, sorted(self._patterns | self._gaps), self._omitted, self._patterns_omitted

    def _first(self, key: tuple[str, str]) -> bool:
        if key in self._reported:
            return False
        if len(self._reported) < _MAX_DEDUPE_KEYS:
            self._reported.add(key)
        return True


# Operations -----------------------------------------------------------------------------


class _Body(NamedTuple):
    schema: Any  # document node to expand, or None when only a marker is known
    content_type: str | None
    required: bool
    marker: dict | None  # preserved $ref when the request body itself cannot be resolved
    location: str  # JSON pointer of ``schema``, written like ``json_pointer``


class _OperationReader:
    """Reads operations from one parsed document; owns $ref resolution state."""

    def __init__(
        self,
        document: dict,
        version: str,
        limits: ExpansionLimits,
        notes: _Notes,
        document_ref: str,
    ) -> None:
        self._doc = document
        self._is_31 = version.startswith("3.1")
        self._limits = limits
        self._notes = notes
        self._document_ref = document_ref
        self._targets: dict[str, tuple[str, Any]] = {}  # ref -> (pointer, target)
        self._measures: dict[str, tuple[int, int]] = {}  # pointer -> (nodes, levels) as written
        self._remaining = 0  # node budget left for the schema being expanded
        self._document_remaining = limits.max_document_nodes  # left for the whole document
        self._dropped_reserved = False
        self._truncated = {"depth_limit": 0, "output_depth": 0, "node_budget": 0, "document_budget": 0}
        self._fingerprints = _Fingerprints(document, self._lookup)
        # Documented paths (server base joined) whose path item could not be resolved.
        self.unresolved_paths: list[str] = []

    def read(self) -> list[DocumentedOperation]:
        operations: list[DocumentedOperation] = []
        unresolved: dict[str, None] = {}
        paths = self._doc.get("paths")
        # Reading no operations from a Paths Object that is there would make every documented
        # endpoint look removed, so a document whose paths are unknown is refused instead.
        if paths is not None and not isinstance(paths, dict):
            raise OpenAPIDocumentError("'paths' is not a mapping")
        if paths is not None and "$ref" in paths:
            raise OpenAPIDocumentError("'paths' is a $ref, which the Paths Object does not allow")
        root_base = self._base_path(self._doc.get("servers"), "/servers")
        for documented_path, raw_item in (paths or {}).items():
            if documented_path.startswith("x-"):
                continue  # a Paths Object extension, not a path
            item_pointer = "/paths/" + escape_pointer_token(documented_path)
            if not documented_path.startswith("/"):
                self._notes.warn(f"openapi: path key at {clip_text(item_pointer)} does not start with '/'")
            item = self._path_item(raw_item, item_pointer)
            if item is None:  # the path is documented but its operations are unknown
                base = root_base
                if isinstance(raw_item, dict):
                    base = self._override_base(raw_item, item_pointer, root_base)
                unresolved.setdefault(normalize_path(f"{base}/{documented_path}"))
                continue
            item_base = self._override_base(item, item_pointer, root_base)
            for method in HTTP_METHODS_LOWER:
                if method not in item:
                    continue
                pointer = operation_pointer(documented_path, method)
                operation = item[method]
                if not isinstance(operation, dict):
                    self._notes.warn(f"openapi: {clip_text(pointer)} is not an operation object; skipped")
                    continue
                if len(operations) >= self._limits.max_operations:
                    raise OpenAPIDocumentError(
                        f"document has more than {self._limits.max_operations} operations;"
                        " reading only some would make the rest look removed"
                    )
                base = self._override_base(operation, pointer, item_base)
                operations.append(
                    DocumentedOperation(
                        method=method.upper(),
                        path=normalize_path(f"{base}/{documented_path}"),
                        documented_path=documented_path,
                        operation_id=_text(operation.get("operationId")),
                        summary=_text(operation.get("summary")),
                        deprecated=operation.get("deprecated") is True,
                        request_schema=self._request_schema(item, operation, item_pointer, pointer),
                        response_schema=self._response_schema(operation, pointer),
                        json_pointer=pointer,
                        document_ref=self._document_ref,
                    )
                )
        self.unresolved_paths = list(unresolved)
        for path in self.unresolved_paths:  # recorded exactly; only warnings are clipped
            self._notes.gap(f"{PATTERN_UNRESOLVED_PATH_ITEM} {path}")
        self._final_notes()
        return operations

    def _final_notes(self) -> None:
        truncated = self._truncated
        if truncated["depth_limit"]:
            self._notes.summary(
                f"openapi: {truncated['depth_limit']} nested schema $ref(s) were kept unexpanded"
                f" at the {self._limits.max_ref_depth}-hop depth limit"
            )
        if truncated["output_depth"]:
            self._notes.summary(
                f"openapi: {truncated['output_depth']} schema $ref(s) were kept unexpanded at the"
                f" {MAX_OUTPUT_DEPTH}-level output depth limit"
            )
        if truncated["node_budget"]:
            self._notes.summary(
                f"openapi: {truncated['node_budget']} schema $ref(s) were kept unexpanded at the"
                f" {self._limits.max_nodes}-node budget"
            )
        if truncated["document_budget"]:
            self._notes.summary(
                f"openapi: {truncated['document_budget']} schema(s) were kept unexpanded once the"
                f" document-wide {self._limits.max_document_nodes}-node expansion budget was spent"
            )
        if self.unresolved_paths:
            self._notes.summary(
                f"openapi: {len(self.unresolved_paths)} documented path(s) have a path item that"
                " could not be resolved; their operations are unknown, not absent"
            )
        if self._fingerprints.exhausted:
            self._notes.summary(
                f"openapi: some kept $ref(s) carry no change fingerprint ({REF_DIGEST_KEY}):"
                f" the {_MAX_FINGERPRINT_NODES}-node fingerprint budget was spent"
            )
        if self._dropped_reserved:
            self._notes.summary(
                f"openapi: document keys in the reserved '{_RESERVED_PREFIX}' namespace"
                " were dropped from schemas"
            )
        webhooks = self._doc.get("webhooks")
        if isinstance(webhooks, dict) and webhooks:
            self._notes.summary(
                f"openapi: {len(webhooks)} webhook(s) describe provider-initiated calls and"
                " are not part of the endpoint inventory"
            )

    # Servers ----------------------------------------------------------------------

    def _override_base(self, holder: dict, pointer: str, inherited: str) -> str:
        servers = holder.get("servers")
        if isinstance(servers, list) and servers:
            return self._base_path(servers, pointer + "/servers")
        return inherited

    def _base_path(self, servers: Any, where: str) -> str:
        if servers is None:
            return ""
        if not isinstance(servers, list):
            self._notes.warn(f"openapi: servers at {clip_text(where)} is not a list; ignored")
            return ""
        bases: list[str] = []
        for server in servers:
            url = server.get("url") if isinstance(server, dict) else None
            if not isinstance(url, str):
                self._notes.warn(f"openapi: a server entry at {clip_text(where)} has no url; ignored")
                continue
            bases.append(self._server_base_path(url, server.get("variables"), where))
        if not bases:
            return ""
        distinct = list(dict.fromkeys(bases))
        if len(distinct) > 1:
            shown = ", ".join(repr(clip_text(base or "/", 60)) for base in distinct[:5])
            self._notes.warn(
                f"openapi: servers at {clip_text(where)} declare different base paths ({shown});"
                f" using the first, {clip_text(bases[0] or '/', 60)!r}"
            )
        return bases[0]

    def _server_base_path(self, url: str, variables: Any, where: str) -> str:
        declared = variables if isinstance(variables, dict) else {}
        undefined: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            spec = declared.get(match.group(1))
            default = spec.get("default") if isinstance(spec, dict) else None
            if isinstance(default, (str, int, float)) and not isinstance(default, bool):
                return str(default)
            undefined.append(match.group(1))
            return ""

        resolved = _SERVER_VARIABLE.sub(substitute, url)
        if undefined:
            names = ", ".join(sorted(set(undefined)))
            self._notes.warn(
                f"openapi: server url {clip_text(url)!r} at {clip_text(where)} has no default for"
                f" {clip_text(names, 80)}; treated as empty"
            )
        try:
            # An empty scheme ("{scheme}://host/v1") still names a host, not a path.
            parts = urlsplit("http" + resolved if resolved.startswith("://") else resolved)
        except ValueError:
            self._notes.warn(f"openapi: server url {clip_text(url)!r} is not a valid URL; base path ignored")
            return ""
        if not parts.scheme and not parts.netloc and parts.path and not parts.path.startswith("/"):
            self._notes.warn(
                f"openapi: server url {clip_text(url)!r} at {clip_text(where)} is relative to the"
                " document's location, which is not known; its path was resolved against '/'"
            )
        return _remove_dot_segments(parts.path)

    # Structural (non-schema) references ---------------------------------------------

    def _lookup(self, ref: str) -> tuple[str, Any]:
        found = self._targets.get(ref)
        if found is None:
            pointer = unquote(ref[1:])  # URI fragment -> JSON pointer (RFC 6901 section 6)
            found = self._targets[ref] = (pointer, _resolve_pointer(self._doc, pointer))
        return found

    def _resolve_component(self, node: Any, where: str) -> tuple[dict | None, str | None, str | None]:
        """Follow local ``$ref`` chains of a path item/parameter/body/response.

        Returns ``(object, None, None)``, or ``(None, failed_ref, status)`` when a
        reference cannot be followed, or ``(None, None, None)`` for a non-object.
        """
        seen: list[str] = []
        current = node
        while isinstance(current, dict) and isinstance(current.get("$ref"), str):
            ref = current["$ref"]
            if not ref.startswith("#"):
                self._notes.remote_ref(ref, where)
                return None, ref, "remote_unsupported"
            pointer, target = self._lookup(ref)
            if target is _MISSING:
                self._notes.missing_ref(ref, where)
                return None, ref, "missing_target"
            if pointer in seen:
                self._notes.warn(f"openapi: circular $ref chain at {clip_text(where)}")
                return None, ref, "cycle"
            if len(seen) >= _MAX_COMPONENT_CHAIN:
                self._notes.warn(f"openapi: $ref chain at {clip_text(where)} is too long")
                return None, ref, "depth_limit"
            seen.append(pointer)
            current = target
        if not isinstance(current, dict):
            self._notes.warn(f"openapi: expected an object at {clip_text(where)}; ignored")
            return None, None, None
        return current, None, None

    def _path_item(self, raw: Any, where: str) -> dict | None:
        if not isinstance(raw, dict):
            self._notes.warn(f"openapi: {clip_text(where)} is not a path item object; skipped")
            return None
        if not isinstance(raw.get("$ref"), str):
            return raw
        target, _failed, _status = self._resolve_component(raw, where)
        if target is None:
            return None
        merged = dict(target)  # local fields override the referenced path item
        merged.update((key, value) for key, value in raw.items() if key != "$ref")
        return merged

    # Request ----------------------------------------------------------------------------

    def _request_schema(self, item: dict, operation: dict, item_pointer: str, pointer: str) -> dict:
        parameters = self._parameters(item.get("parameters"), item_pointer + "/parameters")
        # Operation-level parameters override path-level ones with the same (in, name).
        parameters.update(self._parameters(operation.get("parameters"), pointer + "/parameters"))
        properties: dict[str, Any] = {}
        required: list[str] = []
        held: list[tuple[tuple[str, ...], dict]] = []  # markers set in place after expansion
        for location in _PARAMETER_LOCATIONS:
            entries = [
                (name, schema, needed, at)
                for (where, name), (schema, needed, at) in parameters.items()
                if where == location
            ]
            if not entries:
                continue
            group_properties: dict[str, Any] = {}
            for name, schema, _needed, at in entries:
                marker = self._over_budget(schema, at)
                group_properties[name] = schema if marker is None else None
                if marker is not None:
                    held.append(((location, "properties", name), marker))
            group: dict[str, Any] = {"type": "object", "properties": group_properties}
            names = sorted(name for name, _, needed, _ in entries if needed)
            if names:
                group["required"] = names
                required.append(location)
            properties[location] = group
        body = None
        if "requestBody" in operation:
            body = self._request_body(operation["requestBody"], pointer + "/requestBody")
        if body is not None and body.marker is None:
            marker = self._over_budget(body.schema, body.location)
            properties["body"] = body.schema if marker is None else None
            if marker is not None:
                held.append((("body",), marker))
            if body.required:
                required.append("body")
        skeleton: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            skeleton["required"] = sorted(required)
        schema = _schema_root(self._expand(skeleton, pointer))
        for keys, marker in held:
            parent = schema["properties"]
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = marker
        if body is not None:
            if body.marker is not None:
                schema["properties"]["body"] = body.marker
            if body.content_type is not None:
                schema[CONTENT_TYPE_KEY] = body.content_type
        return schema

    def _parameters(self, raw: Any, where: str) -> dict[tuple[str, str], tuple[Any, bool, str]]:
        """(in, name) -> (schema, required, JSON pointer of the schema)."""
        found: dict[tuple[str, str], tuple[Any, bool, str]] = {}
        if raw is None:
            return found
        if not isinstance(raw, list):
            self._notes.warn(f"openapi: parameters at {clip_text(where)} is not a list; ignored")
            return found
        for index, entry in enumerate(raw):
            parameter, _failed, _status = self._resolve_component(entry, f"{where}/{index}")
            if parameter is None:
                continue
            name, location = parameter.get("name"), parameter.get("in")
            if not isinstance(name, str) or location not in _PARAMETER_LOCATIONS:
                self._notes.warn(
                    f"openapi: parameter at {clip_text(where)}/{index} has no usable name/in; ignored"
                )
                continue
            if location == "header" and name.lower() in _IGNORED_HEADER_PARAMETERS:
                continue
            required = location == "path" or parameter.get("required") is True
            schema, suffix = _parameter_schema(parameter)
            found[(location, name)] = (schema, required, f"{where}/{index}{suffix}")
        return found

    def _request_body(self, raw: Any, where: str) -> _Body | None:
        body, failed, status = self._resolve_component(raw, where)
        if failed is not None:
            marker = self._stamped({"$ref": failed, REF_STATUS_KEY: status}, failed)
            return _Body(None, None, False, marker, where)
        if body is None:
            return None
        content = body.get("content")
        media_type = _preferred_media_type(content, request=True)
        if media_type is None:
            self._notes.warn(f"openapi: request body at {clip_text(where)} declares no content")
            return None
        location = f"{where}/content/{escape_pointer_token(media_type)}/schema"
        required = body.get("required") is True
        return _Body(content[media_type].get("schema", {}), media_type, required, None, location)

    # Response ---------------------------------------------------------------------------

    def _response_schema(self, operation: dict, pointer: str) -> dict:
        responses = operation.get("responses")
        if not isinstance(responses, dict):
            return {}
        numeric = sorted((code for code in responses if _SUCCESS_STATUS.fullmatch(code)), key=int)
        success = numeric + [code for code in responses if code.upper() == "2XX"]
        # "default" describes success only when no 2xx response is declared.
        candidates = success or [code for code in responses if code == "default"]
        for status in candidates:
            where = f"{pointer}/responses/{escape_pointer_token(status)}"
            schema = self._response_candidate(responses[status], status, where)
            if schema is not None:
                return schema
        return {STATUS_KEY: candidates[0]} if candidates else {}

    def _response_candidate(self, raw: Any, status: str, where: str) -> dict | None:
        response, failed, why = self._resolve_component(raw, where)
        if failed is not None:  # the response itself is unknown: keep the reference
            return self._stamped({"$ref": failed, REF_STATUS_KEY: why, STATUS_KEY: status}, failed)
        if response is None:
            return None
        content = response.get("content")
        media_type = _preferred_media_type(content, request=False)
        if media_type is None:
            return None
        media = content[media_type]
        schema: dict = {}
        if "schema" in media:
            location = f"{where}/content/{escape_pointer_token(media_type)}/schema"
            marker = self._over_budget(media["schema"], location)
            schema = marker if marker is not None else _schema_root(self._expand(media["schema"], where))
        schema[STATUS_KEY] = status
        schema[CONTENT_TYPE_KEY] = media_type
        return schema

    # Schema expansion -------------------------------------------------------------------

    def _over_budget(self, schema: Any, location: str) -> dict | None:
        """A ``node_budget`` reference for a schema the document budget can no longer hold.

        ``location`` is the schema's JSON pointer, written like ``json_pointer`` (through
        structural references); the fingerprint is taken from the schema itself.
        """
        if type(schema) is not dict and type(schema) is not list:
            return None
        if (
            type(schema) is dict
            and type(schema.get("$ref")) is str
            and not (self._is_31 and _constrains_beside_ref(schema))
        ):
            return None  # a plain reference: ``_admit`` keeps it naming its own target
        if _node_count(schema) <= self._document_remaining:
            return None
        self._truncated["document_budget"] += 1
        marker = {"$ref": "#" + location.replace("%", "%25"), REF_STATUS_KEY: "node_budget"}
        digest = self._fingerprints.of_node(schema)
        if digest is not None:
            marker[REF_DIGEST_KEY] = digest
        return marker

    def _stamped(self, node: dict, ref: str) -> dict:
        """``node`` with the fingerprint of a resolvable local ``ref``'s target, if known."""
        if ref.startswith("#"):
            pointer, target = self._lookup(ref)
            if target is not _MISSING:
                digest = self._fingerprints.of_pointer(pointer)
                if digest is not None:
                    node[REF_DIGEST_KEY] = digest
        return node

    def _expand(self, root: Any, where: str) -> Any:
        """Fresh copy of ``root`` with local schema ``$ref`` targets inlined within limits."""
        own = _node_count(root)
        self._remaining = self._limits.max_nodes - own
        self._document_remaining -= own
        holder: list[Any] = [None]
        queue: deque[tuple[Any, Any, Any, int, tuple[str, ...], int, int]] = deque()
        queue.append((root, holder, 0, _SCHEMA, (), 0, 0))
        while queue:
            value, parent, key, mode, refs, hops, depth = queue.popleft()
            origin: str | None = None
            overlays: tuple = ()
            if mode == _SCHEMA and type(value) is dict and type(value.get("$ref")) is str:
                if self._is_31 and _constrains_beside_ref(value):
                    value = _ref_as_all_of(value)  # the siblings apply as well: keep both
                else:
                    status, ref, value, refs, hops, origin, overlays = self._follow(
                        value, refs, hops, depth, where
                    )
                    if status is not None:
                        parent[key] = self._marker(ref, status, origin, overlays, depth, queue)
                        continue
            if type(value) is dict:
                copy: dict[str, Any] = {}
                parent[key] = copy
                if overlays:
                    self._fill_overlaid(copy, value, refs, hops, depth, overlays, queue)
                else:
                    self._fill(copy, value, mode, refs, hops, depth, queue)
                if origin is not None:
                    copy[ORIGIN_KEY] = origin
                    digest = self._fingerprints.of_pointer(origin)
                    if digest is not None:
                        copy[REF_DIGEST_KEY] = digest
            elif type(value) is list:
                items: list[Any] = [None] * len(value)
                parent[key] = items
                child_mode = _LITERAL if mode == _LITERAL else _SCHEMA
                for index, item in enumerate(value):
                    if type(item) is dict or type(item) is list:
                        queue.append((item, items, index, child_mode, refs, hops, depth + 1))
                    else:
                        items[index] = item
            else:
                parent[key] = value
        return holder[0]

    def _fill(
        self,
        copy: dict,
        source: dict,
        mode: int,
        refs: tuple[str, ...],
        hops: int,
        depth: int,
        queue: deque,
    ) -> None:
        append = queue.append
        if mode == _SCHEMA:
            for name, child in source.items():
                if name.startswith(_RESERVED_PREFIX):
                    self._dropped_reserved = True
                    continue
                if type(child) is dict or type(child) is list:
                    copy[name] = None  # placeholder keeps the document's key order
                    append((child, copy, name, _keyword_mode(name), refs, hops, depth + 1))
                else:
                    copy[name] = child
            return
        child_mode = _LITERAL if mode == _LITERAL else _SCHEMA
        for name, child in source.items():
            if type(child) is dict or type(child) is list:
                copy[name] = None
                append((child, copy, name, child_mode, refs, hops, depth + 1))
            else:
                copy[name] = child

    def _fill_overlaid(
        self,
        copy: dict,
        target: dict,
        refs: tuple[str, ...],
        hops: int,
        depth: int,
        overlays: tuple,
        queue: deque,
    ) -> None:
        """OpenAPI 3.1: annotation keywords next to ``$ref`` override the referenced schema's."""
        entries: dict[str, tuple[Any, tuple[str, ...], int]] = {
            name: (child, refs, hops) for name, child in target.items()
        }
        for siblings, sibling_refs, sibling_hops in reversed(overlays):  # outermost wins
            for name, child in siblings.items():
                entries[name] = (child, sibling_refs, sibling_hops)
        for name, (child, child_refs, child_hops) in entries.items():
            if name.startswith(_RESERVED_PREFIX):
                self._dropped_reserved = True
                continue
            if type(child) is dict or type(child) is list:
                copy[name] = None
                queue.append((child, copy, name, _keyword_mode(name), child_refs, child_hops, depth + 1))
            else:
                copy[name] = child

    def _marker(
        self,
        ref: str,
        status: str,
        origin: str | None,
        overlays: tuple,
        depth: int,
        queue: deque,
    ) -> dict:
        marker: dict[str, Any] = {"$ref": ref}
        if overlays:
            self._fill_overlaid(marker, {}, (), 0, depth, overlays, queue)
        marker[REF_STATUS_KEY] = status
        self._stamped(marker, ref)
        if origin is not None:  # the reference was reached through an inlined alias
            marker[ORIGIN_KEY] = origin
        return marker

    def _follow(
        self,
        node: dict,
        refs: tuple[str, ...],
        hops: int,
        depth: int,
        where: str,
    ) -> tuple[str | None, str, Any, tuple[str, ...], int, str | None, tuple]:
        """Resolve a schema ``$ref`` (and any ref-to-ref chain) or say why not.

        Returns ``(status, ref, target, refs, hops, origin, overlays)``; ``status`` is
        None when ``target`` should be inlined. In 3.1 a target that is itself a
        ``$ref`` with constraining siblings ends the chain and is inlined rewritten.
        """
        origin: str | None = None
        overlays: tuple = ()
        current = node
        while True:
            ref = current["$ref"]
            if self._is_31 and len(current) > 1:
                siblings = {name: child for name, child in current.items() if name != "$ref"}
                overlays = (*overlays, (siblings, refs, hops))
            status, pointer, target = self._admit(ref, refs, hops, depth, where)
            if status is not None:
                return status, ref, None, refs, hops, origin, overlays
            refs = (*refs, pointer)
            hops += 1
            origin = pointer
            if type(target) is dict and type(target.get("$ref")) is str:
                if self._is_31 and _constrains_beside_ref(target):
                    return None, ref, _ref_as_all_of(target), refs, hops, origin, overlays
                current = target
                continue
            return None, ref, target, refs, hops, origin, overlays

    def _admit(
        self,
        ref: str,
        refs: tuple[str, ...],
        hops: int,
        depth: int,
        where: str,
    ) -> tuple[str | None, str | None, Any]:
        if not ref.startswith("#"):
            self._notes.remote_ref(ref, where)
            return "remote_unsupported", None, None
        pointer, target = self._lookup(ref)
        if target is _MISSING:
            self._notes.missing_ref(ref, where)
            return "missing_target", None, None
        if pointer in refs:
            return "cycle", None, None
        if hops >= self._limits.max_ref_depth:
            self._truncated["depth_limit"] += 1
            return "depth_limit", None, None
        measured = self._measures.get(pointer)
        if measured is None:
            measured = self._measures[pointer] = _measure(target)
        size, levels = measured
        # The target's root takes the reference's place at ``depth`` (0 for a schema root).
        if depth + levels + _REWRITE_LEVELS > MAX_OUTPUT_DEPTH:
            self._truncated["output_depth"] += 1
            return "depth_limit", None, None
        if size > self._remaining:
            self._truncated["node_budget"] += 1
            return "node_budget", None, None
        if size > self._document_remaining:
            self._truncated["document_budget"] += 1
            return "node_budget", None, None
        self._remaining -= size
        self._document_remaining -= size
        return None, pointer, target


# Fingerprints ---------------------------------------------------------------------------


class _FingerprintBudgetSpent(Exception):
    """Internal: the fingerprint walk budget for one document is used up."""


class _Fingerprints:
    """``REF_DIGEST_KEY`` values for one document, memoized per pointer.

    A fingerprint covers a ``$ref`` target and every local target it reaches through
    further ``$ref``s, so an edit anywhere below a reference that was not expanded
    changes it, while edits to annotation-only keywords (descriptions, examples, ...)
    and extensions do not. Targets on a common cycle form one strongly connected group
    and share one fingerprint, which keeps the computation finite and linear.
    """

    def __init__(self, document: dict, lookup: Callable[[str], tuple[str, Any]]) -> None:
        self._document = document
        self._lookup = lookup  # memoized ref -> (pointer, target) of the reader
        self._scans: dict[str, tuple[str, tuple[str, ...]]] = {}  # pointer -> (own hash, targets)
        self._digests: dict[str, str] = {}
        self._nodes: dict[int, tuple[Any, str | None]] = {}  # id(root) -> (root, fingerprint)
        self._budget = _MAX_FINGERPRINT_NODES
        self.exhausted = False

    def of_pointer(self, pointer: str) -> str | None:
        found = self._digests.get(pointer)
        if found is not None or self.exhausted:
            return found
        try:
            self._close(pointer)
        except _FingerprintBudgetSpent:
            self.exhausted = True
            return None
        return self._digests[pointer]

    def of_node(self, node: Any) -> str | None:
        """Fingerprint of a schema that is not itself a ``$ref`` target (a schema root)."""
        key = id(node)
        if key in self._nodes:
            return self._nodes[key][1]
        digest = None
        if not self.exhausted:
            try:
                own, targets = self._scan(node)
            except _FingerprintBudgetSpent:
                self.exhausted = True
            else:
                reached = [self.of_pointer(target) for target in targets]
                if None not in reached:
                    digest = _short_digest(json.dumps([own, sorted(set(reached))]))
        self._nodes[key] = (node, digest)  # holding the node keeps its id from being reused
        return digest

    def _close(self, start: str) -> None:
        """Fingerprint every group of targets reachable from ``start`` (iterative Tarjan)."""
        index: dict[str, int] = {start: 0}
        low: dict[str, int] = {start: 0}
        stack: list[str] = [start]
        on_stack: set[str] = {start}
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            pointer, position = work[-1]
            targets = self._targets_of(pointer)
            if position < len(targets):
                work[-1] = (pointer, position + 1)
                target = targets[position]
                if target in self._digests:
                    continue
                if target not in index:
                    index[target] = low[target] = len(index)
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, 0))
                elif target in on_stack:
                    low[pointer] = min(low[pointer], index[target])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[pointer])
            if low[pointer] == index[pointer]:
                group: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == pointer:
                        break
                self._seal(group)

    def _seal(self, group: list[str]) -> None:
        members = set(group)
        own = sorted([member, self._scans[member][0]] for member in group)
        reached = sorted(
            {
                self._digests[target]
                for member in group
                for target in self._scans[member][1]
                if target not in members
            }
        )
        digest = _short_digest(json.dumps([own, reached]))
        for member in group:
            self._digests[member] = digest

    def _targets_of(self, pointer: str) -> tuple[str, ...]:
        scan = self._scans.get(pointer)
        if scan is None:
            target = _resolve_pointer(self._document, pointer)
            scan = ("missing", ()) if target is _MISSING else self._scan(target)
            self._scans[pointer] = scan
        return scan[1]

    def _scan(self, node: Any) -> tuple[str, tuple[str, ...]]:
        """(sha256 of the node's structural JSON, resolvable local targets it references)."""
        targets: dict[str, None] = {}
        holder: list[Any] = [None]
        stack: list[tuple[Any, Any, Any, int]] = [(node, holder, 0, _SCHEMA)]
        walked = 0
        while stack:
            value, parent, key, mode = stack.pop()
            if type(value) is dict:
                walked += 1 + len(value)
                if walked > self._budget:
                    raise _FingerprintBudgetSpent
                copy: dict[str, Any] = {}
                parent[key] = copy
                for name, child in value.items():
                    if mode == _SCHEMA:
                        if name in _ANNOTATION_KEYWORDS or name.startswith("x-"):
                            continue
                        if name == "$ref" and type(child) is str and child.startswith("#"):
                            pointer, target = self._lookup(child)
                            if target is not _MISSING:
                                targets.setdefault(pointer)
                        child_mode = _keyword_mode(name)
                    else:
                        child_mode = _LITERAL if mode == _LITERAL else _SCHEMA
                    if type(child) is dict or type(child) is list:
                        copy[name] = None
                        stack.append((child, copy, name, child_mode))
                    else:
                        copy[name] = child
            elif type(value) is list:
                walked += 1 + len(value)
                if walked > self._budget:
                    raise _FingerprintBudgetSpent
                items: list[Any] = [None] * len(value)
                parent[key] = items
                child_mode = _LITERAL if mode == _LITERAL else _SCHEMA
                for position, item in enumerate(value):
                    if type(item) is dict or type(item) is list:
                        stack.append((item, items, position, child_mode))
                    else:
                        items[position] = item
            else:
                walked += 1
                parent[key] = value
        self._budget -= walked
        text = json.dumps(holder[0], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest(), tuple(targets)


def _short_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:32]


# Helpers -------------------------------------------------------------------------------


def _keyword_mode(name: str) -> int:
    if name in _LITERAL_KEYWORDS or name.startswith("x-"):
        return _LITERAL
    if name in _NAME_MAP_KEYWORDS:
        return _NAMES
    return _SCHEMA


def _constrains_beside_ref(node: dict) -> bool:
    """3.1: does a ``$ref`` node carry a sibling that is more than an annotation?"""
    return any(
        name != "$ref" and name not in _OVERRIDE_KEYWORDS and not name.startswith("x-")
        for name in node
    )


def _ref_as_all_of(node: dict) -> dict:
    """``{"$ref": r, <siblings>}`` as ``{<siblings>, "allOf": [{"$ref": r}, ...]}``.

    In 3.1 a ``$ref`` applies in addition to its siblings, exactly like an ``allOf``
    member, so both keep applying and nothing is merged; a sibling ``allOf`` is
    flattened into the same list. The siblings stay where they were written.
    """
    rewritten = {name: child for name, child in node.items() if name not in ("$ref", "allOf")}
    members = node.get("allOf", [])
    rewritten["allOf"] = [{"$ref": node["$ref"]}, *(members if type(members) is list else [members])]
    return rewritten


def _node_count(node: Any) -> int:
    """Number of JSON values (containers and scalars) in ``node`` as written."""
    count = 0
    stack = [node]
    while stack:
        current = stack.pop()
        count += 1
        if type(current) is dict:
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)
    return count


def _measure(node: Any) -> tuple[int, int]:
    """(number of JSON values, container levels) of ``node`` as written; a scalar has 0 levels."""
    if type(node) is not dict and type(node) is not list:
        return 1, 0
    count = levels = 0
    stack: list[tuple[Any, int]] = [(node, 1)]
    while stack:
        current, level = stack.pop()
        count += 1
        levels = max(levels, level)
        for child in current.values() if type(current) is dict else current:
            if type(child) is dict or type(child) is list:
                stack.append((child, level + 1))
            else:
                count += 1
    return count, levels


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return _MISSING
    node = document
    for raw_token in pointer[1:].split("/"):
        token = unescape_pointer_token(raw_token)
        if type(node) is dict:
            if token not in node:
                return _MISSING
            node = node[token]
        elif type(node) is list:
            if not _ARRAY_INDEX.fullmatch(token) or int(token) >= len(node):
                return _MISSING
            node = node[int(token)]
        else:
            return _MISSING
    return node


def _remove_dot_segments(path: str) -> str:
    """A server URL path with "." and ".." resolved against "/" (RFC 3986 section 5.2.4);
    "" for the root. Empty segments and a trailing "/" are dropped as well."""
    kept: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if kept:
                kept.pop()
        elif segment not in ("", "."):
            kept.append(segment)
    return "/" + "/".join(kept) if kept else ""


def _parameter_schema(parameter: dict) -> tuple[Any, str]:
    """The parameter's schema and that schema's pointer below the parameter object."""
    if "schema" in parameter:
        return parameter["schema"], "/schema"
    content = parameter.get("content")
    if isinstance(content, dict):
        for media_type, media in content.items():
            if isinstance(media, dict) and "schema" in media:
                return media["schema"], f"/content/{escape_pointer_token(media_type)}/schema"
    return {}, ""


def _preferred_media_type(content: Any, *, request: bool) -> str | None:
    """application/json, then any +json, then (requests) form encodings, then first."""
    if not isinstance(content, dict):
        return None
    keys = [key for key, media in content.items() if isinstance(media, dict)]
    if not keys:
        return None
    bases = [key.split(";", 1)[0].strip().lower() for key in keys]
    for key, base in zip(keys, bases):
        if base == "application/json":
            return key
    for key, base in zip(keys, bases):
        if base.endswith("+json"):
            return key
    if request:
        for preferred in _FORM_MEDIA_TYPES:
            for key, base in zip(keys, bases):
                if base == preferred:
                    return key
    return keys[0]


def _schema_root(value: Any) -> dict:
    """Roots must be objects: map 3.1 boolean schemas to their object equivalents."""
    if isinstance(value, dict):
        return value
    if value is False:
        return {"not": {}}
    return {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _info_version(value: Any, notes: _Notes) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        notes.warn("openapi: info.version is not a string; recorded as parsed")
        return str(value)
    return None


def _server_urls(servers: Any) -> tuple[str, ...]:
    if not isinstance(servers, list):
        return ()
    return tuple(
        server["url"]
        for server in servers
        if isinstance(server, dict) and isinstance(server.get("url"), str)
    )


# Cache ---------------------------------------------------------------------------------


class _SnapshotCache:
    """Bounded LRU of parse results stored as marshal snapshots.

    Restoring a snapshot builds brand-new containers, so callers that mutate a result
    can never corrupt the cache or another caller's result. Provenance is immutable
    and shared as-is.
    """

    def __init__(self, max_entries: int, max_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple, tuple[DocumentProvenance, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> ParsedDocument | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
        provenance, blob = entry
        operations, warnings, patterns, unresolved, warnings_omitted, patterns_omitted = (
            marshal.loads(blob)
        )
        return ParsedDocument(
            provenance=provenance,
            operations=[
                DocumentedOperation(**dict(zip(_OPERATION_FIELDS, fields))) for fields in operations
            ],
            warnings=warnings,
            unsupported_patterns=patterns,
            unresolved_paths=unresolved,
            warnings_omitted=warnings_omitted,
            patterns_omitted=patterns_omitted,
        )

    def put(self, key: tuple, parsed: ParsedDocument) -> None:
        payload = (
            [tuple(getattr(op, name) for name in _OPERATION_FIELDS) for op in parsed.operations],
            parsed.warnings,
            parsed.unsupported_patterns,
            parsed.unresolved_paths,
            parsed.warnings_omitted,
            parsed.patterns_omitted,
        )
        try:
            blob = marshal.dumps(payload)
        except ValueError:  # not snapshot-able (e.g. nesting beyond marshal's limit): skip
            return
        if len(blob) > self._max_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous[1])
            self._entries[key] = (parsed.provenance, blob)
            self._bytes += len(blob)
            while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
                _, (_, evicted) = self._entries.popitem(last=False)
                self._bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


_CACHE = _SnapshotCache(_CACHE_ENTRIES, _CACHE_MAX_BYTES)
