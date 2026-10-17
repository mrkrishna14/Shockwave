"""Evaluation of case-scenario reports (``staging.CASES_REPORT_FORMAT``).

A case harness evaluates every case of a scenario and records the requests its loopback
fixture server received. ``evaluate_cases_report`` decides, conservatively, what one such
report proves; ``verification.scenarios.interpret`` (risk scenarios) and
``verification.scenarios.controls`` (controls) map the evaluation to their own outcomes.

The evaluation is inconclusive unless all of this holds:

* identity: the report is a ``shockwave.cases.v1`` scenario report for this scenario, kind,
  phase and the phase's fixture set;
* integrity: the staged harness and binding are the catalog's, and the staged fixture
  bytes hash to the canonical fixture file (the fixture revision);
* the cases are the catalog's cases, in order, with the catalog's invariant expectations
  (a case "repaired" to expect the buggy value is tampering), and every case's comparison
  ran (an exception, a missing field or a non-JSON result proves nothing);
* preconditions, which are the evidence that the application invoked the SDK method, the
  request targeted the expected provider operation and the fixture response reached the
  real logic: every case made at least one request, every request was
  ``GET <operation path>`` for one of the case's own resource ids and every one of those ids
  was requested, no request fell outside a case or beyond the recording bound, and (Stripe
  SDK bindings) every request carried a Stripe-Version header naming the fixture set's API
  version and authenticated with a test-mode key (``Bearer sk_test_...``);
* consistency: each case's status agrees with its expected and actual values, the report
  status agrees with the cases, and the native runner's exit code agrees with the report
  (every case passed exactly when the run exited 0).

Then the outcome is "passed" (every case passed) or "failed" (the failing cases are listed
in ``failures``). Report contents are untrusted data from the consumer checkout: anything
quoted is bounded and flattened by ``discovery.text.clip_text``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from discovery.contracts import ExecutionResult
from discovery.text import clip_text
from verification.scenarios.catalog import (
    ScenarioCase,
    ScenarioSpec,
    canonical_json,
    case_request_paths,
    fixture_api_version,
    fixture_sha256,
)
from verification.scenarios.staging import (
    AUTHORIZATION_KINDS,
    CASES_REPORT_FORMAT,
    REPORT_KIND,
    SCHEMA_VERSION,
    binding_sha256,
    harness_sha256,
)

CaseStatus = Literal["passed", "failed", "error"]
CasesOutcome = Literal["passed", "failed", "inconclusive"]
_CASE_STATUSES = ("passed", "failed", "error")


@dataclass(frozen=True)
class RecordedRequest:
    """One request the harness's fixture server received (the key itself is never recorded).

    Fields hold the reported values verbatim, so preconditions compare exactly what was
    sent (a zero-width character must not make another path look like the expected one);
    ``to_document`` and every reason render them through ``clip_text``.
    """

    method: str
    path: str
    stripe_version: str | None
    authorization: str  # staging.AUTHORIZATION_KINDS

    def to_document(self) -> dict:
        return {
            "method": clip_text(self.method, 16),
            "path": clip_text(self.path, 200),
            "stripe_version": None if self.stripe_version is None else clip_text(self.stripe_version, 64),
            "authorization": self.authorization,
        }

    def label(self) -> str:
        return f"{clip_text(self.method, 16)} {clip_text(self.path, 120)}"


@dataclass(frozen=True)
class CaseEvidence:
    """One case as reported, checked against its catalog definition."""

    case_id: str
    role: Literal["relevant", "guard", "control"]
    expected: object
    actual: object
    status: CaseStatus
    detail: str  # "expected <value>, actual <value>", or the error label of an "error" case
    requests: tuple[RecordedRequest, ...]

    def to_document(self) -> dict:
        return {
            "case_id": self.case_id,
            "role": self.role,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
            "detail": self.detail,
            "requests": [request.to_document() for request in self.requests],
        }


@dataclass(frozen=True)
class CasesEvaluation:
    outcome: CasesOutcome
    reason: str
    cases: tuple[CaseEvidence, ...] = ()  # set whenever the report's cases could be read
    failures: tuple[CaseEvidence, ...] = ()  # failed cases, when outcome == "failed"
    fixture_sha256: str | None = None  # as reported (verified against the canonical file unless inconclusive)
    unattributed_requests: tuple[RecordedRequest, ...] = ()


def value_text(value: object, limit: int = 80) -> str:
    """A case value as one bounded line: strings as-is, booleans as True/False, else canonical JSON."""
    if isinstance(value, str):
        return clip_text(value, limit)
    if isinstance(value, bool):
        return "True" if value else "False"
    try:
        return clip_text(canonical_json(value), limit * 2)
    except (TypeError, ValueError):
        return f"<{type(value).__name__}>"


def describe_case(case: ScenarioCase, actual: object) -> str:
    """``expected $10.00, actual $0.10`` / ``expected needs_manual_review=True, actual False``."""
    expected = case.expected
    field = ".".join(case.result_path)
    label = f"{clip_text(field, 60)}=" if field and not isinstance(expected, str) else ""
    return f"expected {label}{value_text(expected)}, actual {value_text(actual)}"


def evaluate_cases_report(
    spec: ScenarioSpec, phase: str, fixture_id: str, execution: ExecutionResult, report: dict
) -> CasesEvaluation:
    """What one case report proves for ``spec`` run in ``phase`` against ``fixture_id``."""
    problem = _identity_problem(spec, phase, fixture_id, report)
    if problem is None and report.get("status") == "error":
        problem = (
            f"the harness reported an error before any case ran ({_error_label(report)}); a missing dependency, "
            "syntax error or import failure proves nothing about the contract"
        )
    problem = problem or _integrity_problem(spec, fixture_id, report)
    if problem is not None:
        return CasesEvaluation("inconclusive", problem)
    reported_sha = report.get("fixture_sha256")

    cases, problem = _read_cases(spec, report)
    if problem is not None:
        return CasesEvaluation("inconclusive", problem, fixture_sha256=reported_sha)
    unattributed, problem = _read_requests(report.get("unattributed_requests"), "unattributed_requests")

    def evaluation(outcome: CasesOutcome, reason: str, failures: tuple[CaseEvidence, ...] = ()) -> CasesEvaluation:
        return CasesEvaluation(outcome, reason, cases, failures, reported_sha, unattributed)

    problem = (
        problem
        or _error_problem(cases)
        or _request_log_problem(report, unattributed)
        or _precondition_problem(spec, fixture_id, cases)
        or _consistency_problem(report, execution, cases)
    )
    if problem is not None:
        return evaluation("inconclusive", problem)
    failures = tuple(case for case in cases if case.status == "failed")
    if not failures:
        return evaluation("passed", _passed_reason(cases))
    listed = "; ".join(f"{case.case_id} ({case.role}): {case.detail}" for case in failures)
    return evaluation("failed", f"case(s) failed - {listed}", failures)


def _identity_problem(spec: ScenarioSpec, phase: str, fixture_id: str, report: dict) -> str | None:
    if report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != REPORT_KIND:
        return f"the report is not a {SCHEMA_VERSION} {REPORT_KIND}"
    if report.get("report_format") != CASES_REPORT_FORMAT:
        return f"the report is not a {CASES_REPORT_FORMAT} case report (got {_quote(report.get('report_format'))})"
    if report.get("scenario_id") != spec.scenario_id:
        return f"the report belongs to scenario {_quote(report.get('scenario_id'))}, not {spec.scenario_id}"
    if report.get("scenario_kind") != spec.kind:
        return f"the report is for a {_quote(report.get('scenario_kind'))} scenario, but {spec.scenario_id} is a {spec.kind}"
    if report.get("phase") != phase:
        return f"the report is for phase {_quote(report.get('phase'))}, not {phase}"
    if report.get("fixture_id") != fixture_id:
        return f"the {phase} run used fixture {_quote(report.get('fixture_id'))}, but this phase requires {fixture_id}"
    return None


def _integrity_problem(spec: ScenarioSpec, fixture_id: str, report: dict) -> str | None:
    if report.get("harness_sha256") != harness_sha256(spec):
        return "the staged harness differs from the catalog harness (modified harness)"
    if report.get("binding_sha256") != binding_sha256(spec):
        return "binding.json differs from the catalog binding (tampered binding)"
    try:
        expected_sha = fixture_sha256(fixture_id)
    except OSError as exc:
        return f"the canonical fixture {fixture_id} cannot be read ({type(exc).__name__})"
    if report.get("fixture_sha256") != expected_sha:
        return (
            f"fixture integrity mismatch: the staged fixture bytes do not hash to the canonical {fixture_id} "
            f"(sha256 {expected_sha[:12]}...)"
        )
    if report.get("status") not in ("passed", "failed"):
        return f"unknown report status {_quote(report.get('status'))}"
    return None


def _read_cases(spec: ScenarioSpec, report: dict) -> tuple[tuple[CaseEvidence, ...], str | None]:
    entries = report.get("cases")
    if not isinstance(entries, list) or len(entries) != len(spec.cases):
        count = len(entries) if isinstance(entries, list) else "no"
        return (), f"the report lists {count} cases, but {spec.scenario_id} defines {len(spec.cases)}"
    cases: list[CaseEvidence] = []
    for position, (case, entry) in enumerate(zip(spec.cases, entries, strict=True)):
        if not isinstance(entry, dict) or entry.get("case_id") != case.case_id:
            found = _quote(entry.get("case_id")) if isinstance(entry, dict) else "not an object"
            return (), f"report case {position} is {found}, but the catalog's case {position} is {case.case_id}"
        if entry.get("relevant") is not case.relevant:
            return (), f"case {case.case_id}: the report changes whether it is the relevant case"
        if not _same_json(entry.get("expected"), case.expected_json):
            return (), (
                f"expectation tampering: case {case.case_id} expected {value_text(entry.get('expected'))}, but its "
                f"invariant expectation is {value_text(case.expected)}; a test is never repaired by changing its "
                "expectation"
            )
        status = entry.get("status")
        if status not in _CASE_STATUSES:
            return (), f"case {case.case_id}: unknown case status {_quote(status)}"
        requests, problem = _read_requests(entry.get("requests"), f"case {case.case_id} requests")
        if problem is not None:
            return (), problem
        actual = entry.get("actual")
        if status == "error":
            detail = f"error before its comparison: {_error_label(entry)}"
        elif status == "passed" and not _same_json(actual, case.expected_json):
            return (), f"inconsistent report: case {case.case_id} says passed, but its actual value differs"
        elif status == "failed" and (_same_json(actual, case.expected_json) or not _json_data(actual)):
            return (), f"inconsistent report: case {case.case_id} says failed without a differing actual value"
        else:
            detail = describe_case(case, actual)
        cases.append(
            CaseEvidence(
                case_id=case.case_id,
                role=spec.case_role(case),
                expected=case.expected,
                actual=actual if status != "error" else None,
                status=status,
                detail=detail,
                requests=requests,
            )
        )
    return tuple(cases), None


def _read_requests(entries: object, label: str) -> tuple[tuple[RecordedRequest, ...], str | None]:
    if not isinstance(entries, list):
        return (), f"{label}: the request log is missing or malformed"
    requests: list[RecordedRequest] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("method"), str)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("stripe_version"), str | None)
            or entry.get("authorization") not in AUTHORIZATION_KINDS
        ):
            return (), f"{label}: a request record is malformed"
        requests.append(
            RecordedRequest(entry["method"], entry["path"], entry["stripe_version"], entry["authorization"])
        )
    return tuple(requests), None


def _error_problem(cases: tuple[CaseEvidence, ...]) -> str | None:
    errored = [case for case in cases if case.status == "error"]
    if not errored:
        return None
    first = errored[0]
    more = f" (and {len(errored) - 1} more)" if len(errored) > 1 else ""
    return (
        f"case {first.case_id} raised {first.detail.removeprefix('error before its comparison: ')}{more}; a case whose "
        "comparison never ran proves nothing about the contract"
    )


def _request_log_problem(report: dict, unattributed: tuple[RecordedRequest, ...]) -> str | None:
    dropped = report.get("requests_dropped")
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped != 0:
        return "the request log was truncated or is malformed, so not every request can be checked"
    if unattributed:
        first = unattributed[0]
        return (
            f"{len(unattributed)} request(s) reached the fixture server outside every case's call (first: "
            f"{first.label()}), so the requests cannot all be attributed to the cases"
        )
    return None


def _precondition_problem(spec: ScenarioSpec, fixture_id: str, cases: tuple[CaseEvidence, ...]) -> str | None:
    stripe = spec.binding.http_injection == "stripe_sdk_env"
    api_version = fixture_api_version(fixture_id) if stripe else None
    by_id = {case.case_id: case for case in spec.cases}
    for evidence in cases:
        case = by_id[evidence.case_id]
        allowed = case_request_paths(spec, case)
        ids = ", ".join(case.resource_ids)
        if not evidence.requests:
            return (
                f"case {case.case_id}: the consumer call made no request to the fixture server, so the run never showed "
                f"the application invoking the SDK method ({spec.method} {spec.path})"
            )
        for request in evidence.requests:
            if request.method != spec.method or request.path not in allowed:
                return (
                    f"case {case.case_id}: a request targeted {request.label()}, not {spec.method} {spec.path} for {ids}"
                )
        missing = sorted(set(allowed) - {request.path for request in evidence.requests})
        if missing:
            return (
                f"case {case.case_id}: {', '.join(missing)} was never requested, so not every fixture response reached "
                "the consumer's logic"
            )
        if not stripe:
            continue
        for request in evidence.requests:
            if not request.stripe_version:
                return f"case {case.case_id}: a request carried no Stripe-Version header"
            if api_version is not None and request.stripe_version != api_version:
                return (
                    f"case {case.case_id}: a request carried Stripe-Version {clip_text(request.stripe_version, 64)}, but "
                    f"the fixture set simulates API version {api_version}"
                )
            if request.authorization != "test_mode_bearer":
                return (
                    f"case {case.case_id}: a request was not authenticated with a test-mode key (Bearer sk_test_...; "
                    f"recorded: {request.authorization})"
                )
    return None


def _consistency_problem(report: dict, execution: ExecutionResult, cases: tuple[CaseEvidence, ...]) -> str | None:
    all_passed = all(case.status == "passed" for case in cases)
    if (report.get("status") == "passed") != all_passed:
        return "inconsistent report: its status disagrees with its cases"
    if all_passed and execution.exit_code != 0:
        return f"unrelated failure: every case passed but the native run exited {execution.exit_code}"
    if not all_passed and execution.exit_code == 0:
        return "inconsistent evidence: the report says a case failed but the native run exited 0"
    return None


def _passed_reason(cases: tuple[CaseEvidence, ...]) -> str:
    relevant = [case for case in cases if case.role == "relevant"]
    others = [case.case_id for case in cases if case.role != "relevant"]
    if relevant:
        guards = f"; guard case(s) {', '.join(others)} passed" if others else ""
        return f"all {len(cases)} cases passed (relevant case {relevant[0].case_id}: {relevant[0].detail}{guards})"
    listed = "; ".join(f"{case.case_id}: {case.detail}" for case in cases)
    return f"all {len(cases)} case(s) passed ({listed})"


def _same_json(value: object, expected_json: str) -> bool:
    try:
        return canonical_json(value) == expected_json
    except (TypeError, ValueError):
        return False


def _json_data(value: object) -> bool:
    try:
        canonical_json(value)
    except (TypeError, ValueError):
        return False
    return True


def _error_label(document: dict) -> str:
    error_type = document.get("error_type")
    label = clip_text(error_type, 80) if isinstance(error_type, str) and error_type else "unknown error"
    message = document.get("message")
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


__all__ = [
    "CaseEvidence",
    "CasesEvaluation",
    "RecordedRequest",
    "describe_case",
    "evaluate_cases_report",
    "value_text",
]
