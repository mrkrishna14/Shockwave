"""``execute``: run the old/proposed scenario phases on the LOCAL RUNNER DOUBLE and interpret them.

For every predicted risk, ``build_verification_plan`` (trusting only the three demo repository
connections) either returns a plan for its bound executable scenario or raises
``NoExecutableScenario``; the latter is recorded with ``not_run_result`` and the missing evidence
(the reporting ledger's storage risk). Each plan runs its baseline (current-contract fixture) and
proposed (SIMULATED fixture) phases with ``dev_runner_double.run_phase_locally`` - trusted local
fixtures; plain subprocess, not a sandbox, not Sai's runner - and ``interpret_verification``
decides: baseline failures, zero tests, infrastructure errors and timeouts are inconclusive, never
a verified regression. The reporting summary CONTROL runs under both fixture variants through the
control API (never tied to a risk). One supporting observation is recorded as well: the reporting
consumer's OWN suite run against the simulated proposed fixture data; it never changes the storage
risk's not_run status.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from analyzer.impact import risk_category
from analyzer.rules import STORAGE_MISSING_EVIDENCE
from discovery.contracts import APIChange, Risk, VerificationPlan, VerificationResult
from shockwave_demo.change import load_change
from shockwave_demo.common import (
    CONSUMER_MANIFEST_PATH,
    RUNNER_LABEL,
    RUNNER_RESULT_PREFIX,
    TIMING_NOTE,
    DemoError,
    Layout,
    count,
    display_path,
    read_json,
    read_result,
    remove_tree,
    repo_relative,
    short_scenario,
    write_json,
)
from shockwave_demo.prepare import load_snapshots, run_own_suite
from shockwave_demo.records import write_run_record
from verification.scenarios.catalog import (
    STRIPE_FIXTURE_PROPOSED,
    fixture_path,
    fixture_sha256,
)
from verification.scenarios.controls import ControlResult, run_control_locally
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import (
    PhaseRun,
    classify_phase,
    interpret_verification,
)
from verification.scenarios.plans import (
    NoExecutableScenario,
    PlanConfig,
    build_verification_plan,
    not_run_result,
)
from verification.scenarios.staging import control_argv_hint, runner_argv_hint

PLANS_RESULT = "verification/plans.json"
RESULTS_RESULT = "verification/results.json"
CONTROLS_RESULT = "verification/controls.json"
OBSERVATIONS_RESULT = "verification/observations.json"
_MISSING = re.compile(r"Missing evidence: ([^.]+)\.")


def without_timing(run: PhaseRun) -> PhaseRun:
    return PhaseRun(run.execution.model_copy(update={"duration_ms": 0}), run.report, run.output_tail)


def labeled(result: VerificationResult) -> VerificationResult:
    """The result with its provenance in the record (the summary is not part of the result id)."""
    return result.model_copy(update={"summary": f"{RUNNER_RESULT_PREFIX} {result.summary}"})


def not_run_reason(error: Exception, risk: Risk, change: APIChange) -> str:
    category = risk_category(change, risk) or "unknown"
    found = _MISSING.search(risk.explanation)
    missing = [part.strip() for part in found.group(1).split(";")] if found else []
    if category == "storage":
        missing = list(dict.fromkeys([f"an executable {category} scenario", *missing, *STORAGE_MISSING_EVIDENCE]))
    listed = "; ".join(missing) if missing else "an executable scenario for this usage"
    return (
        f"Not run: {str(error).rstrip('.')}. Potential {category} risk, not verified: no executable proof ran, so it remains a "
        f"prediction. Missing evidence: {listed}."
    )


def trusted_config(snapshots: Mapping) -> PlanConfig:
    """Only the three team-written demo repositories are trusted local fixtures."""
    return PlanConfig(frozenset(str(item.repository_connection_id) for item in snapshots.values()))


def scenario_argv(plan: VerificationPlan) -> list[str]:
    interpreter = "python" if plan.runner_profile.endswith("python") else "node"
    return [interpreter, *runner_argv_hint(plan)[1:]]


def phase_record(
    layout: Layout,
    *,
    run_id: str,
    stage: str,
    purpose: str,
    plan: VerificationPlan,
    phase: str,
    classify_as: str,
    run: PhaseRun,
    repository: str,
    commit: str,
    checkout: str,
    patch: Mapping | None = None,
) -> tuple[dict, object]:
    """Persist one scenario phase run; returns (normalized record, its classification)."""
    measured = run.execution.duration_ms
    normalized_run = without_timing(run)
    classification = classify_phase(plan, classify_as, normalized_run)  # type: ignore[arg-type]
    fixture_id = plan.old_fixture_id if classify_as == "baseline" else plan.new_fixture_id
    cases = classification.evaluation.cases if classification.evaluation is not None else ()
    execution = normalized_run.execution
    record = {
        "run_id": run_id,
        "stage": stage,
        "purpose": purpose,
        "runner": RUNNER_LABEL,
        "repository": repository,
        "commit_sha": commit,
        "scenario_id": plan.scenario_id,
        "plan_id": str(plan.id),
        "phase": phase,
        "fixture": {
            "fixture_set_id": fixture_id,
            "path": repo_relative(fixture_path(fixture_id)),
            "sha256": fixture_sha256(fixture_id),
            "reported_sha256": classification.evaluation.fixture_sha256 if classification.evaluation else None,
        },
        "patch": dict(patch) if patch else None,
        "command": {"argv": scenario_argv(plan), "cwd": checkout},
        "exit_code": execution.exit_code,
        "timed_out": execution.timed_out,
        "infrastructure_error": execution.infrastructure_error,
        "tests_run": execution.tests_run,
        "outcome": classification.outcome,
        "reason": classification.reason,
        "assertions": [case.to_document() for case in cases],
    }
    return write_run_record(layout, record, run.output_tail, measured), classification


def control_record(layout: Layout, result: ControlResult, repository: str, commit: str, checkout: str) -> dict:
    run_id = f"control.{short_scenario(result.scenario_id)}.{result.variant}"
    normalized = result.without_timing()
    execution = normalized.execution
    record = {
        "run_id": run_id,
        "stage": "execute",
        "purpose": f"unaffected-path CONTROL against the {result.variant} fixture (not tied to any risk)",
        "runner": RUNNER_LABEL,
        "repository": repository,
        "commit_sha": commit,
        "scenario_id": result.scenario_id,
        "phase": result.variant,
        "fixture": {
            "fixture_set_id": result.fixture_id,
            "path": repo_relative(fixture_path(result.fixture_id)),
            "sha256": fixture_sha256(result.fixture_id),
            "reported_sha256": result.fixture_sha256,
        },
        "patch": None,
        "command": {"argv": ["python", *control_argv_hint(result.scenario_id)[1:]], "cwd": checkout},
        "exit_code": None if execution is None else execution.exit_code,
        "timed_out": False if execution is None else execution.timed_out,
        "infrastructure_error": None if execution is None else execution.infrastructure_error,
        "tests_run": result.tests_run,
        "outcome": result.status,
        "reason": result.reason,
        "assertions": [case.to_document() for case in result.cases],
    }
    measured = None if result.execution is None else result.execution.duration_ms
    return write_run_record(layout, record, result.output_tail, measured)


def run_execute(layout: Layout) -> list[str]:
    snapshots, repositories = load_snapshots(layout)
    change, analysis = load_change(layout)
    by_snapshot = {details["repository_snapshot_id"]: name for name, details in repositories.items()}
    config = trusted_config(snapshots)
    plans: list[VerificationPlan] = []
    results: list[VerificationResult] = []
    lines = [f"Execute old/proposed scenarios - {RUNNER_LABEL}"]
    for risk in analysis.risks:
        snapshot = snapshots[str(risk.repository_snapshot_id)]
        try:
            plans.append(build_verification_plan(risk, snapshot, config=config))
        except NoExecutableScenario as error:
            result = not_run_result(risk, not_run_reason(error, risk, change))
            results.append(result)
            lines.append(
                f"  NOT_RUN     {by_snapshot[str(risk.repository_snapshot_id)]}: {risk_category(change, risk)} risk "
                "(potential; no executable scenario; missing evidence recorded)"
            )
    for plan in plans:
        repository = by_snapshot[str(plan.repository_snapshot_id)]
        commit = repositories[repository]["commit_sha"]
        if plan.runner_profile.endswith("node") and not (layout.repo(repository) / "node_modules" / "stripe").is_dir():
            raise DemoError(f"{repository} has no installed stripe-node (node_modules): run `demo.py prepare` first")
        work = layout.scratch_dir / f"execute-{short_scenario(plan.scenario_id)}"
        try:
            runs = {}
            classifications = {}
            for phase in ("baseline", "proposed"):
                run = run_phase_locally(plan, layout.repo(repository), phase, work_dir=work, node_executable=layout.node)
                runs[phase] = without_timing(run)
                record, classifications[phase] = phase_record(
                    layout,
                    run_id=f"execute.{short_scenario(plan.scenario_id)}.{phase}",
                    stage="execute",
                    purpose=f"original code against the {phase} fixture",
                    plan=plan,
                    phase=phase,
                    classify_as=phase,
                    run=run,
                    repository=repository,
                    commit=commit,
                    checkout=display_path(work / f"checkout-{phase}", layout),
                )
                lines.append(
                    f"  {phase:<9}   {repository}: exit {record['exit_code']}, {count(record['tests_run'], 'test')}: "
                    f"{record['outcome']}"
                )
        finally:
            remove_tree(work, inside=layout.scratch_dir)
        result = labeled(interpret_verification(plan, runs["baseline"], runs["proposed"]))
        results.append(result)
        lines.append(f"  RESULT      {repository}: {result.status} - {_short_reason(classifications['proposed'])}")
    controls = run_controls(layout, repositories, lines)
    observations = run_observations(layout, repositories, analysis, change, lines)
    order = {str(risk.id): index for index, risk in enumerate(analysis.risks)}
    results.sort(key=lambda item: order[str(item.risk_id)])
    write_json(layout.result_path(PLANS_RESULT), [plan.model_dump(mode="json") for plan in plans])
    write_json(layout.result_path(RESULTS_RESULT), [result.model_dump(mode="json") for result in results])
    write_json(
        layout.result_path(CONTROLS_RESULT),
        {
            "kind": "shockwave-demo.controls@1",
            "runner": RUNNER_LABEL,
            "timing": TIMING_NOTE,
            "controls": controls,
        },
    )
    write_json(layout.result_path(OBSERVATIONS_RESULT), {"kind": "shockwave-demo.observations@1", "observations": observations})
    from shockwave_demo.views import write_views

    write_views(layout)
    statuses = {}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1
    lines.append(
        "  summary     "
        + ", ".join(f"{name} {number}" for name, number in sorted(statuses.items()))
        + "; controls: "
        + ", ".join(f"{item['variant']} {item['status']}" for item in controls)
    )
    return lines


def _short_reason(classification) -> str:
    return classification.reason if len(classification.reason) <= 160 else classification.reason[:157] + "..."


def run_controls(layout: Layout, repositories: Mapping, lines: list[str]) -> list[dict]:
    manifest = read_json(CONSUMER_MANIFEST_PATH)
    from verification.scenarios.catalog import CONTROL_SCENARIO_IDS

    documents = []
    for entry in manifest["repositories"]:
        for scenario_id in entry.get("scenarios", []):
            if scenario_id not in CONTROL_SCENARIO_IDS:
                continue
            repository = entry["name"]
            commit = repositories[repository]["commit_sha"]
            for variant in ("baseline", "proposed"):
                work = layout.scratch_dir / f"control-{short_scenario(scenario_id)}-{variant}"
                try:
                    result = run_control_locally(
                        scenario_id, layout.repo(repository), variant, work_dir=work, node_executable=layout.node
                    )
                    record = control_record(
                        layout, result, repository, commit, display_path(work / f"checkout-control-{variant}", layout)
                    )
                finally:
                    remove_tree(work, inside=layout.scratch_dir)
                document = result.without_timing().to_document()
                document.update({"repository": repository, "commit_sha": commit, "run_id": record["run_id"]})
                documents.append(document)
                lines.append(
                    f"  CONTROL     {repository} ({variant}): {result.status} - {result.tests_run} test, "
                    f"{len(result.requests)} requests (GET /v1/payment_intents/<id>, test-mode auth)"
                )
    return documents


def run_observations(layout: Layout, repositories: Mapping, analysis, change: APIChange, lines: list[str]) -> list[dict]:
    """The storage-risk repository's own suite against the SIMULATED proposed fixture data (supporting evidence)."""
    manifest = {entry["name"]: entry for entry in read_json(CONSUMER_MANIFEST_PATH)["repositories"]}
    by_snapshot = {details["repository_snapshot_id"]: name for name, details in repositories.items()}
    observations = []
    for risk in analysis.risks:
        if risk_category(change, risk) != "storage":
            continue
        repository = by_snapshot[str(risk.repository_snapshot_id)]
        record = run_own_suite(
            layout,
            manifest[repository],
            repositories[repository]["commit_sha"],
            f"execute.{repository}.own-suite-proposed-fixture",
            stage="execute",
            purpose=(
                "supporting observation: the consumer's own suite with its bundled fixture data replaced by the "
                "SIMULATED proposed fixture set (assertions unchanged)"
            ),
            replace_fixture=fixture_path(STRIPE_FIXTURE_PROPOSED).read_bytes(),
        )
        observations.append(
            {
                "risk_id": str(risk.id),
                "repository": repository,
                "run_id": record["run_id"],
                "outcome": record["outcome"],
                "tests_run": record["tests_run"],
                "tests_failed": record["tests_failed"],
                "failing_tests": record["failing_tests"],
                "fixture": record["fixture"],
                "statement": (
                    "Supporting observation only: the consumer's own suite was run with its bundled fixture data "
                    "replaced by the simulated proposed fixture set. "
                    + (
                        f"{count(record['tests_failed'], 'own test')} failed ({', '.join(record['failing_tests'])}). "
                        if record["tests_failed"]
                        else "No own test failed. "
                    )
                    + "Shockwave has no executable storage scenario, so the storage risk's verification status stays "
                    "not_run (a potential risk); whether this observation upgrades it is a product decision."
                ),
            }
        )
        lines.append(
            f"  OBSERVED    {repository} own suite vs proposed fixture: {record['outcome']} "
            f"({record['tests_failed']} of {record['tests_run']} failed: {', '.join(record['failing_tests']) or '-'}); "
            "storage risk stays not_run"
        )
    return observations


def load_verification(layout: Layout) -> tuple[list[VerificationPlan], list[VerificationResult]]:
    plans = [VerificationPlan.model_validate(item) for item in read_result(layout, PLANS_RESULT, produced_by="execute")]
    results = [
        VerificationResult.model_validate(item) for item in read_result(layout, RESULTS_RESULT, produced_by="execute")
    ]
    return plans, results
