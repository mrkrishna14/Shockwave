"""Contract models used by the scanner.

Canonical models live in Ishan's ``shared/schemas.py``. Until that bootstrap
lands, this module provides *contract-shaped local doubles* that follow
CONTRACTS.md (shockwave.v1) field-for-field. When ``shared.schemas`` is
importable, its models are used instead and nothing here is redefined.

Two additive, optional ScanRequest fields are proposed (see scanner/HANDOFF.md):
``consumer_org_id`` (Dependency requires it but ScanRequest does not carry it)
and ``base_urls`` (APIProject.base_urls, needed to recognise raw HTTP calls).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "shockwave.v1"

try:  # pragma: no cover - exercised only once Ishan's bootstrap exists
    from shared.schemas import (  # type: ignore[import-not-found]
        Dependency,
        Endpoint,
        Evidence,
        RepositorySnapshot,
        ScanRequest,
        ScanResult,
    )

    USING_SHARED_SCHEMAS = True
except ImportError:
    USING_SHARED_SCHEMAS = False

    Confidence = Literal["direct", "inferred", "unresolved"]

    class _Model(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Evidence(_Model):
        id: str
        kind: Literal["openapi", "source", "docs", "declared_change", "test"]
        visibility: Literal["consumer_private", "provider_owned", "public"]
        source_uri: str
        revision: str
        file: Optional[str] = None
        line_start: Optional[int] = Field(default=None, ge=1)
        line_end: Optional[int] = Field(default=None, ge=1)
        json_pointer: Optional[str] = None
        excerpt: Optional[str] = Field(default=None, max_length=500)

    class Endpoint(_Model):
        id: str
        service_id: str
        method: str
        path: str
        snapshot_id: str
        operation_id: Optional[str] = None
        request_schema: dict = Field(default_factory=dict)
        response_schema: dict = Field(default_factory=dict)
        evidence: list[Evidence] = Field(default_factory=list)
        reconciliation: Literal[
            "documented_and_found",
            "implementation_only",
            "documented_not_found_in_scan",
            "conflict",
            "not_compared",
        ] = "not_compared"
        confidence: Confidence = "direct"

    class DependencyOrigin(_Model):
        kind: Literal["sdk_response", "http_response"]
        library: str
        call: str

    class DependencyUsage(_Model):
        operation: Literal[
            "read", "divide", "multiply", "compare", "store", "pass_through", "unknown"
        ]
        operand: Union[float, int, str, None] = None
        destination: Optional[str] = None

    class Dependency(_Model):
        id: str
        consumer_org_id: str
        enrollment_id: str
        repository_snapshot_id: str
        service_id: str
        endpoint_id: str
        language: Literal["python", "javascript", "typescript"]
        field_path: str
        origin: DependencyOrigin
        usage: DependencyUsage
        confidence: Confidence
        evidence: list[Evidence]

    class RepositorySnapshot(_Model):
        id: str
        repository_connection_id: str
        commit_sha: str
        fetched_at: datetime
        content_hash: str

    class ScanRequest(_Model):
        repository_snapshot_id: str
        enrollment_id: str
        service_id: str
        endpoint_catalog: list[Endpoint]
        supported_languages: list[str] = Field(default_factory=list)
        # Proposed additive optional fields (pending Ishan's approval):
        consumer_org_id: Optional[str] = None
        base_urls: Optional[list[str]] = None

    class ScanCoverage(_Model):
        files_scanned: int
        files_skipped: int
        unsupported_patterns: list[str]

    class ScanResult(_Model):
        schema_version: Literal["shockwave.v1"] = SCHEMA_VERSION
        repository_snapshot_id: str
        dependencies: list[Dependency]
        coverage: ScanCoverage
        warnings: list[str]


__all__ = [
    "SCHEMA_VERSION",
    "USING_SHARED_SCHEMAS",
    "Dependency",
    "Endpoint",
    "Evidence",
    "RepositorySnapshot",
    "ScanRequest",
    "ScanResult",
]
