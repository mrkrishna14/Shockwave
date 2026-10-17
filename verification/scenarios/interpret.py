"""Interpretation of scenario execution evidence into verification and validation outcomes.

Sai's runner executes the staged native scenario and returns, per phase, its
ExecutionResult plus the scenario report the harness wrote. This module decides what
that evidence proves, conservatively:

* A phase PASSED only when the report says the relevant assertion held and the native
  runner exited 0. It is a RELEVANT_FAIL only when the report says the relevant
  comparison ran and the displayed value differed from the catalog's invariant
  expectation, and the runner exited non-zero. A report counts only if it is the one
  this plan staged: same scenario, phase and phase fixture, the relevant assertion, the
  catalog expectation (a test "repaired" to expect $0.10 is tampering), an untouched
  harness and binding, and a recorded request for the provider endpoint under test.
* Everything else is inconclusive: infrastructure errors, timeouts, no exit code, zero
  tests, a missing, oversized, unparseable or foreign report, harness errors (missing
  dependency, syntax error, any exception before the comparison), an unrelated failure
  (the relevant assertion held but the run failed) and inconsistent evidence.
* verified_regression needs a PASSED baseline (current-contract fixture) and a
  RELEVANT_FAIL on the proposed fixture, never merely a non-zero exit: a failing or
  broken baseline proves nothing about the proposed contract. Both PASSED is compatible
  within the tested scenario only. No execution at all stays not_run.
* A patched run (the migration applied, the proposed fixture, the same expectation) is
  validated only when it PASSED, and only within the tested scope. The patch is pinned
  to the proposed contract: unless made version-aware it is not backward compatible with
  current-contract responses, so every validated reason says its deployment must be
  coordinated with the provider's cutover.

Report contents are untrusted data produced inside the consumer checkout: they are
bounded, flattened to one line and quoted, never interpreted as instructions. A report
is consistency-checked, not authenticated: the staged argv keeps the checkout's pytest
configuration (conftest.py, ini files, plugins) out of the run, but code the run imports
from the checkout (the consumer's modules, or files a patch adds or changes) executes
in-process and could still forge a passing report. Validation therefore assumes a
non-adversarial patch; Sai's migration adapter should confine a patch to the
dependency's evidence files.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from discovery.contracts import SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION
from discovery.contracts import ExecutionResult, VerificationPlan, VerificationResult
from discovery.ids import verification_result_id
from verification.scenarios.catalog import ScenarioSpec
from verification.scenarios.staging import (
    MAX_REPORT_BYTES,
    REPORT_KIND,
    SCHEMA_VERSION,
    Phase,
    binding_sha256,
    clip_text,
    fixture_id_for_phase,
    harness_sha256,
    validated_spec,
)

PhaseOutcome = Literal["passed", "relevant_fail", "inconclusive"]
PatchedStatus = Literal["validated", "validation_failed", "inconclusive"]

# Ends every validated reason (PRODUCT.md: the consumer is pinned to the new contract).
PINNED_PATCH_CAVEAT = (
    "The patch is pinned to the proposed contract: it is not backward compatible with current-contract "
    "responses unless made version-aware, so its deployment must be coordinated with the provider's cutover."
)


@dataclass(frozen=True)
class PhaseRun:
    """One executed phase: the runner's result plus the raw report (read bounded, <= 64 KiB)."""

    execution: ExecutionResult
    report: dict | bytes | str | None


@dataclass(frozen=True)
class PhaseClassification:
    phase: Phase
    outcome: PhaseOutcome
    reason: str
    actual: str | None = None


@dataclass(frozen=True)
class PatchedOutcome:
    """Patched code against the proposed fixture with the same expectation (Sai maps the status).

    A validated reason ends with ``PINNED_PATCH_CAVEAT``.
    """

    status: PatchedStatus
    reason: str
    execution: ExecutionResult | None


def interpret_verification(
    plan: VerificationPlan,
    baseline: PhaseRun | None,
    proposed: PhaseRun | None,
) -> VerificationResult:
    """Combine baseline (current contract) and proposed-contract evidence into a result."""
    if baseline is None and proposed is None:
        status = "not_run"
        summary = "Not run: no execution evidence was supplied for either phase, so the risk remains a prediction."
    else:
        status, summary = _verdict(
            plan, classify_phase(plan, "baseline", baseline), classify_phase(plan, "proposed", proposed)
        )
    runs = [run for run in (baseline, proposed) if run is not None]
    return VerificationResult(
        schema_version=CONTRACT_SCHEMA_VERSION,
        id=verification_result_id(str(plan.id), _fingerprint(baseline), _fingerprint(proposed)),
        risk_id=str(plan.risk_id),
        repository_snapshot_id=str(plan.repository_snapshot_id),
        status=status,
        baseline=_execution_of(baseline),
        proposed=_execution_of(proposed),
        scenario_id=plan.scenario_id,
        summary=summary,
        artifact_ids=sorted({artifact for run in runs for artifact in run.execution.artifact_ids}),
    )


