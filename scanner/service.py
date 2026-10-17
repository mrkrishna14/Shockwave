"""Public entry point: ``scan_repository(request, snapshot, repo_root) -> ScanResult``.

Local and read-only: no network, no package installation, no execution of
consumer code. Repository contents are treated purely as data.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from .adapters.python import PythonAdapter
from .adapters.typescript import TypeScriptAdapter
from .contracts import (
    SCHEMA_VERSION, Dependency, Evidence, RepositorySnapshot, ScanRequest, ScanResult,
)
from .mappings import KNOWN_BASE_URLS
from .origins import Catalog, FileContext, Finding, Loc
from .walker import walk_repository

# Fixed namespace so identifiers are stable across repeated scans of a commit.
SCANNER_NAMESPACE = uuid.UUID("6f1c7a52-6c3e-4b8e-9d0a-5d1f2b9e7c11")
SUPPORTED = {"python", "javascript", "typescript"}
ADAPTERS = {"python": PythonAdapter, "javascript": TypeScriptAdapter, "typescript": TypeScriptAdapter}


def _uuid(*parts) -> str:
    return str(uuid.uuid5(SCANNER_NAMESPACE, "|".join(str(p) for p in parts)))


def _evidence(loc: Loc, snapshot: RepositorySnapshot) -> Evidence:
    return Evidence(
        id=_uuid("evidence", snapshot.repository_connection_id, snapshot.commit_sha, loc.file, loc.line, loc.col, loc.label),
        kind="source",
        visibility="consumer_private",
        source_uri=f"repository-snapshot://{snapshot.id}/{loc.file}#L{loc.line}",
        revision=snapshot.commit_sha,
        file=loc.file,
        line_start=loc.line,
        line_end=loc.end_line,
        json_pointer=None,
        excerpt=loc.excerpt or None,
    )


def _field_path(path: tuple) -> str:
    return "response" + "".join(f".{p}" for p in path)


def scan_repository(request: ScanRequest, snapshot: RepositorySnapshot, repo_root: Path) -> ScanResult:
    if request.repository_snapshot_id != snapshot.id:
        raise ValueError("request.repository_snapshot_id does not match snapshot.id")
    consumer_org_id = getattr(request, "consumer_org_id", None)
    if not consumer_org_id:
        raise ValueError(
            "consumer_org_id is required to build Dependency records; "
            "pass it on ScanRequest (proposed additive field, see scanner/HANDOFF.md)"
        )
    base_urls = list(getattr(request, "base_urls", None) or KNOWN_BASE_URLS.get(request.service_id, []))
    wanted = {l.lower() for l in request.supported_languages} or set(SUPPORTED)
    unknown_langs = sorted(wanted - SUPPORTED)
    languages = wanted & SUPPORTED

    catalog = Catalog([e for e in request.endpoint_catalog if e.service_id == request.service_id])
    walk = walk_repository(Path(repo_root), languages)

    warnings: list[str] = []
    unsupported: list[str] = [f"unsupported_language: {l}" for l in unknown_langs]
    if not base_urls:
        warnings.append(f"no_base_urls: raw HTTP calls cannot be attributed to {request.service_id}")
    for path, reason in walk.skipped:
        warnings.append(f"skipped_file: {path} ({reason})")
    for path, reason in walk.skipped_dirs:
        warnings.append(f"skipped_dir: {path}/ ({reason})")

    findings: list[Finding] = []
    files_scanned = 0
    files_failed = 0
    for sf in walk.files:
        ctx = FileContext(sf.rel_path, sf.language, sf.data, request.service_id, base_urls, catalog)
        try:
            ADAPTERS[sf.language](ctx).run()
        except Exception as exc:  # one bad file must not erase the rest
            files_failed += 1
            warnings.append(f"adapter_error: {sf.rel_path} ({type(exc).__name__})")
            continue
        files_scanned += 1
        findings.extend(ctx.findings)
        warnings.extend(ctx.warnings)
        unsupported.extend(ctx.unsupported)

    deps: dict[str, Dependency] = {}
    use_site: dict[str, tuple] = {}
    for f in findings:
        use = f.evidence[-1]
        ep = f.origin.endpoint
        key = (
            snapshot.repository_connection_id, snapshot.commit_sha, request.enrollment_id,
            use.file, use.line, use.col, ep.service_id, ep.method.upper(), ep.path,
            _field_path(f.path), f.operation,
        )
        dep_id = _uuid("dependency", *key)
        use_site[dep_id] = (use.file, use.line, use.col)
        ev_seen, ev_list = set(), []
        for loc in f.evidence:
            k = (loc.file, loc.line, loc.col, loc.label)
            if k not in ev_seen:
                ev_seen.add(k)
                ev_list.append(_evidence(loc, snapshot))
        if dep_id in deps:
            existing = deps[dep_id]
            merged = {e.id: e for e in existing.evidence}
            for e in ev_list:
                merged.setdefault(e.id, e)
            # keep the strongest confidence observed for the same use site
            order = {"direct": 0, "inferred": 1, "unresolved": 2}
            conf = min(existing.confidence, f.confidence, key=lambda c: order[c])
            deps[dep_id] = existing.model_copy(update={
                "evidence": sorted(merged.values(), key=lambda e: (e.file or "", e.line_start or 0, e.id)),
                "confidence": conf,
            })
            continue
        deps[dep_id] = Dependency(
            id=dep_id,
            consumer_org_id=consumer_org_id,
            enrollment_id=request.enrollment_id,
            repository_snapshot_id=snapshot.id,
            service_id=ep.service_id,
            endpoint_id=ep.id,
            language=f.language,
            field_path=_field_path(f.path),
            origin={"kind": f.origin.kind, "library": f.origin.library, "call": f.origin.call},
            usage={"operation": f.operation, "operand": f.operand, "destination": f.destination},
            confidence=f.confidence,
            evidence=ev_list,
        )

    ordered = sorted(
        deps.values(), key=lambda d: (use_site[d.id], d.field_path, d.usage.operation, d.id)
    )
    return ScanResult(
        schema_version=SCHEMA_VERSION,
        repository_snapshot_id=snapshot.id,
        dependencies=ordered,
        coverage={
            "files_scanned": files_scanned,
            "files_skipped": len(walk.skipped) + files_failed,
            "unsupported_patterns": sorted(set(unsupported)),
        },
        warnings=sorted(set(warnings)),
    )

