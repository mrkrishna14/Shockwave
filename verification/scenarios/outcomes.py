"""Expected verification outcomes: tables shared by the handoff docs and the tests.

Each row names the evidence a phase produced (a key of PHASE_EVIDENCE) and the status
the interpreter must return. ``tests/analyzer/test_scenarios_interpret.py`` drives
``interpret_verification`` / ``interpret_patched_run`` through every row with synthetic
runner results, and ``render_outcome_table_markdown`` renders the same rows for the
handoff, so the documented table cannot drift from the tested behavior. The case-scenario
table (CASE_PHASE_EVIDENCE / CASE_OUTCOME_TABLE: multi-case Stripe reports with recorded
requests, a relevant case and guard cases, plus controls through ``interpret_control``) is
driven the same way by ``tests/analyzer/test_stripe_scenarios.py``. This module is
documentation only: it never fabricates execution evidence for real results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

InterpretFunction = Literal["interpret_verification", "interpret_patched_run", "interpret_control"]

# Evidence vocabulary for one phase (exit code, tests run, scenario report).
PHASE_EVIDENCE: dict[str, str] = {
    "not_executed": "not executed",
    "pass": "exit 0, 1 test; report passed (expected $10.00, actual $10.00)",
    "relevant_fail": "exit 1, 1 test; report failed (expected $10.00, actual $0.10)",
    "infrastructure_error": "runner infrastructure error (e.g. no sandbox available); no exit code, no report",
    "timeout": "timed out; no exit code, no report",
    "zero_tests": "exit 0 but 0 tests ran; no report",
    "missing_dependency": "exit 1, 1 test; report error ModuleNotFoundError (an ImportError) before the comparison",
    "syntax_error": "exit 1, 1 test; report error SyntaxError before the comparison",
    "unrelated_failure": "exit 1, 2 tests; report passed (the relevant assertion held, another test failed)",
    "missing_report": "exit 1, 1 test; no report written",
    "unparseable_report": "exit 1, 1 test; report is not valid JSON",
    "oversized_report": "exit 1, 1 test; report larger than 64 KiB",
    "tampered_expectation": "exit 0, 1 test; report passed with expected $0.10, actual $0.10 (expectation edited)",
    "wrong_fixture": "exit 1, 1 test; report failed but names the other phase's fixture",
    "modified_harness": "exit 1, 1 test; report failed but harness_sha256 differs from the catalog harness",
    "failed_report_exit_zero": "exit 0, 1 test; report failed (contradicts the native runner)",
    "no_provider_request": "exit 0, 1 test; report passed but no request to GET /v1/charges/{id} was recorded",
}


@dataclass(frozen=True)
class OutcomeRow:
    case: str
    baseline: str | None  # PHASE_EVIDENCE key; None: not applicable (patched validation)
    proposed_or_patched: str  # PHASE_EVIDENCE key
    expected_status: str
    which_function: InterpretFunction
    why: str


_V: InterpretFunction = "interpret_verification"
_P: InterpretFunction = "interpret_patched_run"

OUTCOME_TABLE: tuple[OutcomeRow, ...] = (
    OutcomeRow(
        "old pass / new relevant fail", "pass", "relevant_fail", "verified_regression", _V,
        "baseline passed and the relevant assertion failed under the proposed contract",
    ),
    OutcomeRow(
        "both pass", "pass", "pass", "compatible_within_tested_scope", _V,
        "a passing scenario proves only its tested scope",
    ),
    OutcomeRow(
        "baseline fail", "relevant_fail", "relevant_fail", "inconclusive", _V,
        "a failing baseline proves nothing about the proposed contract",
    ),
    OutcomeRow(
        "baseline fail, new pass", "relevant_fail", "pass", "inconclusive", _V,
        "a failing baseline proves nothing about the proposed contract",
    ),
    OutcomeRow(
        "baseline broken (missing dependency)", "missing_dependency", "relevant_fail", "inconclusive", _V,
        "a broken baseline proves nothing about the proposed contract",
    ),
    OutcomeRow(
        "baseline not executed", "not_executed", "relevant_fail", "inconclusive", _V,
        "a regression needs a passing baseline",
    ),
    OutcomeRow(
        "infrastructure error", "pass", "infrastructure_error", "inconclusive", _V,
        "failed infrastructure establishes nothing about compatibility",
    ),
    OutcomeRow("timeout", "pass", "timeout", "inconclusive", _V, "an unfinished run proves nothing"),
    OutcomeRow("zero tests", "pass", "zero_tests", "inconclusive", _V, "success without tests proves nothing"),
    OutcomeRow(
        "missing dependency", "pass", "missing_dependency", "inconclusive", _V,
        "the relevant comparison never ran",
    ),
    OutcomeRow("syntax error", "pass", "syntax_error", "inconclusive", _V, "the relevant comparison never ran"),
    OutcomeRow(
        "unrelated failure", "pass", "unrelated_failure", "inconclusive", _V,
        "a non-zero exit is not a relevant assertion failure",
    ),
    OutcomeRow(
        "missing report", "pass", "missing_report", "inconclusive", _V,
        "without the report the failing assertion cannot be identified",
    ),
    OutcomeRow(
        "unparseable report", "pass", "unparseable_report", "inconclusive", _V,
        "without the report the failing assertion cannot be identified",
    ),
    OutcomeRow(
        "oversized report", "pass", "oversized_report", "inconclusive", _V,
        "reports are bounded to 64 KiB",
    ),
    OutcomeRow(
        "expectation tampering", "pass", "tampered_expectation", "inconclusive", _V,
        "the invariant stays $10.00; a test is never repaired by changing its expectation",
    ),
    OutcomeRow(
        "wrong fixture for phase", "pass", "wrong_fixture", "inconclusive", _V,
        "the proposed phase must run the proposed-contract fixture",
    ),
    OutcomeRow(
        "modified harness", "pass", "modified_harness", "inconclusive", _V,
        "only the catalog harness identifies the relevant assertion",
    ),
    OutcomeRow(
        "failed report but exit 0", "pass", "failed_report_exit_zero", "inconclusive", _V,
        "report and native runner disagree",
    ),
    OutcomeRow(
        "provider response never requested", "pass", "no_provider_request", "inconclusive", _V,
        "the run did not exercise the provider contract",
    ),
    OutcomeRow(
        "proposed not executed", "pass", "not_executed", "inconclusive", _V,
        "the proposed contract was never exercised",
    ),
    OutcomeRow(
        "nothing executed", "not_executed", "not_executed", "not_run", _V,
        "no execution evidence: the risk stays a prediction",
    ),
    OutcomeRow(
        "patched new pass", None, "pass", "validated", _P,
        "the patch meets the unchanged $10.00 expectation on the proposed contract (tested scope only); it is "
        "pinned to the proposed contract, not backward compatible with current-contract responses unless made "
        "version-aware, so its deployment must be coordinated with the provider's cutover",
    ),
    OutcomeRow(
        "patched still failing", None, "relevant_fail", "validation_failed", _P,
        "the relevant assertion still fails on the proposed contract",
    ),
    OutcomeRow(
        "patched expectation tampering", None, "tampered_expectation", "inconclusive", _P,
        "a test repaired by expecting $0.10 validates nothing",
    ),
    OutcomeRow(
        "patched missing dependency", None, "missing_dependency", "inconclusive", _P,
        "the relevant comparison never ran",
    ),
    OutcomeRow("patched timeout", None, "timeout", "inconclusive", _P, "an unfinished run proves nothing"),
    OutcomeRow(
        "patched not executed", None, "not_executed", "inconclusive", _P,
        "nothing validated the patch",
    ),
)


# Case scenarios (Stripe): evidence of one phase of a multi-case run. "Preconditions" are
# the recorded-request checks: every case requested exactly its own GET resources, with a
# Stripe-Version header naming the fixture set's API version and a test-mode key.
CASE_PHASE_EVIDENCE: dict[str, str] = {
    "not_executed": "not executed",
    "pass": "exit 0, 1 test; every case passed; preconditions held",
    "relevant_fail": (
        "exit 1, 1 test; the relevant case failed (expected needs_manual_review=True, actual False), every guard "
        "case passed; preconditions held"
    ),
    "guard_fail": "exit 1, 1 test; a guard case failed (expected needs_manual_review=False, actual True), the relevant case passed",
    "relevant_and_guard_fail": "exit 1, 1 test; the relevant case and a guard case failed",
    "case_error": "exit 1, 1 test; one case raised before its comparison (e.g. the SDK raised on a 404)",
    "harness_error": "exit 1, 1 test; report error ModuleNotFoundError before any case ran",
    "infrastructure_error": "runner infrastructure error (e.g. no sandbox available); no exit code, no report",
    "timeout": "timed out; no exit code, no report",
    "zero_tests": "exit 0 but 0 tests ran; no report",
    "missing_report": "exit 1, 1 test; no report written",
    "unparseable_report": "exit 1, 1 test; report is not valid JSON",
    "oversized_report": "exit 1, 1 test; report larger than 64 KiB",
    "tampered_expectation": "exit 0, 1 test; every case passed, but the relevant case expects the buggy value",
    "wrong_fixture": "exit 0, 1 test; every case passed, but the report names the other phase's fixture set",
    "modified_harness": "exit 0, 1 test; every case passed, but harness_sha256 differs from the catalog harness",
    "tampered_binding": "exit 0, 1 test; every case passed, but binding_sha256 differs from the catalog binding",
    "fixture_integrity": "exit 0, 1 test; every case passed, but fixture_sha256 differs from the canonical fixture file",
    "no_request": "exit 0, 1 test; every case passed, but one case's call made no request",
    "wrong_path": "exit 0, 1 test; every case passed, but a request targeted another resource or path",
    "missing_stripe_version": "exit 0, 1 test; every case passed, but a request carried no Stripe-Version header",
    "live_mode_auth": "exit 0, 1 test; every case passed, but a request was not authenticated with a test-mode key",
    "unattributed_request": "exit 0, 1 test; every case passed, but a request arrived outside every case's call",
    "failed_report_exit_zero": "exit 0, 1 test; a case failed (contradicts the native runner)",
    "passed_report_exit_nonzero": "exit 1, 1 test; every case passed (another failure)",
    "control_fail": "exit 1, 1 test; the control case failed; preconditions held",
}

_C: InterpretFunction = "interpret_control"

CASE_OUTCOME_TABLE: tuple[OutcomeRow, ...] = (
    OutcomeRow(
        "old pass / new relevant fail (guards pass)", "pass", "relevant_fail", "verified_regression", _V,
        "baseline passed and only the relevant case failed under the proposed contract, with every precondition held",
    ),
    OutcomeRow("both pass", "pass", "pass", "compatible_within_tested_scope", _V, "a passing scenario proves only its tested scope"),
    OutcomeRow(
        "baseline relevant fail", "relevant_fail", "relevant_fail", "inconclusive", _V,
        "a failing baseline proves nothing about the proposed contract",
    ),
    OutcomeRow(
        "baseline guard fail", "guard_fail", "relevant_fail", "inconclusive", _V,
        "a failing baseline proves nothing about the proposed contract",
    ),
    OutcomeRow(
        "baseline precondition (no request)", "no_request", "relevant_fail", "inconclusive", _V,
        "a baseline that never exercised the SDK call is no baseline",
    ),
    OutcomeRow(
        "baseline not executed", "not_executed", "relevant_fail", "inconclusive", _V, "a regression needs a passing baseline",
    ),
    OutcomeRow(
        "guard failure", "pass", "guard_fail", "inconclusive", _V,
        "a failing guard is an unrelated failure, not the isolated relevant failure",
    ),
    OutcomeRow(
        "relevant and guard failure", "pass", "relevant_and_guard_fail", "inconclusive", _V,
        "the relevant failure is not isolated when a guard fails too",
    ),
    OutcomeRow("case error", "pass", "case_error", "inconclusive", _V, "a case whose comparison never ran proves nothing"),
    OutcomeRow("harness error", "pass", "harness_error", "inconclusive", _V, "no case ran"),
    OutcomeRow(
        "infrastructure error", "pass", "infrastructure_error", "inconclusive", _V,
        "failed infrastructure establishes nothing about compatibility",
    ),
    OutcomeRow("timeout", "pass", "timeout", "inconclusive", _V, "an unfinished run proves nothing"),
    OutcomeRow("zero tests", "pass", "zero_tests", "inconclusive", _V, "success without tests proves nothing"),
    OutcomeRow(
        "missing report", "pass", "missing_report", "inconclusive", _V,
        "without the report the failing case cannot be identified",
    ),
    OutcomeRow(
        "unparseable report", "pass", "unparseable_report", "inconclusive", _V,
        "without the report the failing case cannot be identified",
    ),
    OutcomeRow("oversized report", "pass", "oversized_report", "inconclusive", _V, "reports are bounded to 64 KiB"),
    OutcomeRow(
        "expectation tampering", "pass", "tampered_expectation", "inconclusive", _V,
        "a test is never repaired by changing its expectation",
    ),
    OutcomeRow(
        "wrong fixture for phase", "pass", "wrong_fixture", "inconclusive", _V,
        "the proposed phase must run the proposed fixture set (identity mismatch)",
    ),
    OutcomeRow(
        "modified harness", "pass", "modified_harness", "inconclusive", _V,
        "only the catalog harness identifies the cases (integrity mismatch)",
    ),
    OutcomeRow(
        "tampered binding", "pass", "tampered_binding", "inconclusive", _V,
        "only the catalog binding defines the cases (integrity mismatch)",
    ),
    OutcomeRow(
        "fixture integrity mismatch", "pass", "fixture_integrity", "inconclusive", _V,
        "the staged fixture must be the canonical fixture revision",
    ),
    OutcomeRow(
        "no request (precondition)", "pass", "no_request", "inconclusive", _V,
        "no evidence that the application invoked the SDK method",
    ),
    OutcomeRow(
        "wrong request (precondition)", "pass", "wrong_path", "inconclusive", _V,
        "no evidence that the request targeted the expected operation and resource",
    ),
    OutcomeRow(
        "missing Stripe-Version (precondition)", "pass", "missing_stripe_version", "inconclusive", _V,
        "no evidence of the API version the fixture set simulates",
    ),
    OutcomeRow(
        "non-test-mode auth (precondition)", "pass", "live_mode_auth", "inconclusive", _V,
        "offline runs must authenticate with the dummy test-mode key",
    ),
    OutcomeRow(
        "request outside every case (precondition)", "pass", "unattributed_request", "inconclusive", _V,
        "every request must be attributable to a case",
    ),
    OutcomeRow(
        "failed report but exit 0", "pass", "failed_report_exit_zero", "inconclusive", _V, "report and native runner disagree",
    ),
    OutcomeRow(
        "passed report but exit 1", "pass", "passed_report_exit_nonzero", "inconclusive", _V,
        "a non-zero exit with passing cases is an unrelated failure",
    ),
    OutcomeRow("nothing executed", "not_executed", "not_executed", "not_run", _V, "no execution evidence: the risk stays a prediction"),
    OutcomeRow(
        "patched new pass", None, "pass", "validated", _P,
        "every case passes on the proposed contract (tested scope only); the patch is pinned to the proposed contract, "
        "not backward compatible with current-contract responses unless made version-aware, so its deployment must be "
        "coordinated with the provider's cutover",
    ),
    OutcomeRow(
        "patched relevant still failing", None, "relevant_fail", "validation_failed", _P,
        "the relevant case still fails on the proposed contract",
    ),
    OutcomeRow(
        "patched guard failing", None, "guard_fail", "validation_failed", _P,
        "the patch broke a guard case the scenario requires",
    ),
    OutcomeRow("patched case error", None, "case_error", "inconclusive", _P, "a case whose comparison never ran validates nothing"),
    OutcomeRow(
        "patched expectation tampering", None, "tampered_expectation", "inconclusive", _P,
        "a test repaired by expecting the buggy value validates nothing",
    ),
    OutcomeRow("patched no request", None, "no_request", "inconclusive", _P, "no evidence the SDK call ran"),
    OutcomeRow("patched timeout", None, "timeout", "inconclusive", _P, "an unfinished run proves nothing"),
    OutcomeRow("patched not executed", None, "not_executed", "inconclusive", _P, "nothing validated the patch"),
    OutcomeRow("control pass", None, "pass", "passed", _C, "the unaffected path passed against this fixture variant"),
    OutcomeRow("control fail", None, "control_fail", "failed", _C, "the unaffected path failed against this fixture variant"),
    OutcomeRow(
        "control no request", None, "no_request", "inconclusive", _C, "no evidence the SDK call ran",
    ),
    OutcomeRow("control zero tests", None, "zero_tests", "inconclusive", _C, "success without tests proves nothing"),
    OutcomeRow("control not executed", None, "not_executed", "inconclusive", _C, "nothing ran"),
)


def render_outcome_table_markdown() -> str:
    """OUTCOME_TABLE as a Markdown table (used in the analyzer handoff)."""
    return _render(OUTCOME_TABLE, PHASE_EVIDENCE)


def render_case_outcome_table_markdown() -> str:
    """CASE_OUTCOME_TABLE (multi-case Stripe scenarios and controls) as a Markdown table."""
    return _render(CASE_OUTCOME_TABLE, CASE_PHASE_EVIDENCE)


def _render(table: tuple[OutcomeRow, ...], evidence: dict[str, str]) -> str:
    header = (
        "| Case | Baseline run (current-contract fixture) | Proposed or patched run (proposed fixture) "
        "| Interpreted by | Expected status | Why |"
    )
    lines = [header, "| --- | --- | --- | --- | --- | --- |"]
    for row in table:
        cells = (
            row.case,
            _baseline_cell(row) if row.baseline is None else evidence[row.baseline],
            evidence[row.proposed_or_patched],
            f"`{row.which_function}`",
            f"`{row.expected_status}`",
            row.why,
        )
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def _baseline_cell(row: OutcomeRow) -> str:
    return "n/a (control run, one fixture variant)" if row.which_function == "interpret_control" else "n/a (patched validation)"
