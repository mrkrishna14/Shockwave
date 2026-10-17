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
import logging
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from fcntl import LOCK_EX, LOCK_UN, flock

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
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_LOG = logging.getLogger(__name__)


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
    _last_orphan_sweep: float = 0

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir).resolve()
        self.repositories.update(STRIPE_REPOSITORIES)
        self.artifacts = ArtifactStore(self.work_dir / "artifacts")
        self.records: dict[str, dict] = {}  # snapshot id -> {root, commit_sha, content_hash, connection id}
        self._load_registry()
        self.runner = RunnerService(self.artifacts, self.snapshots, self.scenarios,
                                    consent_check=self.registered)

    @property
    def registry_path(self) -> Path:
        return self.work_dir / "registry.json"

    def snapshot_directory(self) -> Path:
        snapshots = self.work_dir / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        if snapshots.is_symlink() or snapshots.resolve().parent != self.work_dir:
            raise ValueError("SNAPSHOT_DIRECTORY_UNSAFE")
        return snapshots

    @contextmanager
    def _registry_lock(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with (self.work_dir / "registry.lock").open("a+b") as lock:
            flock(lock.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock.fileno(), LOCK_UN)

    def _read_registry(self) -> dict:
        try:
            records = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError) as error:
            self._quarantine_registry(str(error))
            return {}
        if not isinstance(records, dict):
            self._quarantine_registry("top-level value is not an object")
            return {}
        return records

    def _quarantine_registry(self, reason: str) -> None:
        # Preserve damaged bytes for diagnosis instead of silently erasing all records.
        try:
            damaged = self.work_dir / f"registry.invalid.{uid()}.json"
            os.replace(self.registry_path, damaged)
            _LOG.error("Damaged snapshot registry moved to %s: %s", damaged, reason)
        except OSError:
            _LOG.error("Could not read or quarantine the snapshot registry: %s", reason)

    def _validated_record(self, snapshot_id: str, record: object) -> TrustedSnapshot | None:
        try:
            if not isinstance(record, dict):
                return None
            root = Path(record["root"])
            expected = self.snapshot_directory()
            if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve().parent != expected:
                return None
            if not _COMMIT_SHA.fullmatch(record["commit_sha"]):
                return None
            if not re.fullmatch(r"[0-9a-f]{64}", record["content_hash"]):
                return None
            RepositorySnapshot(id=snapshot_id, repository_connection_id=record["repository_connection_id"],
                               commit_sha=record["commit_sha"], fetched_at=record["fetched_at"],
                               content_hash=record["content_hash"])
            trusted = TrustedSnapshot.register_team_fixture(root, record["commit_sha"])
            return trusted if trusted.content_hash == record["content_hash"] else None
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def _load_registry(self) -> None:
        """Re-register snapshots fetched before a backend restart (unchanged copies only)."""
        with self._registry_lock():
            records = self._read_registry()
            valid = {}
            for snapshot_id, record in records.items():
                trusted = self._validated_record(snapshot_id, record)
                if trusted is None:
                    _LOG.warning("Ignoring invalid or changed snapshot record %s", snapshot_id)
                    continue
                valid[snapshot_id] = record
                self.snapshots[snapshot_id] = trusted
                self.records[snapshot_id] = record
            for snapshot_id in set(self.records) - set(valid):
                self.records.pop(snapshot_id, None)
                self.snapshots.pop(snapshot_id, None)
            if len(valid) != len(records):
                self._quarantine_registry("one or more invalid snapshot records")
                self._write_registry(valid)

    def _write_registry(self, records: dict) -> None:
        fd, name = tempfile.mkstemp(prefix="registry.", suffix=".tmp", dir=self.work_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(records, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.registry_path)
            directory = os.open(self.work_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def remember(self, snapshot, trusted: TrustedSnapshot) -> None:
        record = {"root": str(trusted.root), "commit_sha": trusted.commit_sha,
                  "content_hash": trusted.content_hash,
                  "repository_connection_id": str(snapshot.repository_connection_id),
                  "fetched_at": snapshot.fetched_at if isinstance(snapshot.fetched_at, str)
                  else snapshot.fetched_at.isoformat()}
        with self._registry_lock():
            records = self._read_registry()
            records[snapshot.id] = record
            self._write_registry(records)
            self.records[snapshot.id] = record
            self.snapshots[snapshot.id] = trusted

    def snapshot(self, snapshot_id: str) -> TrustedSnapshot:
        if snapshot_id not in self.records:
            self._load_registry()  # another worker may have fetched it after this process started
        record = self.records[snapshot_id]
        trusted = self._validated_record(snapshot_id, record)
        if trusted is None:
            self.snapshots.pop(snapshot_id, None)
            self.records.pop(snapshot_id, None)
            raise ValueError("SNAPSHOT_UNAVAILABLE")
        self.snapshots[snapshot_id] = trusted
        return trusted

    def purge_snapshot(self, snapshot_id: str) -> None:
        """Remove one private copy after consent revocation; never follow registry paths outside work_dir."""
        with self._registry_lock():
            records = self._read_registry()
            record = records.pop(snapshot_id, None)
            allowed = self.snapshot_directory()
            root = allowed / snapshot_id
            if isinstance(record, dict):
                candidate = Path(record.get("root", ""))
                legacy = allowed / f"{record.get('repository_connection_id', '')}-{record.get('commit_sha', '')}"
                if candidate in (root, legacy) and candidate.parent == allowed:
                    root = candidate
            if root.is_symlink() and root.parent == allowed:
                root.unlink()
            elif root.is_dir() and root.resolve().parent == allowed:
                shutil.rmtree(root)
            if record is not None:
                self._write_registry(records)
            self.records.pop(snapshot_id, None)
            self.snapshots.pop(snapshot_id, None)

    def reconcile_cleanup(self, store) -> int:
        """Finish committed revocations after a process crash or in another worker."""
        with store.read() as db:
            pending = [row["snapshot_id"] for row in db.execute("SELECT snapshot_id FROM snapshot_cleanup")]
        completed = 0
        for snapshot_id in pending:
            try:
                self.purge_snapshot(snapshot_id)
            except (OSError, ValueError) as error:
                _LOG.error("Could not purge snapshot %s: %s", snapshot_id, error)
                continue
            with store.tx() as db:
                db.execute("DELETE FROM snapshot_cleanup WHERE snapshot_id=?", (snapshot_id,))
            completed += 1
        self.prune_orphans(store)
        return completed

    def prune_orphans(self, store, *, grace_seconds: int = 86400) -> int:
        """Delete old copies with no SQLite snapshot, while leaving in-flight scans alone."""
        if time.monotonic()-self._last_orphan_sweep < 3600:
            return 0
        self._last_orphan_sweep = time.monotonic()
        with store.read() as db:
            active_scan=db.execute("""SELECT 1 FROM records WHERE kind='job'
                AND json_extract(payload,'$.kind')='scan'
                AND json_extract(payload,'$.status') IN ('queued','running') LIMIT 1""").fetchone()
            if active_scan:
                return 0
            persisted=[json.loads(row['payload']) for row in db.execute("SELECT payload FROM records WHERE kind='snapshot'")]
        if not (self.work_dir / "snapshots").is_dir():
            return 0
        allowed=self.snapshot_directory()
        persisted_ids={snapshot['id'] for snapshot in persisted}
        known={allowed / snapshot_id for snapshot_id in persisted_ids}
        known.update(allowed / f"{snapshot['repository_connection_id']}-{snapshot['commit_sha']}"
                     for snapshot in persisted)
        with self._registry_lock():
            records=self._read_registry()
            removed=0
            for path in allowed.iterdir():
                if path in known or path.is_symlink() or not path.is_dir():
                    continue
                if time.time()-path.stat().st_mtime < grace_seconds:
                    continue
                shutil.rmtree(path)
                removed+=1
            stale_ids=[snapshot_id for snapshot_id,record in records.items()
                       if snapshot_id not in persisted_ids and
                       (not isinstance(record,dict) or
                        not isinstance(record.get('root'),str) or
                        not Path(record['root']).is_dir())]
            for snapshot_id in stale_ids:
                records.pop(snapshot_id,None)
                self.records.pop(snapshot_id,None)
                self.snapshots.pop(snapshot_id,None)
            if stale_ids:
                self._write_registry(records)
            return removed

    def snapshot_record(self, snapshot_id: str) -> RepositorySnapshot:
        self.snapshot(snapshot_id)
        record = self.records[snapshot_id]
        return RepositorySnapshot(id=snapshot_id, repository_connection_id=record["repository_connection_id"],
                                  commit_sha=record["commit_sha"], fetched_at=record["fetched_at"],
                                  content_hash=record["content_hash"])

    def registered(self, snapshot_id: str) -> bool:
        # The orchestrator re-checks enrollment and repository consent before and after
        # each job; the runner refuses anything that was not registered by fetch_repository.
        try:
            self.snapshot(snapshot_id)
            return True
        except (KeyError, ValueError):
            return False

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
            raise ValueError("NODE_DEPENDENCIES_MISSING")
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
        if not isinstance(commit_sha, str) or not _COMMIT_SHA.fullmatch(commit_sha):
            raise ValueError("INVALID_SNAPSHOT_REF")
        source = self.state.repositories.get(connection.full_name)
        if source is None:
            raise ValueError(f"no local team repository registered for {connection.full_name}")
        snapshots = self.state.snapshot_directory()
        snapshot_id = uid()
        root = snapshots / snapshot_id
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=snapshots))
        try:
            copied = staging / "source"
            shutil.copytree(source, copied, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "node_modules"))
            # Registration hashes the copy and rejects symlinks, including links outside the source.
            TrustedSnapshot.register_team_fixture(copied, commit_sha)
            os.rename(copied, root)
            trusted = TrustedSnapshot.register_team_fixture(root, commit_sha)
            snapshot = RepositorySnapshot(id=snapshot_id, repository_connection_id=connection.id,
                                          commit_sha=commit_sha, fetched_at=now(),
                                          content_hash=trusted.content_hash)
            self.state.remember(snapshot, trusted)
            return snapshot
        except BaseException:
            if root.exists():
                shutil.rmtree(root)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def scan_repository(self, request, snapshot, repo_root):
        trusted = self.state.snapshot(snapshot.id)
        return noirit_scan_repository(request, snapshot, trusted.root, consumer_org_id=self.consumer_org_id)

    def discard_snapshot(self, snapshot_id):
        """Drop a fetched copy when its scan loses the job lease or consent fence."""
        self.state.purge_snapshot(snapshot_id)

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
        snapshot = self.state.snapshot(plan.repository_snapshot_id)
        if plan.scenario_id == NOT_EXECUTABLE:
            risk = self.state.risks[plan.risk_id]
            reason = self.state.unrunnable.get(plan.risk_id, "no executable scenario")
            self.logs = f"Not run: {reason}"
            return not_run_result(risk, f"No executable scenario for this usage, so it remains a prediction ({reason}).")
        if get_scenario(plan.scenario_id).service_id == STRIPE:
            return self._verify_locally(plan)
        result = self.state.runner.verify_regression(plan, snapshot.root)
        self.logs = self._bounded_logs(result.artifact_ids)
        return result

    def _verify_locally(self, plan):
        """Stripe case scenarios: current and proposed runs of the real consumer code (runner double)."""
        root = self.state.snapshot(plan.repository_snapshot_id).root
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
        snapshot = self.state.snapshot(request.repository_snapshot_id)
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
    factory.reconcile_cleanup = state.reconcile_cleanup
    factory.prerequisites = lambda: (["Stripe checkout tests need `npm ci --ignore-scripts --prefix demo/templates/checkout`."]
                                     if not (NODE_MODULES / "stripe" / "package.json").is_file() else [])
    return factory
