"""Language-neutral value model, URL/endpoint resolution and finding emission.

Adapters translate syntax nodes into these values:

* ``Sym``      – a library symbol (an SDK module/client, an HTTP function).
* ``Const``    – a string constant (or template parts) usable in URL building.
* ``Handle``   – an HTTP response object before ``.json()``.
* ``OriginVal``– a provider response body (SDK result or parsed HTTP JSON).
* ``FieldVal`` – a field read from an origin (``response.amount``).

Every value carries the syntax-node locations that justify it, so evidence is
never invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Union

from .contracts import Endpoint

UNKNOWN = None  # placeholder for an unresolvable URL part


@dataclass(frozen=True)
class Loc:
    file: str
    line: int  # 1-based
    end_line: int
    col: int  # 0-based column, used only for stable ordering
    excerpt: str
    label: str = ""


@dataclass(frozen=True)
class Sym:
    name: str


@dataclass(frozen=True)
class Const:
    parts: tuple  # tuple[str | None, ...]


@dataclass(frozen=True)
class Handle:
    endpoint: Endpoint
    library: str
    call: str
    evidence: tuple


@dataclass(frozen=True)
class OriginVal:
    endpoint: Endpoint
    kind: str  # sdk_response | http_response
    library: str
    call: str
    evidence: tuple
    confidence: str = "direct"


@dataclass(frozen=True)
class FieldVal:
    origin: OriginVal
    path: tuple
    evidence: tuple
    confidence: str


Value = Union[Sym, Const, Handle, OriginVal, FieldVal, None]


@dataclass
class Finding:
    origin: OriginVal
    path: tuple  # () means the whole response object
    operation: str
    operand: Union[float, int, str, None]
    destination: Optional[str]
    confidence: str
    evidence: tuple  # tuple[Loc, ...], last element is the use site
    language: str


def weaker(a: str, b: str) -> str:
    order = {"direct": 0, "inferred": 1, "unresolved": 2}
    return a if order[a] >= order[b] else b


_PARAM = re.compile(r"^\{[^{}/]+\}$")


def _norm_template(path: str) -> list[str]:
    segs = [s for s in path.split("/") if s != ""]
    return ["{}" if _PARAM.match(s) else s for s in segs]


class Catalog:
    """Endpoint lookup by canonical identity (service_id, METHOD, path)."""

    def __init__(self, endpoints: list[Endpoint]):
        self.endpoints = sorted(
            endpoints, key=lambda e: (e.service_id, e.method.upper(), e.path, e.id)
        )

    def by_template(self, service_id: str, method: str, path: str) -> Optional[Endpoint]:
        want = _norm_template(path)
        for ep in self.endpoints:
            if ep.service_id == service_id and ep.method.upper() == method.upper():
                if _norm_template(ep.path) == want:
                    return ep
        return None

    def by_url_path(self, service_id: str, method: str, segments: list) -> list[Endpoint]:
        """segments: list of literal str, or UNKNOWN for an interpolated segment.

        A partially interpolated segment is passed as the marker ``"\\x00partial"``.
        """
        matches = []
        for ep in self.endpoints:
            if ep.service_id != service_id or ep.method.upper() != method.upper():
                continue
            tmpl = _norm_template(ep.path)
            if len(tmpl) != len(segments):
                continue
            ok = True
            for t, s in zip(tmpl, segments):
                if t == "{}":
                    continue
                if s is UNKNOWN or s == "\x00partial" or s != t:
                    ok = False
                    break
            if ok:
                matches.append(ep)
        return matches


@dataclass
class UrlResolution:
    status: str  # matched | foreign | dynamic | unmatched | ambiguous
    endpoint: Optional[Endpoint] = None
    detail: str = ""


def resolve_url(
    parts: tuple, method: str, service_id: str, base_urls: list[str], catalog: Catalog
) -> UrlResolution:
    """Resolve URL parts (literals + UNKNOWN placeholders) against a provider."""
    if not parts or parts[0] is UNKNOWN:
        return UrlResolution("dynamic", detail="URL host is not a literal")
    # Join, using \x00 for unknown parts.
    text = "".join("\x00" if p is UNKNOWN else p for p in parts)
    base = next((b.rstrip("/") for b in base_urls if text.startswith(b.rstrip("/") + "/") or text == b.rstrip("/")), None)
    if base is None:
        prefix = text.split("\x00", 1)[0]
        has_unknown = "\x00" in text
        if prefix.startswith("/") and not prefix.startswith("//"):
            return UrlResolution("foreign")  # relative, same-origin URL
        if "://" in prefix:
            host_part = prefix.split("://", 1)[1]
            if "/" in host_part or not has_unknown:
                return UrlResolution("foreign")  # literal host of another service
            return UrlResolution("dynamic", detail="URL host only partially literal")
        return UrlResolution("dynamic", detail="URL scheme/host not literal")
    rest = text[len(base):]
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    segments: list = []
    for seg in [s for s in rest.split("/") if s != ""]:
        if seg == "\x00":
            segments.append(UNKNOWN)
        elif "\x00" in seg:
            segments.append("\x00partial")
        else:
            segments.append(seg)
    found = catalog.by_url_path(service_id, method, segments)
    shown = "/" + "/".join("{?}" if s is UNKNOWN or s == "\x00partial" else s for s in segments)
    if len(found) == 1:
        return UrlResolution("matched", found[0])
    if not found:
        return UrlResolution("unmatched", detail=f"{method} {shown}")
    return UrlResolution("ambiguous", detail=f"{method} {shown}")


@dataclass
class FileContext:
    """Per-file sinks shared by adapters."""

    file: str
    language: str
    source: bytes
    service_id: str
    base_urls: list[str]
    catalog: Catalog
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def warn(self, code: str, line: int, detail: str = "") -> None:
        msg = f"{code}: {self.file}:{line}"
        if detail:
            msg += f" ({detail})"
        self.warnings.append(msg)

    def gap(self, code: str, line: int, detail: str = "") -> None:
        msg = f"{code}: {self.file}:{line}"
        if detail:
            msg += f" ({detail})"
        self.unsupported.append(msg)

    def loc(self, node, label: str = "") -> Loc:
        line = node.start_point.row + 1
        lines = self.source.splitlines()
        text = lines[line - 1].decode("utf-8", "replace").strip() if line - 1 < len(lines) else ""
        if len(text) > 200:
            text = text[:197] + "..."
        return Loc(self.file, line, node.end_point.row + 1, node.start_point.column, text, label)


def number_literal(text: str):
    t = text.replace("_", "")
    try:
        if re.fullmatch(r"[+-]?\d+", t):
            return int(t)
        return float(t)
    except ValueError:
        return None
