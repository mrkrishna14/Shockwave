"""End-to-end demo of Kanishk's slice: a labeled report plus deterministic JSON samples.

    python -m analyzer.demo [--execute-local] [--write-samples DIR] [--node PATH]

Run from the repository root. Every stage calls the contract functions on checked-in
fixtures: ``discover_endpoints`` (hand-maintained Demo Payments specs plus a literal
FastAPI scan of examples/provider/demo_payments/service), ``detect_changes`` (once on the
schemas alone, once with the declared cents-to-dollars change), ``analyze_impact``
(against contract doubles of Noirit's scanner output, matched through the v1 Endpoint
records they were keyed to) and ``build_verification_plan``.
The pinned Stripe document is discovered on its own and only counted: public contract
discovery, not Stripe production source, and never merged with the fictional service.

The output keeps predicted, verified and not-run states apart. Every risk is a
prediction. Verification runs only with ``--execute-local``, on the development runner
double (trusted local fixtures only, not a sandbox and not Sai's runner), and is labeled
that way, inside each VerificationResult.summary as well; without the flag the display
risks keep their plans and stay predicted/not_run. Storage and comparison risks have no
executable scenario, so they are not_run either way. The patched run applies a simulated
targeted edit (it removes the divide-by-100 at the dependency's evidence line) only to
show that the unchanged $10.00 expectation passes; that edit is pinned to the proposed
contract, so a real deployment must be coordinated with the provider's cutover. Real
patches are Sai's migration adapter's job.

Samples are reproducible byte for byte: ids are content-derived, keys are sorted, and
there are no timestamps or absolute paths. Measured durations are normalized to 0 before
interpretation (the local-double wrapper says so), because they vary between runs and
would otherwise change the content-derived VerificationResult ids.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from analyzer.changes import detect_changes
from analyzer.fieldpaths import is_api_field_path
from analyzer.impact import analyze_impact, risk_category, validate_risk_evidence
from analyzer.rules import describe_usage, source_location
from discovery import discover, discover_endpoints
from discovery.contracts import (
    CONTRACTS_SOURCE,
    AnalysisResult,
    APIChange,
    Dependency,
    DiscoveryInput,
    DiscoveryResult,
    Endpoint,
    RepositorySnapshot,
    Risk,
    VerificationPlan,
    VerificationResult,
)
from discovery.ids import EndpointIdentity, endpoint_identities, same_endpoint
from discovery.inventory import summarize_inventory
from discovery.pinned import STRIPE_PIN_PATH, load_pinned_document
from discovery.records import FetchedDocument
from discovery.text import clip_text
from verification.scenarios.catalog import bound_scenario_ids
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import (
    PatchedOutcome,
    PhaseRun,
    classify_phase,
    interpret_patched_run,
    interpret_verification,
)
from verification.scenarios.plans import (
    NoExecutableScenario,
    PlanConfig,
    build_verification_plan,
    not_run_result,
)

__all__ = [
    "LOCAL_DOUBLE_SAMPLE",
    "SAMPLE_FILES",
    "DemoRun",
    "PatchNotApplicable",
    "SimulatedPatch",
    "main",
    "render_report",
    "render_sample",
    "run_demo",
    "sample_documents",
    "write_samples",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "examples" / "provider" / "demo_payments"
DEMO_SOURCE_ROOT = DEMO_DIR / "service"
DEMO_SPECS = {
    "v1": DEMO_DIR / "openapi" / "demo-payments.v1.yaml",
    "v2_proposed": DEMO_DIR / "openapi" / "demo-payments.v2.yaml",
}
DECLARED_CHANGE_PATH = DEMO_DIR / "changes" / "charge_amount_cents_to_dollars.json"
PROVIDER_IDS_PATH = DEMO_DIR / "fixture_ids.json"
CONSUMER_FIXTURES = REPO_ROOT / "tests" / "analyzer" / "fixtures"
DEPENDENCIES_PATH = CONSUMER_FIXTURES / "demo_dependencies.json"
CONSUMER_IDS_PATH = CONSUMER_FIXTURES / "consumer_fixture_ids.json"
CONSUMER_REPOSITORIES = CONSUMER_FIXTURES / "consumer_doubles"
STRIPE_DOCUMENT_REF = "stripe/openapi:latest/openapi.spec3.json"

STRIPE_LABEL = "public contract discovery; not Stripe production source"
LOCAL_DOUBLE_LABEL = "LOCAL RUNNER DOUBLE - trusted local fixtures, not a sandbox, not Sai's runner"
LOCAL_DOUBLE_RUNNER = (
    "dev_runner_double (local, trusted fixtures only; plain subprocess, not a sandbox; not Sai's runner)"
)
# Prefixed to every VerificationResult.summary the double produces, so the label travels with
# the record itself (the summary is not part of the content-derived result id).
LOCAL_DOUBLE_RESULT_PREFIX = (
    "LOCAL RUNNER DOUBLE (trusted local fixtures; plain subprocess, not a sandbox; not Sai's "
    "runner; demo run, duration_ms normalized to 0):"
)
TIMING_NOTE = (
    "duration_ms is normalized to 0 before interpretation: measured durations vary between "
    "runs, interpretation never reads them, and result ids derive from the normalized evidence"
)
PATCH_NOTE = (
    "simulated targeted edit for the local double only: removes the divide-by-100 conversion at "
    "the dependency's evidence line and keeps the $10.00 expectation; real patches come from "
    "Sai's migration adapter. The edit is pinned to the proposed contract: it is not backward "
    "compatible with current-contract (cents) responses, which it would display 100x too large, "
    "so its deployment must be coordinated with the provider's cutover"
)
ANALYSIS_MODE_LABELS = {
    "rules": "deterministic rules, not model inference",
    "model": "live model inference",
    "fixture": "replayed fixture reply, not live inference",
}
SAMPLE_FILES = (
    "stripe_inventory_summary.json",
    "demo_payments_discovery_v1.json",
    "demo_payments_changes.json",
    "demo_payments_analysis.json",
    "demo_payments_verification_plans.json",
)
LOCAL_DOUBLE_SAMPLE = "demo_payments_verification_results.local_double.json"

# The consumer doubles are local fixtures that Sai's adapter never fetched, so there is no
# real fetch time. A fixed value keeps outputs reproducible; no plan or sample contains it.
FIXTURE_FETCHED_AT = datetime(2026, 10, 16, tzinfo=UTC)
_CACHE_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache"})
_WRAP_WIDTH = 100


class PatchNotApplicable(ValueError):
    """The simulated edit did not find exactly one conversion to remove."""


@dataclass(frozen=True)
class SimulatedPatch:
    """Remove one ``/ <operand>`` conversion within a dependency's evidence lines."""

    file: str  # consumer-repository-relative POSIX path (from the dependency's evidence)
    line_start: int
    line_end: int
    operand: str

    @property
    def location(self) -> str:
        if self.line_end != self.line_start:
            return f"{self.file}:{self.line_start}-{self.line_end}"
        return f"{self.file}:{self.line_start}"

    @property
    def description(self) -> str:
        return f"{self.location}: remove the '/ {self.operand}' conversion"

    def apply(self, checkout: Path) -> None:
        """Edit the copy at ``checkout``; refuses unless exactly one conversion matches."""
        path = checkout / self.file
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        if not 1 <= self.line_start <= self.line_end <= len(lines):
            raise PatchNotApplicable(f"{self.location} is outside {self.file}")
        region = "".join(lines[self.line_start - 1 : self.line_end])
        patched, count = _division_pattern(self.operand).subn("", region)
        if count != 1:
            raise PatchNotApplicable(
                f"expected exactly one '/ {self.operand}' at {self.location}, found {count}"
            )
        lines[self.line_start - 1 : self.line_end] = [patched]
        path.write_bytes("".join(lines).encode("utf-8"))


