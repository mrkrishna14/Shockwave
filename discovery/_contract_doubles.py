"""Contract-shaped local doubles of the shockwave.v1 models (CONTRACTS.md).

Ishan owns the canonical Pydantic models in ``shared/schemas.py``. Until that
bootstrap lands, Kanishk's modules import these doubles through
``discovery.contracts``, which switches to ``shared.schemas`` automatically once
it is importable. Field names, enum values and optionality mirror CONTRACTS.md;
do not add fields here. ``schema_version`` is required, as in the contract: pass it
explicitly. Nested objects (coverage, origin, usage) are modelled so
callers can pass plain dicts, which keeps call sites independent of the nested
class names Ishan chooses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "shockwave.v1"
MAX_EXCERPT_CHARS = 1000
HTTP_METHODS = frozenset({"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"})


def _uuid_string(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"expected a string UUID, got {value!r}") from exc


UUIDStr = Annotated[str, AfterValidator(_uuid_string)]
Confidence = Literal["direct", "inferred", "unresolved"]
Severity = Literal["critical", "high", "medium", "low", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Discovery ------------------------------------------------------------------


class Evidence(_Model):
    id: UUIDStr
    kind: Literal["openapi", "source", "docs", "declared_change", "test"]
    visibility: Literal["consumer_private", "provider_owned", "public"]
    source_uri: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    json_pointer: str | None = None
    excerpt: str | None = None

    @field_validator("file")
    @classmethod
    def _repository_relative_posix(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = value.split("/")
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in ("", ".", "..") for part in parts)
            or (len(value) > 1 and value[1] == ":")
        ):
            raise ValueError(f"file must be a normalized repository-relative POSIX path, got {value!r}")
        return value

    @field_validator("json_pointer")
    @classmethod
    def _json_pointer(cls, value: str | None) -> str | None:
        if value is not None and value != "" and not value.startswith("/"):
            raise ValueError(f"json_pointer must be empty or start with '/', got {value!r}")
        return value

    @field_validator("excerpt")
    @classmethod
    def _bounded_excerpt(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_EXCERPT_CHARS:
            raise ValueError(f"excerpt exceeds {MAX_EXCERPT_CHARS} characters")
        return value

    @model_validator(mode="after")
    def _line_range(self) -> Evidence:
        if self.line_start is not None and self.line_start < 1:
            raise ValueError("line_start is 1-based")
        if self.line_end is not None:
            if self.line_start is None:
                raise ValueError("line_end requires line_start")
            if self.line_end < self.line_start:
                raise ValueError("line_end must be >= line_start")
        if (self.line_start is not None) and self.file is None:
            raise ValueError("line numbers require file")
        return self


class Endpoint(_Model):
    id: UUIDStr
    service_id: str = Field(min_length=1)
    method: str
    path: str
    snapshot_id: UUIDStr
    operation_id: str | None = None
    request_schema: dict
    response_schema: dict
    evidence: list[Evidence]
    reconciliation: Literal[
        "documented_and_found",
        "implementation_only",
        "documented_not_found_in_scan",
        "conflict",
        "not_compared",
    ]
    confidence: Confidence

    @field_validator("method")
    @classmethod
    def _uppercase_method(cls, value: str) -> str:
        if value not in HTTP_METHODS:
            raise ValueError(f"method must be an uppercase HTTP method, got {value!r}")
        return value

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"path must start with '/', got {value!r}")
        return value


class Coverage(_Model):
    files_scanned: int = Field(ge=0)
    files_skipped: int = Field(ge=0)
    unsupported_patterns: list[str]


class DiscoveryInput(_Model):
    service_id: str = Field(min_length=1)
    snapshot_id: UUIDStr
    openapi_documents: list[str]
    source_snapshot_id: UUIDStr | None = None
    frameworks: list[str]


class DiscoveryResult(_Model):
    schema_version: Literal["shockwave.v1"]
    service_id: str = Field(min_length=1)
    snapshot_id: UUIDStr
    endpoints: list[Endpoint]
    coverage: Coverage
    warnings: list[str]


# Consumer analysis ------------------------------------------------------------


class DependencyOrigin(_Model):
    kind: Literal["sdk_response", "http_response"]
    library: str
    call: str


class DependencyUsage(_Model):
    operation: Literal["read", "divide", "multiply", "compare", "store", "pass_through", "unknown"]
    operand: float | int | str | None
    destination: str | None


class Dependency(_Model):
    id: UUIDStr
    consumer_org_id: UUIDStr
    enrollment_id: UUIDStr
    repository_snapshot_id: UUIDStr
    service_id: str = Field(min_length=1)
    endpoint_id: UUIDStr
    language: Literal["python", "javascript", "typescript"]
    field_path: str = Field(min_length=1)
    origin: DependencyOrigin
    usage: DependencyUsage
    confidence: Confidence
    evidence: list[Evidence]


class RepositorySnapshot(_Model):
    id: UUIDStr
    repository_connection_id: UUIDStr
    commit_sha: str = Field(min_length=1)
    fetched_at: datetime
    content_hash: str = Field(min_length=1)


class APIChange(_Model):
    id: UUIDStr
    api_project_id: UUIDStr
    service_id: str = Field(min_length=1)
    endpoint_id: UUIDStr
    old_snapshot_id: UUIDStr
    new_snapshot_id: UUIDStr
    field_path: str = Field(min_length=1)
    kind: Literal["field_removed", "type_changed", "enum_changed", "units_changed", "behavior_changed"]
    old_semantics: str
    new_semantics: str
    structural_breaking: bool
    evidence: list[Evidence]


class Risk(_Model):
    id: UUIDStr
    change_id: UUIDStr
    consumer_org_id: UUIDStr
    enrollment_id: UUIDStr
    repository_snapshot_id: UUIDStr
    dependency_ids: list[UUIDStr]
    severity: Severity
    confidence: Confidence
    explanation: str
    suggested_action: str
    evidence: list[Evidence]


class AnalysisResult(_Model):
    schema_version: Literal["shockwave.v1"]
    change_id: UUIDStr
    risks: list[Risk]
    unresolved_dependency_ids: list[UUIDStr]
    warnings: list[str]
    analysis_mode: Literal["rules", "model", "fixture"]


# Verification -----------------------------------------------------------------


class VerificationPlan(_Model):
    id: UUIDStr
    risk_id: UUIDStr
    repository_snapshot_id: UUIDStr
    scenario_id: str = Field(min_length=1)
    runner_profile: Literal["trusted_python", "trusted_node", "sandbox_python", "sandbox_node"]
    test_files: list[str]
    old_fixture_id: str = Field(min_length=1)
    new_fixture_id: str = Field(min_length=1)
    expected_behavior: str
    timeout_seconds: int = Field(gt=0)


class ExecutionResult(_Model):
    exit_code: int | None
    timed_out: bool
    infrastructure_error: str | None
    artifact_ids: list[str]
    duration_ms: int = Field(ge=0)
    tests_run: int = Field(ge=0)


class VerificationResult(_Model):
    schema_version: Literal["shockwave.v1"]
    id: UUIDStr
    risk_id: UUIDStr
    repository_snapshot_id: UUIDStr
    status: Literal["verified_regression", "compatible_within_tested_scope", "inconclusive", "not_run"]
    baseline: ExecutionResult | None
    proposed: ExecutionResult | None
    scenario_id: str = Field(min_length=1)
    summary: str
    artifact_ids: list[str]