def interpret_patched_run(plan: VerificationPlan, patched: PhaseRun | None) -> PatchedOutcome:
    """Patched code vs the proposed fixture, judged by the same invariant expectation."""
    result = classify_phase(plan, "patched", patched)
    execution = _execution_of(patched)
    against = f"against the proposed contract fixture {plan.new_fixture_id}"
    if result.outcome == "passed":
        return PatchedOutcome(
            "validated",
            f"Validated within the tested scope only: {against}, {result.reason} with the unchanged "
            f"invariant expectation. {PINNED_PATCH_CAVEAT}",
            execution,
        )
    if result.outcome == "relevant_fail":
        return PatchedOutcome("validation_failed", f"Validation failed: {against}, {result.reason}.", execution)
    return PatchedOutcome(
        "inconclusive", f"Inconclusive: the patched run is not a relevant result - {result.reason}.", execution
    )


def classify_phase(plan: VerificationPlan, phase: Phase, run: PhaseRun | None) -> PhaseClassification:
    """Classify one phase as passed, relevant_fail or inconclusive, with the reason."""
    if run is None:
        return _inconclusive(phase, f"the {phase} phase was not executed")
    try:
        spec = validated_spec(plan)
    except ValueError as exc:
        return _inconclusive(phase, f"the plan does not match the scenario catalog ({clip_text(exc)})")
    execution = _as_execution(run.execution)
    problem = _execution_problem(execution)
    if problem is not None:
        return _inconclusive(phase, problem)
    report, problem = _parse_report(run.report)
    if report is None:
        return _inconclusive(phase, problem or "no scenario report was produced")
    return _classify_report(plan, spec, phase, execution, report)


def _verdict(
    plan: VerificationPlan, baseline: PhaseClassification, proposed: PhaseClassification
) -> tuple[str, str]:
    if baseline.outcome != "passed":
        return "inconclusive", (
            f"Inconclusive: there is no passing baseline against the current contract fixture "
            f"{plan.old_fixture_id} - {baseline.reason}. A failing or broken baseline proves nothing about "
            "the proposed contract."
        )
    if proposed.outcome == "relevant_fail":
        return "verified_regression", (
            f"Verified regression within scenario {plan.scenario_id}: against the current contract "
            f"fixture {plan.old_fixture_id}, {baseline.reason}; against the proposed contract fixture "
            f"{plan.new_fixture_id}, {proposed.reason}. Proven for the tested scenario only."
        )
    if proposed.outcome == "passed":
        return "compatible_within_tested_scope", (
            f"Compatible within the tested scenario only: against both the current ({plan.old_fixture_id}) "
            f"and the proposed ({plan.new_fixture_id}) contract fixtures, {proposed.reason}. This does not "
            "establish compatibility beyond what the scenario tests."
        )
    return "inconclusive", (
        f"Inconclusive: the baseline passed, but the run against the proposed contract fixture "
        f"{plan.new_fixture_id} is not a relevant result - {proposed.reason}."
    )


def _execution_problem(execution: ExecutionResult) -> str | None:
    if execution.infrastructure_error:
        return f"runner infrastructure error: {clip_text(execution.infrastructure_error)}"
    if execution.timed_out:
        return "the run timed out"
    if execution.exit_code is None:
        return "the run produced no exit code"
    if execution.tests_run == 0:
        return "zero tests ran"
    return None


