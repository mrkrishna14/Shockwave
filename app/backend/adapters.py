"""Integration boundary. Production must inject owners' implementations.
No GitHub, email, model, or arbitrary repository execution happens here.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from shared.schemas import *

class DiscoveryAdapter(Protocol):
    def discover_endpoints(self, request: DiscoveryInput, fetched_inputs: dict, source_root: Path | None) -> DiscoveryResult: ...
class ScannerAdapter(Protocol):
    def scan_repository(self, request: ScanRequest, snapshot: RepositorySnapshot, repo_root: Path) -> ScanResult: ...
class AnalysisAdapter(Protocol):
    def detect_changes(self, old: DiscoveryResult, new: DiscoveryResult, declared_changes: list[APIChange]) -> list[APIChange]: ...
    def analyze_impact(self, change: APIChange, dependencies: list[Dependency]) -> AnalysisResult: ...
    def build_verification_plan(self, risk: Risk, snapshot: RepositorySnapshot) -> VerificationPlan: ...
class IOAdapter(Protocol):
    def fetch_repository(self, connection: RepositoryConnection, commit_sha: str) -> RepositorySnapshot: ...
    def send_invitation(self, recipient_email: str, notice: dict, idempotency_key: str) -> DeliveryResult: ...
    def verify_regression(self, plan: VerificationPlan, repo_root: Path) -> VerificationResult: ...
    def generate_migration(self, request: MigrationRequest, risk: Risk, verification: VerificationResult, repo_root: Path) -> MigrationResult: ...
    def publish_migration(self, migration: MigrationResult, connection: RepositoryConnection, authorization: PublicationAuthorization) -> MigrationResult: ...


@dataclass(frozen=True)
class VerifiedPrincipal:
    """Identity supplied by a verified production provider, never a demo persona cookie."""
    subject: str
    verified_email: str


class ProductionIdentityProvider(Protocol):
    def authenticate(self, bearer_token: str) -> VerifiedPrincipal: ...


class ProductionInstallationVerifier(Protocol):
    def verify_binding(self, principal: VerifiedPrincipal, organization_id: str,
                       state: str, installation_id: str) -> str: ...


class ProductionEmailTransport(Protocol):
    def send_invitation(self, recipient_email: str, acceptance_url: str,
                        idempotency_key: str) -> DeliveryResult: ...


class IsolatedRunner(Protocol):
    """An implementation must enforce isolation before processing customer source."""
    def verify_regression(self, plan: VerificationPlan, snapshot: RepositorySnapshot,
                          repo_root: Path) -> VerificationResult: ...


class ProductionPublicationGateway(Protocol):
    def publish_migration(self, migration: MigrationResult, connection: RepositoryConnection,
                          authorization: PublicationAuthorization) -> MigrationResult: ...