@dataclass(frozen=True)
class ConsumerRepository:
    handle: str  # key in consumer_fixture_ids.json and the consumer_doubles directory name
    root: Path
    snapshot: RepositorySnapshot


@dataclass(frozen=True)
class StripeInventory:
    summary: dict
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class LocalVerification:
    """One display plan executed on the runner double; runs are timing-normalized."""

    plan: VerificationPlan
    repository: str
    baseline: PhaseRun
    proposed: PhaseRun
    result: VerificationResult
    patch: SimulatedPatch | None
    patched: PatchedOutcome | None
    patched_note: str | None  # why there is no patched outcome
    measured_ms: dict[str, int]  # real durations, shown in the report only


@dataclass(frozen=True)
class DemoRun:
    stripe: StripeInventory
    discovery_v1: DiscoveryResult
    discovery_v2: DiscoveryResult
    schema_only_changes: list[APIChange]
    changes: list[APIChange]
    change: APIChange  # the declared change as published by detect_changes
    dependencies: dict[str, Dependency]  # fixture handle -> dependency
    repositories: dict[str, ConsumerRepository]  # repository snapshot id -> repository
    analysis: AnalysisResult
    evidence_problems: list[str]
    plans: list[VerificationPlan]
    not_run: list[VerificationResult]
    local: list[LocalVerification] | None  # None: nothing was executed


# Pipeline ------------------------------------------------------------------------------


