"""Canonical shockwave.v1 contracts. Internal models are not public serializers."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator

ID = Annotated[str, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
Confidence = Literal['direct', 'inferred', 'unresolved']
Severity = Literal['critical', 'high', 'medium', 'low', 'unknown']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Result(Model):
    schema_version: Literal['shockwave.v1'] = 'shockwave.v1'

class Organization(Model):
    id: ID
    name: str
    kind: Literal['provider', 'consumer']

class Membership(Model):
    user_id: ID
    organization_id: ID
    role: Literal['admin', 'member']

class APIProject(Model):
    id: ID
    provider_org_id: ID
    service_id: str
    name: str
    base_urls: list[str]
    created_at: datetime

class Invitation(Model):
    id: ID
    api_project_id: ID
    recipient_email: str
    expires_at: datetime
    status: Literal['pending', 'accepted', 'revoked', 'expired']
    delivery_status: Literal['pending', 'sent', 'failed', 'development_inbox']
    accepted_by_user_id: ID | None = None
    consumer_org_id: ID | None = None

class Enrollment(Model):
    id: ID
    api_project_id: ID
    consumer_org_id: ID
    status: Literal['active', 'revoked']
    share_summary: bool
    consent_version: str
    accepted_at: datetime
    revoked_at: datetime | None = None

class RepositoryConnection(Model):
    id: ID
    consumer_org_id: ID
    installation_id: str
    github_repository_id: str
    full_name: str
    default_branch: str
    status: Literal['connected', 'revoked', 'error']
    scan_consent_at: datetime
    allow_model_processing: bool
    allow_pr_write: bool

class EnrollmentRepository(Model):
    enrollment_id: ID
    repository_connection_id: ID

class RepositorySnapshot(Model):
    id: ID
    repository_connection_id: ID
    commit_sha: str
    fetched_at: datetime
    content_hash: str

class Evidence(Model):
    id: ID
    kind: Literal['openapi', 'source', 'docs', 'declared_change', 'test']
    visibility: Literal['consumer_private', 'provider_owned', 'public']
    source_uri: str
    revision: str
    file: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    json_pointer: str | None = None
    excerpt: str | None = Field(default=None, max_length=4000)

    @field_validator('file')
    @classmethod
    def relative_file(cls, value):
        if value and (value.startswith('/') or '\\' in value or '..' in value.split('/')):
            raise ValueError('Expected a POSIX repository-relative path')
        return value

class Endpoint(Model):
    id: ID
    service_id: str
    method: Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE']
    path: str
    snapshot_id: ID
    operation_id: str | None = None
    request_schema: dict
    response_schema: dict
    evidence: list[Evidence]
    reconciliation: Literal['documented_and_found', 'implementation_only', 'documented_not_found_in_scan', 'conflict', 'not_compared']
    confidence: Confidence

class Coverage(Model):
    files_scanned: int = Field(ge=0)
    files_skipped: int = Field(ge=0)
    unsupported_patterns: list[str]

class DiscoveryInput(Model):
    service_id: str
    snapshot_id: ID
    openapi_documents: list[str]
    source_snapshot_id: ID | None = None
    frameworks: list[str]

class DiscoveryResult(Result):
    service_id: str
    snapshot_id: ID
    endpoints: list[Endpoint]
    coverage: Coverage
    warnings: list[str]

class Origin(Model):
    kind: Literal['sdk_response', 'http_response']
    library: str
    call: str

class Usage(Model):
    operation: Literal['read', 'divide', 'multiply', 'compare', 'store', 'pass_through', 'unknown']
    operand: float | str | None
    destination: str | None

class Dependency(Model):
    id: ID
    consumer_org_id: ID
    enrollment_id: ID
    repository_snapshot_id: ID
    service_id: str
    endpoint_id: ID
    language: Literal['python', 'javascript', 'typescript']
    field_path: str
    origin: Origin
    usage: Usage
    confidence: Confidence
    evidence: list[Evidence]

class ScanRequest(Model):
    repository_snapshot_id: ID
    enrollment_id: ID
    service_id: str
    endpoint_catalog: list[Endpoint]
    supported_languages: list[str]

class ScanResult(Result):
    repository_snapshot_id: ID
    dependencies: list[Dependency]
    coverage: Coverage
    warnings: list[str]

class APIChange(Model):
    id: ID
    api_project_id: ID
    service_id: str
    endpoint_id: ID
    old_snapshot_id: ID
    new_snapshot_id: ID
    field_path: str
    kind: Literal['field_removed', 'type_changed', 'enum_changed', 'units_changed', 'behavior_changed']
    old_semantics: str
    new_semantics: str
    structural_breaking: bool
    evidence: list[Evidence]

class Risk(Model):
    id: ID
    change_id: ID
    consumer_org_id: ID
    enrollment_id: ID
    repository_snapshot_id: ID
    dependency_ids: list[ID]
    severity: Severity
    confidence: Confidence
    explanation: str
    suggested_action: str
    evidence: list[Evidence]

class AnalysisResult(Result):
    change_id: ID
    risks: list[Risk]
    unresolved_dependency_ids: list[ID]
    warnings: list[str]
    analysis_mode: Literal['rules', 'model', 'fixture']

class VerificationPlan(Model):
    id: ID
    risk_id: ID
    repository_snapshot_id: ID
    scenario_id: str
    runner_profile: Literal['trusted_python', 'trusted_node', 'sandbox_python', 'sandbox_node']
    test_files: list[str]
    old_fixture_id: str
    new_fixture_id: str
    expected_behavior: str
    timeout_seconds: int = Field(ge=1, le=120)

class ExecutionResult(Model):
    exit_code: int | None
    timed_out: bool
    infrastructure_error: str | None
    artifact_ids: list[ID]
    duration_ms: int = Field(ge=0)
    tests_run: int = Field(ge=0)

class VerificationResult(Result):
    id: ID
    risk_id: ID
    repository_snapshot_id: ID
    status: Literal['verified_regression', 'compatible_within_tested_scope', 'inconclusive', 'not_run']
    baseline: ExecutionResult | None
    proposed: ExecutionResult | None
    scenario_id: str
    summary: str
    artifact_ids: list[ID]

class MigrationRequest(Model):
    risk_id: ID
    verification_id: ID
    repository_snapshot_id: ID
    target_contract_snapshot_id: ID
    mode: Literal['preview', 'publish_pr']

class MigrationResult(Result):
    id: ID
    risk_id: ID
    repository_snapshot_id: ID
    target_contract_snapshot_id: ID
    status: Literal['patch_proposed', 'patch_validated', 'validation_failed', 'blocked', 'pr_opened']
    generation_mode: Literal['rule', 'model', 'fixture']
    patch_artifact_id: ID | None
    changed_files: list[str]
    validation: ExecutionResult | None
    pr_url: str | None
    branch: str | None
    warnings: list[str]

class Readiness(Model):
    risk_id: ID
    migration_id: ID | None
    approval: Literal['unknown', 'pending', 'approved', 'rejected']
    merge: Literal['unknown', 'not_merged', 'merged']
    deployment: Literal['unknown', 'reported_deployed']
    deployment_evidence: str | None

class Error(Model):
    code: str
    message: str
    retryable: bool

class Job(Result):
    id: ID
    owner_org_id: ID
    kind: Literal['discovery', 'scan', 'analysis', 'verification', 'migration']
    status: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
    stage: str
    created_at: datetime
    updated_at: datetime
    result_id: ID | None
    error: Error | None

class ProviderImpactSummary(Model):
    api_project_id: ID
    change_id: ID
    enrollment_id: ID
    consumer_display_name: str
    coverage: Literal['unscanned', 'partial', 'scanned']
    predicted_risk_count: int = Field(ge=0)
    verified_regression_count: int = Field(ge=0)
    validated_fix_count: int = Field(ge=0)
    pending_approval_count: int = Field(ge=0)
    deployment: Literal['unknown', 'partially_reported', 'all_reported']

class DeliveryResult(Model):
    status: Literal['sent', 'failed', 'development_inbox']
    message_id: str | None
    error: str | None

class PublicationAuthorization(Model):
    user_id: ID
    consumer_org_id: ID
    repository_connection_id: ID
    expected_commit: str
    consent_timestamp: datetime
