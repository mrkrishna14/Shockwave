"""Control scenarios: unaffected consumer paths that must pass under every fixture variant.

A control is a negative control for a simulated change: an executable consumer path that
does not depend on the changed semantics (for the Stripe demo, the reporting summary that
counts PaymentIntents by currency and status and never reads ``amount``). It is NOT tied to
a Risk: no pointer is ever minted for it, ``build_verification_plan`` refuses it, and this
module never fabricates Risk, VerificationPlan or VerificationResult records. Its result is
the non-contract ``ControlResult``: status ``passed`` / ``failed`` / ``inconclusive``, the
per-case evidence (expected, actual, recorded requests), the native run's test count and
exit code, and the fixture id plus the sha256 the harness reported for the staged fixture.

Execution uses the same staging protocol and native harnesses as risk scenarios:
``staging.stage_control`` + ``staging.control_argv_hint`` for a runner, or
``run_control_locally`` on the development runner double (trusted local fixtures only;
plain subprocess, not a sandbox, not Sai's runner). Interpretation is the same case-report
evaluation as for risk scenarios (``verification.scenarios.cases``), so a control is
inconclusive - never passed - on zero tests, a timeout, an infrastructure error, a missing
or tampered report, a case whose call made no request, a request to anything but the
case's own ``GET`` resources, a missing Stripe-Version or a non-test-mode key. A passing
control shows only that the tested path is unaffected within the tested scope.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from discovery.contracts import ExecutionResult
from discovery.text import clip_text
from verification.scenarios.cases import (
    CaseEvidence,
    RecordedRequest,
    evaluate_cases_report,
)
from verification.scenarios.catalog import (
    CONTROL_SCENARIO_IDS,
    UnknownScenario,
    get_control,
)
from verification.scenarios.interpret import (
    PhaseRun,
    _as_execution,
    _execution_problem,
    _parse_report,
)
from verification.scenarios.staging import CONTROL_VARIANTS, ControlVariant

ControlStatus = Literal["passed", "failed", "inconclusive"]
CONTROL_RESULT_KIND = "shockwave.control_result.v1"  # a non-contract document kind
CONTROL_NOTE = (
    "Control scenario: not tied to any risk; no Risk, VerificationPlan or VerificationResult is recorded for it. "
    "A pass shows the tested path is unaffected within the tested scope only."
)

__all__ = [
    "CONTROL_NOTE",
    "CONTROL_RESULT_KIND",
    "CONTROL_SCENARIO_IDS",
    "CONTROL_VARIANTS",
    "ControlResult",
    "interpret_control",
    "run_control_locally",
]


@dataclass(frozen=True)
class ControlResult:
    """The interpreted outcome of one control run against one fixture variant (non-contract)."""

    scenario_id: str
    variant: ControlVariant
    fixture_id: str
    fixture_sha256: str | None  # as the harness reported it; verified against the canonical file unless inconclusive
    status: ControlStatus
    reason: str
    execution: ExecutionResult | None  # None when nothing was executed
    cases: tuple[CaseEvidence, ...]
    unattributed_requests: tuple[RecordedRequest, ...]
    # The runner's bounded output tail (untrusted, may hold local paths; logs only, not in to_document).
    output_tail: str | None = None

    @property
    def tests_run(self) -> int:
        return 0 if self.execution is None else self.execution.tests_run

    @property
    def requests(self) -> tuple[RecordedRequest, ...]:
        """Every recorded request: each case's, in case order, then any outside every case."""
        return (*(request for case in self.cases for request in case.requests), *self.unattributed_requests)

    def without_timing(self) -> ControlResult:
        """The same result with ``execution.duration_ms`` = 0, for byte-stable persisted documents."""
        if self.execution is None:
            return self
        return replace(self, execution=self.execution.model_copy(update={"duration_ms": 0}))

    def to_document(self) -> dict:
        """JSON-ready; deterministic for identical evidence once timing is normalized (``without_timing``)."""
        execution = self.execution
        return {
            "kind": CONTROL_RESULT_KIND,
            "note": CONTROL_NOTE,
            "scenario_id": self.scenario_id,
            "variant": self.variant,
            "fixture_id": self.fixture_id,
            "fixture_sha256": self.fixture_sha256,
            "status": self.status,
            "reason": self.reason,
            "tests_run": self.tests_run,
            "execution": None if execution is None else execution.model_dump(mode="json"),
            "cases": [case.to_document() for case in self.cases],
            "unattributed_requests": [request.to_document() for request in self.unattributed_requests],
            "requests": [request.to_document() for request in self.requests],
        }