def run_demo(
    *,
    execute_local: bool = False,
    node_executable: str | None = None,
    work_dir: Path | None = None,
) -> DemoRun:
    """Run every stage on the checked-in fixtures; nothing executes unless ``execute_local``."""
    provider_ids = _read_json(PROVIDER_IDS_PATH)
    stripe = discover_stripe(provider_ids)
    v1 = discover_demo(provider_ids, "v1")
    v2 = discover_demo(provider_ids, "v2_proposed")
    declared = APIChange.model_validate(_read_json(DECLARED_CHANGE_PATH))
    project_id = provider_ids["api_project_id"]
    schema_only = detect_changes(v1, v2, [], api_project_id=project_id)
    changes = detect_changes(v1, v2, [declared], api_project_id=project_id)
    change = _published(changes, declared)
    dependencies = load_dependencies()
    endpoints = analysis_endpoints(v1)
    analysis = analyze_impact(change, list(dependencies.values()), endpoints=endpoints)
    problems = validate_risk_evidence(analysis, change, list(dependencies.values()), endpoints=endpoints)
    repositories = consumer_repositories(_read_json(CONSUMER_IDS_PATH))
    plans, not_run = plan_verifications(analysis, repositories, trusted_fixture_config(repositories))
    local = None
    if execute_local:
        context = _ExecutionContext(change, analysis, dependencies, repositories, node_executable)
        local = execute_plans_locally(plans, context, work_dir)
    return DemoRun(
        stripe=stripe,
        discovery_v1=v1,
        discovery_v2=v2,
        schema_only_changes=schema_only,
        changes=changes,
        change=change,
        dependencies=dependencies,
        repositories=repositories,
        analysis=analysis,
        evidence_problems=problems,
        plans=plans,
        not_run=not_run,
        local=local,
    )


def discover_stripe(provider_ids: Mapping) -> StripeInventory:
    """Discover the pinned public Stripe document (no source) and summarize what it holds."""
    stripe = provider_ids["stripe"]
    request = DiscoveryInput(
        service_id=stripe["service_id"],
        snapshot_id=stripe["contract_snapshot_id"],
        openapi_documents=[STRIPE_DOCUMENT_REF],
        source_snapshot_id=None,  # public contract only; no provider source is scanned
        frameworks=[],
    )
    outcome = discover(request, {STRIPE_DOCUMENT_REF: load_pinned_document(STRIPE_PIN_PATH)}, None)
    return StripeInventory(summarize_inventory(outcome), tuple(outcome.result.warnings))


def discover_demo(provider_ids: Mapping, contract: str) -> DiscoveryResult:
    """One Demo Payments contract snapshot: its hand-maintained spec plus the FastAPI scan."""
    spec = DEMO_SPECS[contract]
    ref = _repo_relative(spec)
    request = DiscoveryInput(
        service_id=provider_ids["service_id"],
        snapshot_id=provider_ids["contract_snapshots"][contract],
        openapi_documents=[ref],
        source_snapshot_id=provider_ids["source_snapshot_id"],
        frameworks=["fastapi"],
    )
    fetched = {ref: FetchedDocument(content=spec.read_bytes(), source_uri=ref)}
    return discover_endpoints(request, fetched, DEMO_SOURCE_ROOT)


def analysis_endpoints(v1: DiscoveryResult) -> list[Endpoint]:
    """Endpoint records ``analyze_impact`` matches through: the change's old snapshot (v1),
    which is also the catalog the consumer-scan doubles were keyed to."""
    return list(v1.endpoints)


def load_dependencies() -> dict[str, Dependency]:
    """Contract doubles of Noirit's scanner output, keyed by their fixture handle."""
    documents = _read_json(DEPENDENCIES_PATH)["dependencies"]
    return {handle: Dependency.model_validate(document) for handle, document in documents.items()}


def consumer_repositories(consumer_ids: Mapping) -> dict[str, ConsumerRepository]:
    """Consumer doubles as pinned snapshots, keyed by repository snapshot id."""
    repositories: dict[str, ConsumerRepository] = {}
    for handle, ids in consumer_ids["repositories"].items():
        root = CONSUMER_REPOSITORIES / handle
        snapshot = RepositorySnapshot(
            id=ids["repository_snapshot_id"],
            repository_connection_id=ids["repository_connection_id"],
            commit_sha=ids["commit_sha"],
            fetched_at=FIXTURE_FETCHED_AT,
            content_hash=tree_sha256(root),
        )
        repositories[str(snapshot.id)] = ConsumerRepository(handle, root, snapshot)
    return repositories


def trusted_fixture_config(repositories: Mapping[str, ConsumerRepository]) -> PlanConfig:
    """Only the team-written consumer doubles are trusted local fixtures."""
    connections = {str(item.snapshot.repository_connection_id) for item in repositories.values()}
    return PlanConfig(frozenset(connections))


