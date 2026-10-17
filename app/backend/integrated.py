"""Real-module adapters for the platform orchestrator (integration mode).

Live (the owners' real modules, called with shared.schemas models):
- discovery: Kanishk's ``discovery.discover_endpoints`` on the hand-maintained Demo Payments spec
  plus a literal FastAPI scan of examples/provider/demo_payments/service
- scan: Noirit's ``scanner.scan_repository``
- changes / impact / plans: Kanishk's ``analyzer.detect_changes``, ``analyzer.analyze_impact``,
  ``verification.scenarios.build_verification_plan``
- verification / migration: Sai's ``verification.runner.RunnerService.verify_regression`` and
  ``migration.MigrationService.generate_migration``

Labeled stand-ins (inherited from Ishan's DemoAdapters or local):
- repository fetch: a LOCAL TRUSTED TEAM FIXTURE copied from examples/consumers (Noirit's
  demo-payments consumers), not a GitHub snapshot; Sai's GitHub transport needs App credentials
- invitation delivery: the development inbox (DemoAdapters)
- publication: disabled (DemoAdapters raises); no PR is ever created here
Execution is limited to registered, team-owned fixtures. It is not a sandbox.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The owners' modules live at the repository root; uvicorn runs from app/.
_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.append(_REPOSITORY_ROOT)

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from functools import partial

from backend.demo import DemoAdapters
from backend.store import now, uid
from shared.schemas import APIChange, DiscoveryResult, RepositorySnapshot

from analyzer import analyze_impact as kanishk_analyze_impact
from analyzer import detect_changes as kanishk_detect_changes
from analyzer.demo import DECLARED_CHANGE_PATH, PROVIDER_IDS_PATH, discover_demo
from integrations.storage import ArtifactStore
from migration.patches import ApprovedTarget
from migration.service import MigrationService
from scanner import scan_repository as noirit_scan_repository
from verification.runner.profiles import Scenario, TrustedSnapshot
from verification.runner.service import RunnerService
from verification.scenarios.plans import PlanConfig
from verification.scenarios.plans import build_verification_plan as kanishk_build_plan

ROOT = Path(__file__).resolve().parents[2]
CONSUMERS = ROOT / "examples" / "consumers"
DEFAULT_REPOSITORIES = {"local-demo/storefront": CONSUMERS / "python-display"}
FIXTURE_LABEL = "LOCAL TRUSTED TEAM FIXTURE (examples/consumers); not a GitHub snapshot"
_CONVERSION = re.compile(r"""[A-Za-z_]\w*(?:\[\s*['"]amount['"]\s*\]|\.amount)\s*/\s*100(?:\.0)?""")


@dataclass
class IntegratedState:
    """Process-wide state shared by the per-job adapter instances."""

    work_dir: Path
    repositories: dict[str, Path] = field(default_factory=lambda: dict(DEFAULT_REPOSITORIES))
    snapshots: dict[str, TrustedSnapshot] = field(default_factory=dict)
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    plans: dict[str, object] = field(default_factory=dict)  # risk id -> VerificationPlan
    dependencies: dict[str, object] = field(default_factory=dict)  # dependency id -> Dependency
    contracts: tuple[DiscoveryResult, DiscoveryResult] | None = None

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir)
        self.artifacts = ArtifactStore(self.work_dir / "artifacts")
        self.runner = RunnerService(self.artifacts, self.snapshots, self.scenarios,
                                    consent_check=self.registered)

    def registered(self, snapshot_id: str) -> bool:
        # The orchestrator re-checks enrollment and repository consent before and after
        # each job; the runner refuses anything that was not registered by fetch_repository.
        return snapshot_id in self.snapshots

    def contract_pair(self) -> tuple[DiscoveryResult, DiscoveryResult]:
        if self.contracts is None:
            ids = json.loads(PROVIDER_IDS_PATH.read_text(encoding="utf-8"))
            self.contracts = (discover_demo(ids, "v1"), discover_demo(ids, "v2_proposed"))
        return self.contracts


class IntegratedAdapters(DemoAdapters):
    mode = "integrated"

    def __init__(self, consumer_org_id=None, *, state: IntegratedState):
        super().__init__(consumer_org_id=consumer_org_id)
        self.state = state

    # Discovery and contracts (Kanishk) -------------------------------------------------

    def discover_endpoints(self, request, fetched_inputs, source_root):
        if request.service_id != "demo-payments":
            raise ValueError("integrated discovery is wired for the demo-payments project only")
        return self.state.contract_pair()[0]

    def contract_snapshots(self, project):
        return self.state.contract_pair()

    def endpoint_catalog(self, project):
        return list(self.state.contract_pair()[0].endpoints)

    def declared_changes(self, project, old, new):
        """The provider's declared cents-to-dollars change, re-keyed to this project."""
        declared = APIChange.model_validate(json.loads(DECLARED_CHANGE_PATH.read_text(encoding="utf-8")))
        return [declared.model_copy(update={"api_project_id": project["id"]})]

    def detect_changes(self, old, new, declared_changes, *, api_project_id=None):
        return kanishk_detect_changes(old, new, declared_changes, api_project_id=api_project_id)

    def analyze_impact(self, change, dependencies):
        for dependency in dependencies:
            self.state.dependencies[dependency.id] = dependency
        return kanishk_analyze_impact(change, dependencies, endpoints=self.endpoint_catalog(None))

    # Repository fetch (labeled stand-in) and scan (Noirit) ------------------------------

    def fetch_repository(self, connection, commit_sha):
        source = self.state.repositories.get(connection.full_name)
        if source is None:
            raise ValueError(f"no local trusted fixture registered for {connection.full_name}")
        root = self.state.work_dir / "snapshots" / f"{connection.id}-{commit_sha}"
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(source, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "node_modules"))
        trusted = TrustedSnapshot.register_team_fixture(root, commit_sha)
        snapshot = RepositorySnapshot(id=uid(), repository_connection_id=connection.id, commit_sha=commit_sha,
                                      fetched_at=now(), content_hash=trusted.content_hash)
        self.state.snapshots[snapshot.id] = trusted
        return snapshot

    def scan_repository(self, request, snapshot, repo_root):
        trusted = self.state.snapshots[snapshot.id]
        return noirit_scan_repository(request, snapshot, trusted.root, consumer_org_id=self.consumer_org_id)

    # Plans (Kanishk), verification and migration (Sai) ----------------------------------

    def build_verification_plan(self, risk, snapshot):
        trusted = PlanConfig(frozenset({str(snapshot.repository_connection_id)}))
        plan = kanishk_build_plan(risk, snapshot, config=trusted)
        new_snapshot = self.state.contract_pair()[1].snapshot_id
        self.state.scenarios[plan.scenario_id] = Scenario.from_catalog(plan, new_snapshot)
        self.state.plans[risk.id] = plan
        return plan

    def verify_regression(self, plan, repo_root):
        result = self.state.runner.verify_regression(plan, self.state.snapshots[plan.repository_snapshot_id].root)
        self.logs = self._bounded_logs(result.artifact_ids)
        return result

    def generate_migration(self, request, risk, verification, repo_root):
        snapshot = self.state.snapshots[request.repository_snapshot_id]
        plan = self.state.plans[risk.id]
        file, line, expression = self._use_site(risk, snapshot.root)
        target = ApprovedTarget(risk.id, request.repository_snapshot_id, snapshot.commit_sha, risk_service(risk, self.state),
                                file, line, expression,
                                hashlib.sha256((snapshot.root / file).read_bytes()).hexdigest(),
                                request.target_contract_snapshot_id, plan)
        service = MigrationService(self.state.runner, self.state.artifacts, {risk.id: target},
                                   consent_check=self.state.registered)
        result = service.generate_migration(request, risk, verification, snapshot.root)
        self.patch = self.state.artifacts.get(result.patch_artifact_id).decode() if result.patch_artifact_id else ""
        self.logs = self._bounded_logs(result.validation.artifact_ids if result.validation else [])
        return result

    def _use_site(self, risk, root: Path):
        """The scanner's use-site evidence (last source item) holding a '/ 100' amount conversion."""
        for dependency_id in risk.dependency_ids:
            dependency = self.state.dependencies.get(dependency_id)
            sources = [item for item in (dependency.evidence if dependency else []) if item.kind == "source" and item.file]
            if not sources:
                continue
            use = sources[-1]
            text = (root / use.file).read_text(encoding="utf-8").splitlines()[use.line_start - 1]
            match = _CONVERSION.search(text)
            if match:
                return use.file, use.line_start, match.group(0)
        raise ValueError("no approved '/ 100' amount conversion at the risk's use-site evidence")

    def _bounded_logs(self, artifact_ids) -> str:
        texts = []
        for artifact_id in artifact_ids:
            try:
                texts.append(self.state.artifacts.get(artifact_id).decode("utf-8", "replace"))
            except (KeyError, OSError, ValueError):
                continue
        return "\n".join(texts)[:16000]


def risk_service(risk, state: IntegratedState) -> str:
    services = {state.dependencies[d].service_id for d in risk.dependency_ids if d in state.dependencies}
    if len(services) != 1:
        raise ValueError("a migration target needs exactly one service")
    return services.pop()


def integrated_factory(work_dir: Path | None = None, repositories: dict[str, Path] | None = None):
    """An adapters_factory for create_app(..., adapters_factory=integrated_factory())."""
    state = IntegratedState(Path(work_dir or tempfile.mkdtemp(prefix="shockwave-integrated-")))
    if repositories:
        state.repositories.update(repositories)
    factory = partial(IntegratedAdapters, state=state)
    factory.accepts_consumer_org_id = True
    factory.state = state
    return factory
