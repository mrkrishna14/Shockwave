"""Real-module adapters for the platform orchestrator (integration mode).

Two providers are wired, chosen by the API project's ``service_id``:

* ``stripe`` (the demo): discovery of the pinned public Stripe OpenAPI contract (provider source
  is unavailable), consumer repositories = the team's Stripe consumer apps (demo/templates:
  checkout and fraud review), Noirit's real scanner, the declared change from the Stripe change
  manifest (hypothetical, not announced by Stripe), Kanishk's analysis and case scenarios run with
  the local runner double, and the prepared, tested migration patches from demo/migrations.
* ``demo-payments``: the hand-maintained Demo Payments spec + FastAPI source scan, Noirit's
  consumer fixture, Sai's RunnerService and MigrationService.

Stand-ins (inherited from Ishan's DemoAdapters or local): repository fetch copies a team-owned
local repository instead of fetching from GitHub; invitations use the in-app inbox; publication
is disabled. Execution is limited to registered, team-owned code. It is not a sandbox.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The owners' modules live at the repository root (and the Stripe demo helpers under demo/scripts);
# uvicorn runs from app/.
_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_ROOT), str(_ROOT / "demo" / "scripts")):
    if _entry not in sys.path:
        sys.path.append(_entry)

import hashlib
import json
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from functools import partial

from analyzer import analyze_impact as kanishk_analyze_impact
from analyzer import detect_changes as kanishk_detect_changes
from analyzer.demo import DECLARED_CHANGE_PATH, PROVIDER_IDS_PATH, discover_demo
from integrations.storage import ArtifactStore
from migration.patches import ApprovedTarget
from migration.service import MigrationService
from scanner import scan_repository as noirit_scan_repository
from verification.runner.profiles import Scenario, TrustedSnapshot
from verification.runner.service import RunnerService
from verification.scenarios.catalog import get_scenario
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import (
    classify_phase,
    interpret_patched_run,
    interpret_verification,
)
from verification.scenarios.plans import (
    NoExecutableScenario,
    PlanConfig,
    not_run_result,
)
from verification.scenarios.plans import build_verification_plan as kanishk_build_plan

from backend.demo import DemoAdapters
from backend.store import now, uid
from shared.schemas import (
    APIChange,
    DiscoveryResult,
    ExecutionResult,
    MigrationResult,
    RepositorySnapshot,
    VerificationPlan,
)

ROOT = _ROOT
CONSUMERS = ROOT / "examples" / "consumers"
TEMPLATES = ROOT / "demo" / "templates"
STRIPE = "stripe"
NODE = ROOT / ".venv" / "bin" / "node"
NODE_MODULES = ROOT / "demo" / "templates" / "checkout" / "node_modules"  # stripe-node 23.0.0 (npm ci)
DEFAULT_REPOSITORIES = {"acme/storefront": CONSUMERS / "python-display"}
# Stripe consumer repositories offered to the consumer: full name -> template directory.
STRIPE_REPOSITORIES = {
    "acme/stripe-checkout": TEMPLATES / "checkout",
    "acme/fraud-review": TEMPLATES / "fraud",
}
SERVICE_REPOSITORIES = {STRIPE: list(STRIPE_REPOSITORIES), "demo-payments": list(DEFAULT_REPOSITORIES)}
FIXTURE_LABEL = "LOCAL TEAM REPOSITORY (copied from this checkout); not a GitHub snapshot"
NOT_EXECUTABLE = "not-executable"
_CONVERSION = re.compile(r"""[A-Za-z_]\w*(?:\[\s*['"]amount['"]\s*\]|\.amount)\s*/\s*100(?:\.0)?""")
_STRIPE_DETAILED = "GET /v1/payment_intents/{intent}"


@dataclass
class IntegratedState:
    """Process-wide state shared by the per-job adapter instances."""

    work_dir: Path
    repositories: dict[str, Path] = field(default_factory=lambda: dict(DEFAULT_REPOSITORIES))
    snapshots: dict[str, TrustedSnapshot] = field(default_factory=dict)
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    plans: dict[str, object] = field(default_factory=dict)  # risk id -> VerificationPlan
    risks: dict[str, object] = field(default_factory=dict)  # risk id -> Risk (for not_run results)
    unrunnable: dict[str, str] = field(default_factory=dict)  # risk id -> why no scenario exists
    dependencies: dict[str, object] = field(default_factory=dict)  # dependency id -> Dependency
    contracts: dict[str, tuple[DiscoveryResult, DiscoveryResult]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir)
        self.repositories.update(STRIPE_REPOSITORIES)
        self.artifacts = ArtifactStore(self.work_dir / "artifacts")
        self.records: dict[str, dict] = {}  # snapshot id -> {root, commit_sha, content_hash, connection id}
        self._load_registry()
        self.runner = RunnerService(self.artifacts, self.snapshots, self.scenarios,
                                    consent_check=self.registered)

    @property
    def registry_path(self) -> Path:
        return self.work_dir / "registry.json"

    def _load_registry(self) -> None:
        """Re-register snapshots fetched before a backend restart (unchanged copies only)."""
        try:
            records = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for snapshot_id, record in records.items():
            root = Path(record["root"])
            if not root.is_dir():
                continue
            trusted = TrustedSnapshot.register_team_fixture(root, record["commit_sha"])
            if trusted.content_hash == record["content_hash"]:
                self.snapshots[snapshot_id] = trusted
                self.records[snapshot_id] = record

    def remember(self, snapshot, trusted: TrustedSnapshot) -> None:
        self.snapshots[snapshot.id] = trusted
        self.records[snapshot.id] = {"root": str(trusted.root), "commit_sha": trusted.commit_sha,
                                     "content_hash": trusted.content_hash,
                                     "repository_connection_id": str(snapshot.repository_connection_id),
                                     "fetched_at": snapshot.fetched_at if isinstance(snapshot.fetched_at, str)
                                     else snapshot.fetched_at.isoformat()}
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(self.records, indent=2), encoding="utf-8")

    def snapshot_record(self, snapshot_id: str) -> RepositorySnapshot:
        record = self.records[snapshot_id]
        return RepositorySnapshot(id=snapshot_id, repository_connection_id=record["repository_connection_id"],
                                  commit_sha=record["commit_sha"], fetched_at=record["fetched_at"],
                                  content_hash=record["content_hash"])

    def registered(self, snapshot_id: str) -> bool:
        # The orchestrator re-checks enrollment and repository consent before and after
        # each job; the runner refuses anything that was not registered by fetch_repository.
        return snapshot_id in self.snapshots

    def contract_pair(self, service_id: str = "demo-payments") -> tuple[DiscoveryResult, DiscoveryResult]:
        """(current, proposed) contract snapshots; Stripe's proposed contract is the same pinned bytes."""
        if service_id not in self.contracts:
            if service_id == STRIPE:
                from shockwave_demo.common import IDS_PATH
                from shockwave_demo.inventory import discover_stripe

                ids = json.loads(IDS_PATH.read_text(encoding="utf-8"))
                self.contracts[STRIPE] = (discover_stripe(ids["baseline_contract_snapshot_id"]).result,
                                          discover_stripe(ids["proposed_contract_snapshot_id"]).result)
            elif service_id == "demo-payments":
                ids = json.loads(PROVIDER_IDS_PATH.read_text(encoding="utf-8"))
                self.contracts[service_id] = (discover_demo(ids, "v1"), discover_demo(ids, "v2_proposed"))
            else:
                raise ValueError(f"integrated mode supports the stripe and demo-payments projects, not {service_id}")
        return self.contracts[service_id]


def _slim(endpoint):
    """A stored catalog entry: full schemas only for the operation the demo change touches."""
    if f"{endpoint.method} {endpoint.path}" == _STRIPE_DETAILED:
        return endpoint
    note = {"x-shockwave-schema": "omitted from the stored catalog for size; the pinned contract has it"}
    return endpoint.model_copy(update={"request_schema": note, "response_schema": note})


def _run_dir(state: IntegratedState, snapshot_root: Path) -> Path:
    """A fresh work dir for one run. Node consumers resolve stripe-node from a node_modules link
    placed beside the run's checkout copy (Node searches parent directories); snapshots stay link-free."""
    work = state.work_dir / "runs" / uid()
    work.mkdir(parents=True)
    if (snapshot_root / "package.json").is_file():
        if not (NODE_MODULES / "stripe").is_dir():
            raise ValueError(f"{NODE_MODULES.parent} has no installed node_modules; run `npm ci` there "
                             "(demo/scripts/demo.py prepare does it)")
        (work / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    return work


def _proof_lines(plan, phase: str, run) -> list[str]:
    """Machine-readable relevant-case evidence for the UI's proof card (from the scenario report)."""
    try:
        classification = classify_phase(plan, phase, run)
    except Exception:  # noqa: BLE001 - evidence display must never break a verification
        return []
    cases = classification.evaluation.cases if classification.evaluation is not None else ()
    lines = []
    for case in cases:
        document = case.to_document()
        document.pop("requests", None)
        if document.get("role") == "relevant":
            lines.append("SHOCKWAVE-PROOF " + json.dumps({"phase": phase, **document}, default=str))
    return lines


def repository_options(org_id: str, services: set[str]) -> list[dict]:
    """The repositories the consumer's (local) installation offers, for its enrolled APIs."""
    names = [name for service in sorted(services or {STRIPE}) for name in SERVICE_REPOSITORIES.get(service, [])]
    return [{"github_repository_id": f"{name}#{org_id}", "full_name": name, "default_branch": "main", "mode": "local"}
            for name in dict.fromkeys(names)]


class IntegratedAdapters(DemoAdapters):
    mode = "integrated"

    def __init__(self, consumer_org_id=None, *, state: IntegratedState):
        super().__init__(consumer_org_id=consumer_org_id)
        self.state = state

    # Discovery and contracts (Kanishk) -------------------------------------------------

    def discover_endpoints(self, request, fetched_inputs, source_root):
        current = self.state.contract_pair(request.service_id)[0]
        if request.service_id == STRIPE:
            return current.model_copy(update={"endpoints": [_slim(endpoint) for endpoint in current.endpoints]})
        return current

    def contract_snapshots(self, project):
        return self.state.contract_pair(project["service_id"])

    def endpoint_catalog(self, project):
        service = project["service_id"] if isinstance(project, dict) else project
        return list(self.state.contract_pair(service)[0].endpoints)

    def declared_changes(self, project, old, new):
        if project["service_id"] == STRIPE:
            return [self._stripe_declared_change(project, old)]
        declared = APIChange.model_validate(json.loads(DECLARED_CHANGE_PATH.read_text(encoding="utf-8")))
        return [declared.model_copy(update={"api_project_id": project["id"]})]

    def _stripe_declared_change(self, project, current):
        """The declared change from the Stripe change manifest, re-keyed to this project."""
        from discovery.pinned import STRIPE_PIN_PATH
        from shockwave_demo.change import build_declared_change
        from shockwave_demo.common import CHANGE_MANIFEST_PATH, IDS_PATH
        from shockwave_demo.inventory import endpoint_record, find_endpoint

        manifest = json.loads(CHANGE_MANIFEST_PATH.read_text(encoding="utf-8"))
        ids = {**json.loads(IDS_PATH.read_text(encoding="utf-8")), "api_project_id": project["id"]}
        endpoint = find_endpoint(list(current.endpoints), manifest["endpoint"]["method"], manifest["endpoint"]["path"])
        record = endpoint_record(endpoint, json.loads(STRIPE_PIN_PATH.read_text(encoding="utf-8")))
        docs = next(item for item in record["evidence"] if item["kind"] == "docs")
        declared = build_declared_change(manifest, ids, endpoint, docs)
        # One change per project: the manifest-derived id is the same for every project.
        return declared.model_copy(update={"id": str(uuid.uuid5(uuid.UUID(str(declared.id)), project["id"]))})

    def detect_changes(self, old, new, declared_changes, *, api_project_id=None):
        return kanishk_detect_changes(old, new, declared_changes, api_project_id=api_project_id)

    def analyze_impact(self, change, dependencies):
        for dependency in dependencies:
            self.state.dependencies[dependency.id] = dependency
        return kanishk_analyze_impact(change, dependencies, endpoints=self.endpoint_catalog(change.service_id))

    # Repository fetch (local stand-in) and scan (Noirit) --------------------------------

    def fetch_repository(self, connection, commit_sha):
        source = self.state.repositories.get(connection.full_name)
        if source is None:
            raise ValueError(f"no local team repository registered for {connection.full_name}")
        root = self.state.work_dir / "snapshots" / f"{connection.id}-{commit_sha}"
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(source, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "node_modules"))
        trusted = TrustedSnapshot.register_team_fixture(root, commit_sha)
        snapshot = RepositorySnapshot(id=uid(), repository_connection_id=connection.id, commit_sha=commit_sha,
                                      fetched_at=now(), content_hash=trusted.content_hash)
        self.state.remember(snapshot, trusted)
        return snapshot

    def scan_repository(self, request, snapshot, repo_root):
        trusted = self.state.snapshots[snapshot.id]
        return noirit_scan_repository(request, snapshot, trusted.root, consumer_org_id=self.consumer_org_id)

    # Plans (Kanishk), verification and migration ---------------------------------------

    def build_verification_plan(self, risk, snapshot):
        self.state.risks[risk.id] = risk
        trusted = PlanConfig(frozenset({str(snapshot.repository_connection_id)}))
        try:
            plan = kanishk_build_plan(risk, snapshot, config=trusted)
        except NoExecutableScenario as error:
            # No executable scenario: an honest not_run result, never a crash.
            self.state.unrunnable[risk.id] = str(error)
            return VerificationPlan(id=uid(), risk_id=risk.id, repository_snapshot_id=snapshot.id,
                                    scenario_id=NOT_EXECUTABLE, runner_profile="trusted_python", test_files=[],
                                    old_fixture_id="none", new_fixture_id="none",
                                    expected_behavior="No executable scenario exists for this usage.", timeout_seconds=1)
        spec = get_scenario(plan.scenario_id)
        if spec.service_id != STRIPE:
            new_snapshot = self.state.contract_pair(spec.service_id)[1].snapshot_id
            self.state.scenarios[plan.scenario_id] = Scenario.from_catalog(plan, new_snapshot)
        self.state.plans[risk.id] = plan
        return plan

    def verify_regression(self, plan, repo_root):
        if plan.scenario_id == NOT_EXECUTABLE:
            risk = self.state.risks[plan.risk_id]
            reason = self.state.unrunnable.get(plan.risk_id, "no executable scenario")
            self.logs = f"Not run: {reason}"
            return not_run_result(risk, f"No executable scenario for this usage, so it remains a prediction ({reason}).")
        if get_scenario(plan.scenario_id).service_id == STRIPE:
            return self._verify_locally(plan)
        result = self.state.runner.verify_regression(plan, self.state.snapshots[plan.repository_snapshot_id].root)
        self.logs = self._bounded_logs(result.artifact_ids)
        return result

    def _verify_locally(self, plan):
        """Stripe case scenarios: current and proposed runs of the real consumer code (runner double)."""
        root = self.state.snapshots[plan.repository_snapshot_id].root
        work = _run_dir(self.state, root)
        try:
            runs = {phase: run_phase_locally(plan, root, phase, work_dir=work, node_executable=str(NODE))
                    for phase in ("baseline", "proposed")}
        finally:
            shutil.rmtree(work, ignore_errors=True)
        result = interpret_verification(plan, runs["baseline"], runs["proposed"])
        proof = [line for phase, run in runs.items() for line in _proof_lines(plan, phase, run)]
        tails = [f"--- {phase} output ---\n{getattr(run, 'output_tail', '') or ''}" for phase, run in runs.items()]
        self.logs = "\n".join([result.summary, *proof, *tails])[:16000]
        return result

    def generate_migration(self, request, risk, verification, repo_root):
        snapshot = self.state.snapshots[request.repository_snapshot_id]
        plan = self.state.plans.get(risk.id) or self.build_verification_plan(
            risk, self.state.snapshot_record(request.repository_snapshot_id))
        if get_scenario(plan.scenario_id).service_id == STRIPE:
            return self._migrate_locally(request, plan, snapshot)
        file, line, expression = self._use_site(risk, snapshot.root)
        target = ApprovedTarget(risk.id, request.repository_snapshot_id, snapshot.commit_sha,
                                get_scenario(plan.scenario_id).service_id, file, line, expression,
                                hashlib.sha256((snapshot.root / file).read_bytes()).hexdigest(),
                                request.target_contract_snapshot_id, plan)
        service = MigrationService(self.state.runner, self.state.artifacts, {risk.id: target},
                                   consent_check=self.state.registered)
        result = service.generate_migration(request, risk, verification, snapshot.root)
        self.patch = self.state.artifacts.get(result.patch_artifact_id).decode() if result.patch_artifact_id else ""
        self.logs = self._bounded_logs(result.validation.artifact_ids if result.validation else [])
        return result

    def _migrate_locally(self, request, plan, snapshot):
        """Apply the prepared, tested migration patch and re-run the unchanged scenario."""
        from shockwave_demo.common import MIGRATIONS_PATH
        from shockwave_demo.migrations import git_apply

        migrations = json.loads(MIGRATIONS_PATH.read_text(encoding="utf-8"))["migrations"]
        migration = next((item for item in migrations if item["scenario_id"] == plan.scenario_id), None)
        if migration is None:
            raise ValueError(f"no prepared migration for {plan.scenario_id}")
        patch_path = ROOT / migration["patch"]
        work = _run_dir(self.state, snapshot.root)
        try:
            run = run_phase_locally(plan, snapshot.root, "patched", work_dir=work, node_executable=str(NODE),
                                    patch=git_apply(patch_path))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        outcome = interpret_patched_run(plan, run)
        status = {"validated": "patch_validated", "validation_failed": "validation_failed"}.get(outcome.status, "blocked")
        self.patch = patch_path.read_text(encoding="utf-8")
        self.logs = "\n".join([outcome.reason, *_proof_lines(plan, "patched", run)])[:16000]
        changed = [entry if isinstance(entry, str) else entry.get("file", str(entry))
                   for entry in migration.get("files_touched", [])]
        execution = outcome.execution or run.execution
        return MigrationResult(schema_version="shockwave.v1", id=uid(), risk_id=plan.risk_id,
                               repository_snapshot_id=request.repository_snapshot_id,
                               target_contract_snapshot_id=request.target_contract_snapshot_id, status=status,
                               generation_mode="rule", patch_artifact_id=None, changed_files=changed,
                               validation=ExecutionResult.model_validate(execution.model_dump(mode="json")),
                               pr_url=None, branch=None,
                               warnings=[("Targets the proposed contract only; deployment must be coordinated with "
                                          "the provider's change. Current-contract responses are not supported by "
                                          "this migration.")])

    def _use_site(self, risk, root: Path):
        """The use site holding a '/ 100' amount conversion, from the Risk's own source evidence.

        The Risk carries its dependencies' scanner evidence verbatim (origin first, use site last),
        so this also works after a backend restart.
        """
        sources = [item for item in risk.evidence if item.kind == "source" and item.file and item.line_start]
        for use in reversed(sources):
            lines = (root / use.file).read_text(encoding="utf-8").splitlines()
            if 1 <= use.line_start <= len(lines):
                match = _CONVERSION.search(lines[use.line_start - 1])
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


def integrated_factory(work_dir: Path | None = None, repositories: dict[str, Path] | None = None):
    """An adapters_factory for create_app(..., adapters_factory=integrated_factory())."""
    state = IntegratedState(Path(work_dir or tempfile.mkdtemp(prefix="shockwave-integrated-")))
    if repositories:
        state.repositories.update(repositories)
    factory = partial(IntegratedAdapters, state=state)
    factory.accepts_consumer_org_id = True
    factory.state = state
    factory.repository_options = repository_options
    return factory