def plan_verifications(
    analysis: AnalysisResult,
    repositories: Mapping[str, ConsumerRepository],
    config: PlanConfig,
) -> tuple[list[VerificationPlan], list[VerificationResult]]:
    """A plan per risk with exactly one executable scenario; a not_run result for every other risk.

    A risk naming two bound scenarios (``AmbiguousScenario``, a ``NoExecutableScenario``) is
    not_run too, instead of aborting the plans of every other risk.
    """
    plans: list[VerificationPlan] = []
    not_run: list[VerificationResult] = []
    for risk in analysis.risks:
        repository = repositories.get(str(risk.repository_snapshot_id))
        if repository is None:
            raise ValueError(f"risk {risk.id} names an unknown repository snapshot")
        try:
            plans.append(build_verification_plan(risk, repository.snapshot, config=config))
        except NoExecutableScenario as missing:  # includes AmbiguousScenario
            not_run.append(not_run_result(risk, f"Not run: {missing}"))
    return plans, not_run


@dataclass(frozen=True)
class _ExecutionContext:
    change: APIChange
    analysis: AnalysisResult
    dependencies: Mapping[str, Dependency]
    repositories: Mapping[str, ConsumerRepository]
    node_executable: str | None


def execute_plans_locally(
    plans: Sequence[VerificationPlan], context: _ExecutionContext, work_dir: Path | None
) -> list[LocalVerification]:
    """Run each plan on the runner double inside ``work_dir`` (a temporary one by default)."""
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="shockwave-demo-") as temporary:
            return [verify_locally(plan, context, Path(temporary)) for plan in plans]
    return [verify_locally(plan, context, Path(work_dir)) for plan in plans]


def verify_locally(
    plan: VerificationPlan, context: _ExecutionContext, work_root: Path
) -> LocalVerification:
    """Baseline and proposed phases, their verdict, then the simulated patch on the proposed fixture."""
    repository = context.repositories[str(plan.repository_snapshot_id)]
    risk = next(risk for risk in context.analysis.risks if str(risk.id) == str(plan.risk_id))
    dependency = bound_dependency(risk, context.dependencies, plan.scenario_id)
    patch = conversion_removal_patch(dependency) if dependency is not None else None
    work_dir = work_root / plan.scenario_id

    def run(phase: str, patch_checkout: SimulatedPatch | None = None) -> PhaseRun:
        return run_phase_locally(
            plan,
            repository.root,
            phase,
            work_dir=work_dir,
            node_executable=context.node_executable,
            patch=None if patch_checkout is None else patch_checkout.apply,
        )

    baseline, proposed = run("baseline"), run("proposed")
    measured = {"baseline": baseline.execution.duration_ms, "proposed": proposed.execution.duration_ms}
    patched: PatchedOutcome | None = None
    note: str | None = "no simulated patch exists for this usage"
    if patch is not None:
        try:
            patched_run = run("patched", patch)
        except PatchNotApplicable as error:
            note = f"patched run not executed: {error}"
        else:
            measured["patched"] = patched_run.execution.duration_ms
            patched = interpret_patched_run(plan, without_timing(patched_run))
            note = None
    baseline, proposed = without_timing(baseline), without_timing(proposed)
    return LocalVerification(
        plan=plan,
        repository=repository.handle,
        baseline=baseline,
        proposed=proposed,
        result=labeled_local_result(interpret_verification(plan, baseline, proposed)),
        patch=patch,
        patched=patched,
        patched_note=note,
        measured_ms=measured,
    )


def labeled_local_result(result: VerificationResult) -> VerificationResult:
    """The double's result with its provenance inside the record (ids are unchanged)."""
    return result.model_copy(update={"summary": f"{LOCAL_DOUBLE_RESULT_PREFIX} {result.summary}"})


def bound_dependency(
    risk: Risk, dependencies: Mapping[str, Dependency], scenario_id: str
) -> Dependency | None:
    """The risk's dependency that the catalog pointer for ``scenario_id`` was minted for."""
    by_id = {str(dependency.id): dependency for dependency in dependencies.values()}
    for dependency_id in risk.dependency_ids:
        if scenario_id in bound_scenario_ids(risk.evidence, [dependency_id]):
            return by_id.get(str(dependency_id))
    return None


def conversion_removal_patch(dependency: Dependency) -> SimulatedPatch | None:
    """The simulated targeted edit for a divide usage: drop the division at its evidence lines."""
    operand = _operand_text(dependency.usage.operand)
    if dependency.usage.operation != "divide" or operand is None:
        return None
    for item in dependency.evidence:
        if item.kind == "source" and item.file and item.line_start is not None:
            return SimulatedPatch(item.file, item.line_start, item.line_end or item.line_start, operand)
    return None


def without_timing(run: PhaseRun) -> PhaseRun:
    """The same evidence with ``duration_ms`` = 0 (interpretation never reads timing)."""
    return PhaseRun(execution=run.execution.model_copy(update={"duration_ms": 0}), report=run.report)


def tree_sha256(root: Path) -> str:
    """Content hash of a local fixture tree: sorted relative POSIX paths and file bytes."""
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not _CACHE_DIRECTORIES.intersection(path.relative_to(root).parts)
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return f"sha256:{digest.hexdigest()}"