def interpret_control(scenario_id: str, variant: str, run: PhaseRun | None) -> ControlResult:
    """Interpret one control run (``run`` is what the runner returned for that variant)."""
    try:
        spec = get_control(scenario_id)
    except UnknownScenario:
        raise ValueError(f"not a control scenario: {clip_text(scenario_id, 120)!r}") from None
    if variant not in CONTROL_VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(CONTROL_VARIANTS)}; got {clip_text(variant, 40)!r}")
    fixture_id = spec.fixture_for(variant)
    against = f"against the {variant} fixture {fixture_id}"

    def result(status: ControlStatus, reason: str, execution: ExecutionResult | None, **evidence) -> ControlResult:
        return ControlResult(
            scenario_id=spec.scenario_id,
            variant=variant,  # type: ignore[arg-type]
            fixture_id=fixture_id,
            fixture_sha256=evidence.get("fixture_sha256"),
            status=status,
            reason=reason,
            execution=execution,
            cases=evidence.get("cases", ()),
            unattributed_requests=evidence.get("unattributed_requests", ()),
            output_tail=None if run is None else run.output_tail,
        )

    if run is None:
        return result("inconclusive", f"Inconclusive: the control was not executed {against}.", None)
    execution = _as_execution(run.execution)
    problem = _execution_problem(execution)
    report, parse_problem = (None, None) if problem else _parse_report(run.report)
    problem = problem or (parse_problem if report is None else None)
    if report is None:
        reason = problem or "no scenario report was produced"
        return result("inconclusive", f"Inconclusive: the control run {against} is not a result - {reason}.", execution)
    evaluation = evaluate_cases_report(spec, variant, fixture_id, execution, report)
    evidence = {
        "fixture_sha256": evaluation.fixture_sha256,
        "cases": evaluation.cases,
        "unattributed_requests": evaluation.unattributed_requests,
    }
    if evaluation.outcome == "passed":
        return result("passed", f"Control passed {against}: {evaluation.reason}. {CONTROL_NOTE}", execution, **evidence)
    if evaluation.outcome == "failed":
        listed = "; ".join(f"the control case {case.case_id} failed ({case.detail})" for case in evaluation.failures)
        return result("failed", f"Control failed {against}: {listed}.", execution, **evidence)
    return result(
        "inconclusive",
        f"Inconclusive: the control run {against} is not a result - {evaluation.reason}.",
        execution,
        **evidence,
    )


def run_control_locally(
    scenario_id: str,
    consumer_repo: Path,
    variant: str,
    *,
    work_dir: Path,
    node_executable: str | None = None,
    timeout_seconds: int | None = None,
    patch: Callable[[Path], None] | None = None,
) -> ControlResult:
    """Stage and run a control against a copy of ``consumer_repo`` on the LOCAL RUNNER DOUBLE, then interpret it.

    The double copies the repository to ``work_dir/checkout-control-<variant>`` (the source
    is never mutated), optionally applies ``patch`` to the copy, and runs the control's
    trusted profile as a plain local subprocess: trusted local fixtures only, not a sandbox,
    not Sai's runner.
    """
    from verification.scenarios.dev_runner_double import run_control_phase_locally

    run = run_control_phase_locally(
        scenario_id,
        consumer_repo,
        variant,
        work_dir=work_dir,
        node_executable=node_executable,
        timeout_seconds=timeout_seconds,
        patch=patch,
    )
    return interpret_control(scenario_id, variant, run)