def _parse_report(report: dict | bytes | str | None) -> tuple[dict | None, str | None]:
    if report is None:
        return None, "no scenario report was produced"
    if isinstance(report, dict):
        try:
            size = len(json.dumps(report, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return None, "the scenario report is not JSON-serializable"
        if size > MAX_REPORT_BYTES:
            return None, f"the scenario report exceeds {MAX_REPORT_BYTES // 1024} KiB"
        return report, None
    raw = _report_bytes(report)
    if raw is None:
        return None, "the scenario report is unreadable"
    if len(raw) > MAX_REPORT_BYTES:
        return None, f"the scenario report exceeds {MAX_REPORT_BYTES // 1024} KiB"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "the scenario report is not valid JSON"
    if not isinstance(parsed, dict):
        return None, "the scenario report is not a JSON object"
    return parsed, None


def _classify_report(
    plan: VerificationPlan, spec: ScenarioSpec, phase: Phase, execution: ExecutionResult, report: dict
) -> PhaseClassification:
    problem = _identity_problem(plan, phase, report)
    if problem is None and report.get("status") == "error":
        problem = (
            f"the harness reported an error before the relevant comparison ({_error_label(report)}); "
            "a missing dependency, syntax error or import failure proves nothing about the contract"
        )
    problem = problem or _integrity_problem(spec, report) or _exercise_problem(spec, report)
    if problem is not None:
        return _inconclusive(phase, problem)

    status, actual, expected = report.get("status"), report.get("actual"), spec.expected_display
    if status == "passed":
        if actual != expected:
            return _inconclusive(phase, "inconsistent report: status passed but the displayed value differs")
        if execution.exit_code != 0:
            return _inconclusive(
                phase,
                f"unrelated failure: the relevant assertion passed but the native run exited "
                f"{execution.exit_code}",
            )
        reason = f"the relevant assertion {spec.assertion_id} passed (expected {expected}, actual {actual})"
        return PhaseClassification(phase, "passed", reason, actual)
    if execution.exit_code == 0:
        return _inconclusive(phase, "inconsistent evidence: the report says failed but the native run exited 0")
    if not isinstance(actual, str) or actual == expected:
        return _inconclusive(phase, "inconsistent report: status failed without a differing displayed value")
    reason = (
        f"the relevant assertion {spec.assertion_id} failed: expected {expected}, actual {clip_text(actual, 80)}"
    )
    return PhaseClassification(phase, "relevant_fail", reason, actual)


def _identity_problem(plan: VerificationPlan, phase: Phase, report: dict) -> str | None:
    if report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != REPORT_KIND:
        return f"the report is not a {SCHEMA_VERSION} {REPORT_KIND}"
    if report.get("scenario_id") != plan.scenario_id:
        return f"the report belongs to scenario {_quote(report.get('scenario_id'))}, not {plan.scenario_id}"
    if report.get("phase") != phase:
        return f"the report is for phase {_quote(report.get('phase'))}, not {phase}"
    fixture_id = fixture_id_for_phase(plan, phase)
    if report.get("fixture_id") != fixture_id:
        return f"the {phase} run used fixture {_quote(report.get('fixture_id'))}, but this phase requires {fixture_id}"
    return None


def _integrity_problem(spec: ScenarioSpec, report: dict) -> str | None:
    if report.get("assertion_id") != spec.assertion_id:
        return f"the report is not from the relevant assertion {spec.assertion_id} (got {_quote(report.get('assertion_id'))})"
    if report.get("expected") != spec.expected_display:
        return (
            f"expectation tampering: the run expected {_quote(report.get('expected'))}, but the scenario's "
            f"invariant expectation is {spec.expected_display}; a test is never repaired by changing its expectation"
        )
    if report.get("harness_sha256") != harness_sha256(spec):
        return "the staged harness differs from the catalog harness (modified harness)"
    if report.get("binding_sha256") != binding_sha256(spec):
        return "binding.json differs from the catalog binding (tampered binding)"
    if report.get("status") not in ("passed", "failed"):
        return f"unknown report status {_quote(report.get('status'))}"
    return None


def _exercise_problem(spec: ScenarioSpec, report: dict) -> str | None:
    urls = report.get("requested_urls")
    if isinstance(urls, list) and any(_requests_endpoint(url, spec.path) for url in urls):
        return None
    return (
        f"the consumer never requested {spec.method} {spec.path}, so the run did not exercise the "
        "provider contract"
    )


def _requests_endpoint(url: object, path_template: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return _endpoint_pattern(path_template).search(path) is not None


def _endpoint_pattern(path_template: str) -> re.Pattern[str]:
    # Suffix match so a base URL with its own path prefix still counts.
    segments = [
        "[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment)
        for segment in path_template.strip("/").split("/")
    ]
    return re.compile("(?:^|/)" + "/".join(segments) + "/?$")


def _error_label(report: dict) -> str:
    error_type = report.get("error_type")
    label = clip_text(error_type, 80) if isinstance(error_type, str) and error_type else "unknown error"
    message = report.get("message")
    if not isinstance(message, str) or not message.strip():
        return label
    message = clip_text(message, 200)
    return message if message.startswith(label) else f"{label}: {message}"


def _quote(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return f'"{clip_text(value, 80)}"'
    return f"<{type(value).__name__}>"


def _inconclusive(phase: Phase, reason: str) -> PhaseClassification:
    return PhaseClassification(phase, "inconclusive", reason)


def _as_execution(execution: object) -> ExecutionResult:
    # The runner may build the canonical class while this module uses the doubles (or vice versa).
    if isinstance(execution, ExecutionResult):
        return execution
    if hasattr(execution, "model_dump"):
        return ExecutionResult.model_validate(execution.model_dump(mode="json"))
    return ExecutionResult.model_validate(execution)


def _execution_of(run: PhaseRun | None) -> ExecutionResult | None:
    return None if run is None else _as_execution(run.execution)


def _report_bytes(report: object) -> bytes | None:
    if isinstance(report, (bytes, bytearray, memoryview)):
        return bytes(report)
    if isinstance(report, str):
        try:
            return report.encode("utf-8")
        except UnicodeEncodeError:
            return None
    return None


def _fingerprint(run: PhaseRun | None) -> str | None:
    """Content fingerprint of one phase's evidence (a parsed report hashes by content)."""
    if run is None:
        return None
    parsed, _ = _parse_report(run.report)
    if parsed is not None:
        try:
            report_digest: str | None = _sha256(json.dumps(parsed, sort_keys=True, separators=(",", ":")))
        except (TypeError, ValueError, RecursionError):
            report_digest = "unserializable-report"
    else:
        raw = _report_bytes(run.report)
        report_digest = None if raw is None else hashlib.sha256(raw).hexdigest()
    payload = {"execution": _as_execution(run.execution).model_dump(mode="json"), "report": report_digest}
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