def default_node() -> str | None:
    """The ``node`` next to this Python (the venv's Node >= 22.18), else None for a PATH lookup."""
    candidate = Path(sys.executable).parent / "node"
    return str(candidate) if candidate.exists() else None


def _published(changes: Sequence[APIChange], declared: APIChange) -> APIChange:
    for change in changes:
        if str(change.id) == str(declared.id):
            return change
    raise ValueError(f"detect_changes did not publish the declared change {declared.id}")


def _division_pattern(operand: str) -> re.Pattern[str]:
    # " / 100", but never "// 100", "/= 100", "/* ...", "/ 1000" or "/ 100.5".
    return re.compile(r"\s*(?<![/*])/(?![/*=])\s*" + re.escape(operand) + r"(?![\w.])")


def _operand_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return str(int(number)) if number.is_integer() else repr(number)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _repo_relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# Samples -------------------------------------------------------------------------------


def sample_documents(run: DemoRun) -> dict[str, object]:
    """File name -> JSON document; the local-double file only exists when execution ran."""
    documents: dict[str, object] = {
        "stripe_inventory_summary.json": run.stripe.summary,
        "demo_payments_discovery_v1.json": _dump(run.discovery_v1),
        "demo_payments_changes.json": {
            "schema_diff_only": [_dump(change) for change in run.schema_only_changes],
            "with_declared_change": [_dump(change) for change in run.changes],
        },
        "demo_payments_analysis.json": _dump(run.analysis),
        "demo_payments_verification_plans.json": {
            "plans": [_dump(plan) for plan in run.plans],
            "not_run": [_dump(result) for result in run.not_run],
        },
    }
    if run.local is not None:
        documents[LOCAL_DOUBLE_SAMPLE] = {
            "runner": LOCAL_DOUBLE_RUNNER,
            "timing": TIMING_NOTE,
            "patch_source": PATCH_NOTE,
            "results": [_dump(item.result) for item in run.local],
            "patched": [_patched_document(item) for item in run.local],
        }
    return documents


