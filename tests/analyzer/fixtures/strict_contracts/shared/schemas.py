"""A deliberately strict, literal reading of CONTRACTS.md, used only by test_contract_strictness.

It stands in for Ishan's ``shared.schemas`` in a subprocess, to prove Kanishk's modules
do not depend on leniencies of the local doubles:

* ``schema_version`` is required on every top-level result (no default);
* optional values are required-nullable ("explicit null for unavailable optional values");
* identifiers are ``uuid.UUID`` objects (they still serialize to string UUIDs in JSON);
* nested models have names that differ from the doubles' (Coverage, origin, usage).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Confidence = Literal["direct", "inferred", "unresolved"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    id: uuid.UUID
    kind: Literal["openapi", "source", "docs", "declared_change", "test"]
    visibility: Literal["consumer_private", "provider_owned", "public"]
    source_uri: str
    revision: str
    file: str | None
    line_start: int | None
    line_end: int | None
    json_pointer: str | None
    excerpt: str | None


class Endpoint(_Strict):
    id: uuid.UUID
    service_id: str
    method: str
    path: str
    snapshot_id: uuid.UUID
    operation_id: str | None
    request_schema: dict
    response_schema: dict
    evidence: list[Evidence]
    reconciliation: Literal[
        "documented_and_found", "implementation_only", "documented_not_found_in_scan", "conflict", "not_compared"
    ]
    confidence: Confidence


class StrictCoverageRecord(_Strict):
    files_scanned: int
    files_skipped: int
    unsupported_patterns: list[str]


class DiscoveryInput(_Strict):
    service_id: str
    snapshot_id: uuid.UUID
    openapi_documents: list[str]
    source_snapshot_id: uuid.UUID | None
    frameworks: list[str]


class DiscoveryResult(_Strict):
    schema_version: Literal["shockwave.v1"]
    service_id: str
    snapshot_id: uuid.UUID
    endpoints: list[Endpoint]
    coverage: StrictCoverageRecord
    warnings: list[str]


class StrictOriginRecord(_Strict):
    kind: Literal["sdk_response", "http_response"]
    library: str
    call: str


class StrictUsageRecord(_Strict):
    operation: Literal["read", "divide", "multiply", "compare", "store", "pass_through", "unknown"]
    operand: float | int | str | None
    destination: str | None


class Dependency(_Strict):
    id: uuid.UUID
    consumer_org_id: uuid.UUID
    enrollment_id: uuid.UUID
    repository_snapshot_id: uuid.UUID
    service_id: str
    endpoint_id: uuid.UUID
    language: Literal["python", "javascript", "typescript"]
    field_path: str
    origin: StrictOriginRecord
    usage: StrictUsageRecord
    confidence: Confidence
    evidence: list[Evidence]


class RepositorySnapshot(_Strict):
    id: uuid.UUID
    repository_connection_id: uuid.UUID
    commit_sha: str
    fetched_at: datetime
    content_hash: str


class APIChange(_Strict):
    id: uuid.UUID
    api_project_id: uuid.UUID
    service_id: str
    endpoint_id: uuid.UUID
    old_snapshot_id: uuid.UUID
    new_snapshot_id: uuid.UUID
    field_path: str
    kind: Literal["field_removed", "type_changed", "enum_changed", "units_changed", "behavior_changed"]
    old_semantics: str
    new_semantics: str
    structural_breaking: bool
    evidence: list[Evidence]


class Risk(_Strict):
    id: uuid.UUID
    change_id: uuid.UUID
    consumer_org_id: uuid.UUID
    enrollment_id: uuid.UUID
    repository_snapshot_id: uuid.UUID
    dependency_ids: list[uuid.UUID]
    severity: Literal["critical", "high", "medium", "low", "unknown"]
    confidence: Confidence
    explanation: str
    suggested_action: str
    evidence: list[Evidence]


class AnalysisResult(_Strict):
    schema_version: Literal["shockwave.v1"]
    change_id: uuid.UUID
    risks: list[Risk]
    unresolved_dependency_ids: list[uuid.UUID]
    warnings: list[str]
    analysis_mode: Literal["rules", "model", "fixture"]


class VerificationPlan(_Strict):
    id: uuid.UUID
    risk_id: uuid.UUID
    repository_snapshot_id: uuid.UUID
    scenario_id: str
    runner_profile: Literal["trusted_python", "trusted_node", "sandbox_python", "sandbox_node"]
    test_files: list[str]
    old_fixture_id: str
    new_fixture_id: str
    expected_behavior: str
    timeout_seconds: int


class ExecutionResult(_Strict):
    exit_code: int | None
    timed_out: bool
    infrastructure_error: str | None
    artifact_ids: list[str]
    duration_ms: int
    tests_run: int


class VerificationResult(_Strict):
    schema_version: Literal["shockwave.v1"]
    id: uuid.UUID
    risk_id: uuid.UUID
    repository_snapshot_id: uuid.UUID
    status: Literal["verified_regression", "compatible_within_tested_scope", "inconclusive", "not_run"]
    baseline: ExecutionResult | None
    proposed: ExecutionResult | None
    scenario_id: str
    summary: str
    artifact_ids: list[str]