def render_sample(document: object) -> str:
    """Deterministic JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write_samples(run: DemoRun, directory: Path) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, document in sample_documents(run).items():
        path = directory / name
        path.write_bytes(render_sample(document).encode("utf-8"))
        written.append(path)
    return written


def _dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


def _patched_document(item: LocalVerification) -> dict:
    outcome = item.patched
    patch = item.patch
    return {
        "plan_id": str(item.plan.id),
        "risk_id": str(item.plan.risk_id),
        "repository_snapshot_id": str(item.plan.repository_snapshot_id),
        "scenario_id": item.plan.scenario_id,
        "patch": None
        if patch is None
        else {
            "file": patch.file,
            "line_start": patch.line_start,
            "line_end": patch.line_end,
            "removed": f"/ {patch.operand}",
        },
        "status": "not_run" if outcome is None else outcome.status,
        "reason": item.patched_note if outcome is None else outcome.reason,
        "execution": None if outcome is None or outcome.execution is None else _dump(outcome.execution),
    }


# Report --------------------------------------------------------------------------------


def render_report(run: DemoRun) -> str:
    """The human-readable report; every state is labeled predicted, verified or not run."""
    sections = (
        _header(run),
        _stripe_section(run.stripe),
        _discovery_section(run),
        _changes_section(run),
        _impact_section(run),
        _plans_section(run),
        _verification_section(run),
    )
    return "\n".join(line for section in sections for line in section) + "\n"


def _header(run: DemoRun) -> list[str]:
    verification = "LOCAL RUNNER DOUBLE" if run.local is not None else "not executed"
    return [
        "Shockwave demo - know what breaks before you ship",
        (
            f"contracts: {CONTRACTS_SOURCE} | analysis: {run.analysis.analysis_mode} | "
            f"verification: {verification}"
        ),
        "",
    ]


def _stripe_section(stripe: StripeInventory) -> list[str]:
    summary = stripe.summary
    lines = [f"[1] Stripe - {STRIPE_LABEL}"]
    for document in summary["documents"]:
        lines += [
            (
                f"    document    {document['revision_label'] or document['source_uri']} "
                f"(OpenAPI {document['openapi_version']}, info.version {document['info_version']}, "
                f"{document['visibility']})"
            ),
            f"    sha256      {document['sha256']} ({document['byte_size']} bytes)",
            (
                f"    computed    {_count(document['operation_count'], 'operation')} over "
                f"{_count(document['path_count'], 'path')}, "
                f"{_count(document['component_schema_count'], 'component schema')}"
            ),
        ]
    methods = ", ".join(f"{method} {count}" for method, count in summary["by_method"].items())
    statuses = ", ".join(f"{name} {count}" for name, count in summary["by_reconciliation"].items() if count)
    hanging = " " * 16
    lines.append(f"    endpoints   {summary['endpoint_count']} ({methods})")
    lines.append(
        _wrap(
            f"reconciled  {statuses or 'none'}: no Stripe server source is scanned (Stripe SDKs are "
            "clients and stripe-mock is a mock; neither is provider source)",
            "    ",
            hanging,
        )
    )
    lines += [_wrap(f"warning     {warning}", "    ", hanging) for warning in stripe.warnings]
    return [*lines, ""]


def _discovery_section(run: DemoRun) -> list[str]:
    v1, v2 = run.discovery_v1, run.discovery_v2
    coverage = v1.coverage
    lines = [
        f"[2] Demo Payments (fictional service {v1.service_id}; not Stripe) - discover_endpoints",
        f"    v1 snapshot {v1.snapshot_id}",
        f"      spec    {_repo_relative(DEMO_SPECS['v1'])} (hand-maintained)",
        f"      source  {_repo_relative(DEMO_SOURCE_ROOT)} (literal FastAPI scan; never imported)",
    ]
    rows = [("METHOD", "PATH", "RECONCILIATION", "EVIDENCE")]
    rows += [(e.method, e.path, e.reconciliation, _endpoint_evidence(e)) for e in v1.endpoints]
    lines += _table(rows, "    ")
    lines.append(
        _wrap(
            f"coverage: {coverage.files_scanned} files scanned, {coverage.files_skipped} skipped; "
            f"unsupported: {'; '.join(coverage.unsupported_patterns) or 'none'}",
            "    ",
            "      ",
        )
    )
    lines += [_wrap(f"warning: {warning}", "    ", "      ") for warning in v1.warnings]
    same = _reconciliation(v1) == _reconciliation(v2)
    lines.append(
        f"    v2 (proposed) snapshot {v2.snapshot_id}: "
        + ("same reconciliation" if same else "reconciliation differs from v1")
        + f" for {_count(len(v2.endpoints), 'endpoint')}"
    )
    if not same:
        rows = [(e.method, e.path, e.reconciliation, _endpoint_evidence(e)) for e in v2.endpoints]
        lines += _table(rows, "      ")
    return [*lines, ""]


def _changes_section(run: DemoRun) -> list[str]:
    endpoints = {str(endpoint.id): endpoint for endpoint in run.discovery_v1.endpoints}
    lines = [
        "[3] Changes v1 -> v2 - detect_changes",
        f"    schema diff alone: {_count(len(run.schema_only_changes), 'change')}",
    ]
    lines += [
        f"      {_change_line(change)} at {_change_target(change, endpoints)}"
        for change in run.schema_only_changes
    ]
    for change in run.changes:
        lines.append(f"    with declared evidence: {_change_line(change)}")
        lines.append(f"      at:  {_change_target(change, endpoints)}")
        lines.append(_wrap(f"old: {change.old_semantics}", "      ", "           "))
        lines.append(_wrap(f"new: {change.new_semantics}", "      ", "           "))
        kinds: dict[str, int] = {}
        for item in change.evidence:
            kinds[item.kind] = kinds.get(item.kind, 0) + 1
        pointers = sorted(
            {item.json_pointer for item in change.evidence if item.kind == "openapi" and item.json_pointer}
        )
        listed = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        operation = f" (operation {', '.join(pointers)})" if pointers else ""
        lines.append(_wrap(f"evidence: {listed}{operation}", "      ", "           "))
    return [*lines, ""]


def _impact_section(run: DemoRun) -> list[str]:
    analysis, change = run.analysis, run.change
    by_id = {str(dependency.id): dependency for dependency in run.dependencies.values()}
    mode = ANALYSIS_MODE_LABELS.get(analysis.analysis_mode, analysis.analysis_mode)
    lines = [
        f"[4] Impact - analyze_impact (analysis_mode={analysis.analysis_mode}: {mode})",
        _wrap(
            f"inputs: {_count(len(by_id), 'dependency', 'dependencies')} - contract doubles of "
            f"Noirit's scanner output ({_repo_relative(DEPENDENCIES_PATH)})",
            "    ",
            "      ",
        ),
    ]
    identities = endpoint_identities(analysis_endpoints(run.discovery_v1))
    for risk in analysis.risks:
        locations = ", ".join(_where(by_id[str(item)]) for item in risk.dependency_ids if str(item) in by_id)
        scenarios = bound_scenario_ids(risk.evidence, risk.dependency_ids)
        scenario = (
            f"{', '.join(scenarios)} (executable; not run by analysis)"
            if scenarios
            else "none (no executable proof)"
        )
        lines += [
            (
                f"    PREDICTED  risk {_short(risk.id)}  {risk_category(change, risk) or 'unknown'} "
                f"{risk.severity}/{risk.confidence}  {_repository(run, risk.repository_snapshot_id)} "
                f"{locations}"
            ),
            _wrap(f"scenario: {scenario}", "      ", "        "),
            _wrap(risk.explanation, "      ", "      "),
            _wrap(f"suggested action: {risk.suggested_action}", "      ", "        "),
        ]
    referenced = {str(item) for risk in analysis.risks for item in risk.dependency_ids}
    unresolved = [by_id[str(item)] for item in analysis.unresolved_dependency_ids if str(item) in by_id]
    for dependency in unresolved:
        lines.append(
            _wrap(
                f"UNRESOLVED (unknown, not safe)  {_repository(run, dependency.repository_snapshot_id)} "
                f"{_where(dependency)} ({describe_usage(dependency)} of {dependency.field_path})",
                "    ",
                "      ",
            )
        )
    referenced |= {str(item.id) for item in unresolved}
    for dependency in by_id.values():
        if str(dependency.id) not in referenced:
            reason = _unmatched_reason(change, dependency, identities)
            lines.append(f"    not matched  {_where(dependency)}: {reason}")
    lines += [_wrap(f"warning: {warning}", "    ", "      ") for warning in analysis.warnings]
    lines.append(
        f"    evidence references (validate_risk_evidence): {_count(len(run.evidence_problems), 'problem')}"
    )
    lines += [f"      problem: {problem}" for problem in run.evidence_problems]
    return [*lines, ""]


def _plans_section(run: DemoRun) -> list[str]:
    risks = {str(risk.id): risk for risk in run.analysis.risks}
    lines = [
        "[5] Verification plans - build_verification_plan",
        "    trusted local fixtures (PlanConfig): the two storefront consumer-double connections",
    ]
    for plan in run.plans:
        risk = risks[str(plan.risk_id)]
        lines += [
            (
                f"    plan {_short(plan.id)}  risk {_short(plan.risk_id)} "
                f"({_repository(run, plan.repository_snapshot_id)}, {risk_category(run.change, risk)})"
            ),
            f"      scenario  {plan.scenario_id} ({plan.runner_profile}, timeout {plan.timeout_seconds}s)",
            f"      fixtures  {plan.old_fixture_id} -> {plan.new_fixture_id}",
        ]
    for result in run.not_run:
        risk = risks[str(result.risk_id)]
        lines.append(
            _wrap(
                f"NOT_RUN  risk {_short(result.risk_id)} ({_repository(run, result.repository_snapshot_id)}, "
                f"{risk_category(run.change, risk)}): {_not_run_reason(risk)}; it remains a prediction",
                "    ",
                "      ",
            )
        )
    return [*lines, ""]


def _verification_section(run: DemoRun) -> list[str]:
    risks = {str(risk.id): risk for risk in run.analysis.risks}
    categories = sorted({risk_category(run.change, risks[str(r.risk_id)]) or "unknown" for r in run.not_run})
    not_run_line = (
        f"    {', '.join(categories)} risk(s): not_run - no executable proof; they remain predictions"
        if run.not_run
        else None
    )
    if run.local is None:
        lines = [
            "[6] Verification - not executed",
            (
                f"    {_count(len(run.plans), 'plan')} ready; their risks stay predicted/not_run until "
                "a runner executes them"
            ),
            _wrap(
                "rerun with --execute-local to run them on the local runner double (trusted local "
                "fixtures only; not a sandbox, not Sai's runner)",
                "    ",
                "      ",
            ),
        ]
        return [*lines, *([not_run_line] if not_run_line else [])]
    lines = [
        f"[6] Verification - {LOCAL_DOUBLE_LABEL}",
        _wrap(
            f"executed on copies of {_repo_relative(CONSUMER_REPOSITORIES)}/<repository> (contract "
            "doubles of Noirit's consumers); a plain local subprocess, not a sandbox",
            "    ",
            "      ",
        ),
    ]
    for item in run.local:
        plan = item.plan
        lines.append(f"    {item.repository}: {plan.scenario_id} ({plan.runner_profile})")
        for phase, phase_run in (("baseline", item.baseline), ("proposed", item.proposed)):
            lines.append(_phase_line(plan, phase, phase_run, item.measured_ms.get(phase)))
        lines.append(_wrap(f"result: {item.result.status} - {item.result.summary}", "      ", "        "))
        if item.patched is None:
            lines.append(f"      patched: not run - {item.patched_note}")
        else:
            described = item.patch.description if item.patch is not None else "patch"
            lines.append(
                _wrap(
                    f"patched ({described}; simulated edit): {item.patched.status} - {item.patched.reason}",
                    "      ",
                    "        ",
                )
            )
    return [*lines, *([not_run_line] if not_run_line else [])]


def _phase_line(plan: VerificationPlan, phase: str, run: PhaseRun, measured_ms: int | None) -> str:
    execution = run.execution
    classification = classify_phase(plan, phase, run)  # type: ignore[arg-type]
    timing = f", {measured_ms} ms" if measured_ms is not None else ""
    return _wrap(
        f"{phase:<9} exit {execution.exit_code}, {_count(execution.tests_run, 'test')}{timing}: "
        f"{classification.outcome} - {classification.reason}",
        "      ",
        "        ",
    )


def _change_line(change: APIChange) -> str:
    return f"{change.kind}, structural_breaking={str(change.structural_breaking).lower()}"


def _change_target(change: APIChange, endpoints: Mapping[str, Endpoint]) -> str:
    endpoint = endpoints.get(str(change.endpoint_id))
    where = f"{endpoint.method} {endpoint.path}" if endpoint is not None else f"endpoint {change.endpoint_id}"
    return f"{where} {change.field_path}"


def _endpoint_evidence(endpoint: Endpoint) -> str:
    parts = []
    for item in endpoint.evidence:
        if item.kind == "openapi":
            parts.append(f"openapi {item.json_pointer}")
        elif item.kind == "source":
            parts.append(f"source {item.file}:{item.line_start}")
        else:
            parts.append(item.kind)
    return "; ".join(parts)


def _reconciliation(result: DiscoveryResult) -> list[tuple[str, str, str]]:
    return [(endpoint.method, endpoint.path, endpoint.reconciliation) for endpoint in result.endpoints]


def _not_run_reason(risk: Risk) -> str:
    # The planner's AmbiguousScenario condition, through the same catalog function.
    bound = bound_scenario_ids(risk.evidence, risk.dependency_ids)
    if len(bound) > 1:
        return f"{len(bound)} executable scenarios are bound, so no single plan proves it"
    return "no executable scenario"


def _unmatched_reason(
    change: APIChange, dependency: Dependency, identities: Mapping[str, EndpointIdentity]
) -> str:
    if dependency.service_id != change.service_id:
        return f"service {clip_text(dependency.service_id, 80)}, not {change.service_id}"
    if not same_endpoint(dependency.endpoint_id, change.endpoint_id, identities):
        identity = identities.get(str(dependency.endpoint_id))
        if identity is None:
            return "an endpoint record outside the supplied endpoint catalog"
        return f"a different endpoint ({identity[1]} {clip_text(identity[2], 120)})"
    if not is_api_field_path(dependency.field_path):
        return f"local value {clip_text(dependency.field_path, 80)!r}, not an API field"
    return f"unrelated field {clip_text(dependency.field_path, 120)}"


def _repository(run: DemoRun, snapshot_id: object) -> str:
    repository = run.repositories.get(str(snapshot_id))
    return repository.handle if repository is not None else f"snapshot {_short(snapshot_id)}"


def _where(dependency: Dependency) -> str:
    # The location is consumer-scan text: render it through discovery.text before printing.
    location = clip_text(source_location(dependency) or "", 160)
    return location or f"dependency {_short(dependency.id)} (no source location)"


def _table(rows: Sequence[Sequence[str]], indent: str) -> list[str]:
    """Left-aligned columns; the last column is free width."""
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    lines = []
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        lines.append((indent + "  ".join([*cells, row[-1]])).rstrip())
    return lines


def _wrap(text: str, indent: str, subsequent: str) -> str:
    # Runs of spaces are kept (they align labels); newlines and tabs become spaces.
    return textwrap.fill(
        text,
        width=_WRAP_WIDTH,
        initial_indent=indent,
        subsequent_indent=subsequent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _count(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {plural or noun + 's'}"


def _short(value: object) -> str:
    return str(value)[:8]


# CLI -----------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m analyzer.demo",
        description="Shockwave end-to-end demo: discovery, changes, predicted risks, plans and "
        "(optionally) local verification on trusted fixtures.",
    )
    parser.add_argument(
        "--execute-local",
        action="store_true",
        help=f"run the display scenarios on the {LOCAL_DOUBLE_LABEL}",
    )
    parser.add_argument(
        "--write-samples",
        metavar="DIR",
        type=Path,
        help="write deterministic JSON samples into DIR",
    )
    parser.add_argument(
        "--node",
        metavar="PATH",
        help="Node.js >= 22.18 for the TypeScript scenario (default: the node next to this "
        "Python if present, else node on PATH)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run = run_demo(execute_local=args.execute_local, node_executable=args.node or default_node())
    except (ValueError, OSError) as error:  # DiscoveryError/InvalidDeclaredChange are ValueErrors
        print(f"shockwave demo failed: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(render_report(run))
    if args.write_samples is not None:
        try:
            written = write_samples(run, args.write_samples)
        except OSError as error:
            print(f"shockwave demo could not write samples: {error}", file=sys.stderr)
            return 1
        print(f"[7] Samples written to {args.write_samples}: {', '.join(path.name for path in written)}")
        if run.local is None:
            print(
                f"    {LOCAL_DOUBLE_SAMPLE} not written (requires --execute-local); "
                "an existing copy in that directory was left untouched"
            )
    return 1 if run.evidence_problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
