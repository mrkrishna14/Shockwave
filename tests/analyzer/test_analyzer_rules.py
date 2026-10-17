"""Regression tests for analyzer.rules: endpoint removal vs endpoint-wide behavior changes,
threshold and reciprocal-conversion arithmetic, change provenance labels and untrusted text.

Changes and dependencies are built by hand with every optional field explicit, so the rules
are exercised without discovery; one test runs detect_changes to pin the change shapes the
rules rely on (only the structural endpoint removal is a breaking "*" change).
"""

from __future__ import annotations

import pytest

from analyzer.changes import detect_changes
from analyzer.impact import analyze_with_rules
from analyzer.rules import (
    MAX_EXPLANATION_CHARS,
    MISSING_SCENARIO,
    REASON_USAGE_UNCLASSIFIED,
    RULES_LABEL,
    RuleOutcome,
    Unresolved,
    bound_text,
    compose_explanation,
    evaluate_dependency,
    match_unresolved_reason,
)
from discovery.contracts import APIChange, Dependency, DiscoveryResult
from discovery.ids import endpoint_id, stable_id
from discovery.records import operation_pointer
from discovery.text import TRUNCATION_MARK, has_unsafe_characters

SERVICE = "demo-payments"
PROJECT = stable_id("test-rules-project")
OLD_SNAPSHOT = stable_id("test-rules-contract", "old")
NEW_SNAPSHOT = stable_id("test-rules-contract", "new")
CHARGE_PATH = "/v1/charges/{id}"
REFUND_PATH = "/v1/refunds/{id}"
ENDPOINT = endpoint_id(OLD_SNAPSHOT, SERVICE, "GET", CHARGE_PATH)
USD_CENTS = "USD amount in minor currency units (cents)"
USD_DOLLARS = "USD amount in major currency units (dollars)"
CONSISTENT = "refund reads are immediately consistent"
LAGGING = "refund reads may lag by up to 5 seconds"
# Built with chr() so no invisible or control character appears literally in this file.
NEWLINE, TAB, BELL = chr(0x0A), chr(0x09), chr(0x07)
BIDI_OVERRIDE, LINE_SEPARATOR = chr(0x202E), chr(0x2028)


# Builders -----------------------------------------------------------------------------------


def evidence_record(name: str, kind: str, **fields: object) -> dict:
    record: dict = {
        "id": stable_id("test-rules-evidence", name, kind),
        "kind": kind,
        "visibility": "consumer_private" if kind == "source" else "provider_owned",
        "source_uri": f"examples/synthetic/{name}",
        "revision": "synthetic@1",
        "file": None,
        "line_start": None,
        "line_end": None,
        "json_pointer": operation_pointer(CHARGE_PATH, "get") if kind == "openapi" else None,
        "excerpt": None,
    }
    record.update(fields)
    return record


def make_change(
    kind: str,
    *,
    field_path: str = "response.amount",
    old: str = "",
    new: str = "",
    breaking: bool = False,
    evidence_kinds: tuple[str, ...] = ("declared_change",),
) -> APIChange:
    return APIChange.model_validate(
        {
            "id": stable_id("test-rules-change", kind, field_path, old, new, breaking, *evidence_kinds),
            "api_project_id": PROJECT,
            "service_id": SERVICE,
            "endpoint_id": ENDPOINT,
            "old_snapshot_id": OLD_SNAPSHOT,
            "new_snapshot_id": NEW_SNAPSHOT,
            "field_path": field_path,
            "kind": kind,
            "old_semantics": old,
            "new_semantics": new,
            "structural_breaking": breaking,
            "evidence": [evidence_record(f"change-{kind}", item) for item in evidence_kinds],
        }
    )


def make_dependency(
    name: str,
    *,
    operation: str,
    operand: object = None,
    field_path: str = "response.amount",
    destination: str | None = None,
    confidence: str = "direct",
    endpoint: str = ENDPOINT,
    snapshot: str | None = None,
    file: str | None = None,
) -> Dependency:
    path = file if file is not None else f"app/{name}.py"
    return Dependency.model_validate(
        {
            "id": stable_id("test-rules-dependency", name),
            "consumer_org_id": stable_id("test-rules-org"),
            "enrollment_id": stable_id("test-rules-enrollment"),
            "repository_snapshot_id": stable_id("test-rules-snapshot", snapshot or name),
            "service_id": SERVICE,
            "endpoint_id": endpoint,
            "language": "python",
            "field_path": field_path,
            "origin": {"kind": "http_response", "library": "requests", "call": "requests.get"},
            "usage": {"operation": operation, "operand": operand, "destination": destination},
            "confidence": confidence,
            "evidence": [
                evidence_record(
                    f"dependency-{name}",
                    "source",
                    source_uri=f"repository-snapshot://snapshot/{name}",
                    file=path,
                    line_start=7,
                    line_end=7,
                    excerpt=f"# usage {name}",
                )
            ],
        }
    )


def outcome_of(change: APIChange, dependency: Dependency) -> RuleOutcome:
    outcome = evaluate_dependency(change, dependency)
    assert isinstance(outcome, RuleOutcome), outcome
    return outcome


def amount_schema(amount_type: str) -> dict:
    return {"type": "object", "properties": {"amount": {"type": amount_type}}}


def contract_snapshot(snapshot_id: str, responses: dict[str, dict]) -> DiscoveryResult:
    """A fully read contract snapshot: GET endpoints documented by openapi evidence, no gaps."""
    endpoints = [
        {
            "id": endpoint_id(snapshot_id, SERVICE, "GET", path),
            "service_id": SERVICE,
            "method": "GET",
            "path": path,
            "snapshot_id": snapshot_id,
            "operation_id": None,
            "request_schema": {},
            "response_schema": response,
            "evidence": [
                evidence_record(
                    f"spec-{snapshot_id}-{path}",
                    "openapi",
                    revision=f"sha256:{snapshot_id}",
                    json_pointer=operation_pointer(path, "get"),
                    excerpt=f"GET {path}",
                )
            ],
            "reconciliation": "documented_and_found",
            "confidence": "direct",
        }
        for path, response in responses.items()
    ]
    return DiscoveryResult.model_validate(
        {
            "schema_version": "shockwave.v1",
            "service_id": SERVICE,
            "snapshot_id": snapshot_id,
            "endpoints": endpoints,
            "coverage": {"files_scanned": 0, "files_skipped": 0, "unsupported_patterns": []},
            "warnings": [],
        }
    )


# Endpoint-wide behavior changes (F07) -------------------------------------------------------


def test_endpoint_wide_behavior_change_on_a_kept_endpoint_is_not_a_removal():
    change = make_change("behavior_changed", field_path="*", old=CONSISTENT, new=LAGGING)
    usages = [
        (make_dependency("divide", operation="divide", operand=100), "conversion"),
        (make_dependency("status", operation="read", field_path="response.status", confidence="inferred"), "read"),
        (make_dependency("limit", operation="compare", operand=500), "comparison"),
        (make_dependency("ledger", operation="store", destination="db.amount"), "storage"),
    ]
    for dependency, category in usages:
        outcome = outcome_of(change, dependency)
        assert (outcome.category, outcome.severity, outcome.confidence) == (category, "medium", "inferred")
        assert "removes" not in outcome.explanation and "would fail" not in outcome.explanation
        assert "removed" not in outcome.suggested_action
        assert f"{dependency.field_path} from an endpoint whose documented behavior changes" in outcome.explanation
        assert f"Declared change: {CONSISTENT} -> {LAGGING}." in outcome.explanation
        assert MISSING_SCENARIO in outcome.missing_evidence and len(outcome.missing_evidence) == 2


def test_endpoint_wide_behavior_change_leaves_opaque_usages_unresolved():
    change = make_change("behavior_changed", field_path="*", old=CONSISTENT, new=LAGGING)
    forward = make_dependency("forward", operation="pass_through", field_path="response")
    unknown = make_dependency("unknown", operation="unknown")
    read = make_dependency("read", operation="read")
    for dependency in (forward, unknown):
        assert match_unresolved_reason(change, dependency) == REASON_USAGE_UNCLASSIFIED
        assert evaluate_dependency(change, dependency) == Unresolved(REASON_USAGE_UNCLASSIFIED)

    result = analyze_with_rules(change, [forward, unknown, read])
    assert sorted(map(str, result.unresolved_dependency_ids)) == sorted(str(item.id) for item in (forward, unknown))
    assert [(risk.severity, risk.confidence) for risk in result.risks] == [("medium", "inferred")]
    assert "removes" not in result.risks[0].explanation


def test_structural_endpoint_removal_is_high_for_every_usage_on_the_endpoint():
    change = make_change(
        "behavior_changed",
        field_path="*",
        old="endpoint documented in the current contract",
        new="endpoint absent from the proposed contract",
        breaking=True,
        evidence_kinds=("openapi",),
    )
    expected = [
        (make_dependency("divide", operation="divide", operand=100), ("conversion", "high", "direct")),
        (
            make_dependency("status", operation="read", field_path="response.status", confidence="inferred"),
            ("read", "high", "inferred"),
        ),
        (make_dependency("forward", operation="pass_through", field_path="response"), ("read", "high", "direct")),
        (make_dependency("unknown", operation="unknown"), ("read", "high", "direct")),
    ]
    for dependency, triple in expected:
        assert match_unresolved_reason(change, dependency) is None
        outcome = outcome_of(change, dependency)
        assert (outcome.category, outcome.severity, outcome.confidence) == triple
        assert "from an endpoint the proposed contract removes; this call would fail." in outcome.explanation
        assert "Contract change: endpoint documented in the current contract -> endpoint absent" in (
            outcome.explanation
        )


def test_declared_removal_merged_with_the_structural_removal_stays_a_removal():
    # detect_changes appends "; structural: <observed>" to a merged declaration's own semantics (when
    # not already stated); the wording never marks the removal, only structural_breaking does.
    change = make_change(
        "behavior_changed",
        field_path="*",
        old="refunds can be retrieved by id",
        new="refund retrieval is retired",
        breaking=True,
        evidence_kinds=("declared_change", "openapi"),
    )
    outcome = outcome_of(change, make_dependency("refund", operation="divide", operand=100))
    assert (outcome.severity, outcome.confidence) == ("high", "direct")
    assert "removes" in outcome.explanation
    assert "Declared change: refunds can be retrieved by id -> refund retrieval is retired." in outcome.explanation


def test_detect_changes_shapes_drive_removal_and_provenance_labels():
    old = contract_snapshot(OLD_SNAPSHOT, {CHARGE_PATH: amount_schema("number"), REFUND_PATH: amount_schema("number")})
    new = contract_snapshot(NEW_SNAPSHOT, {CHARGE_PATH: amount_schema("string")})
    detected = detect_changes(old, new, [], api_project_id=PROJECT)
    changes = {(change.field_path, change.kind): change for change in detected}
    retyped, removal = changes[("response.amount", "type_changed")], changes[("*", "behavior_changed")]
    assert removal.structural_breaking

    charge = make_dependency("charge", operation="divide", operand=100, endpoint=str(retyped.endpoint_id))
    typed = outcome_of(retyped, charge)
    assert f"Contract change: {retyped.old_semantics} -> {retyped.new_semantics}." in typed.explanation
    assert "Declared change" not in typed.explanation

    refund = make_dependency("refund", operation="read", endpoint=str(removal.endpoint_id))
    removed = outcome_of(removal, refund)
    assert (removed.severity, removed.confidence) == ("high", "direct")
    assert "removes" in removed.explanation and "Contract change:" in removed.explanation
    assert "Declared change" not in removed.explanation


# Threshold comparisons (F27) ------------------------------------------------------------------


@pytest.mark.parametrize("operand", [0, 0.0, -0.0, "0", "-0", "0.00"])
@pytest.mark.parametrize(("old", "new"), [(USD_CENTS, USD_DOLLARS), (USD_DOLLARS, USD_CENTS)])
def test_comparison_with_zero_is_unchanged_by_rescaling(operand, old, new):
    change = make_change("units_changed", old=old, new=new)
    outcome = outcome_of(change, make_dependency("guard", operation="compare", operand=operand))
    assert (outcome.category, outcome.severity, outcome.confidence) == ("comparison", "low", "inferred")
    assert outcome.explanation.startswith("app/guard.py:7 compares response.amount with 0. ")
    assert "same result under the proposed contract" in outcome.explanation
    text = f"{outcome.explanation} {outcome.suggested_action}"
    assert "100x" not in text and "rescale it" not in text and "->" not in text and "$" not in text


@pytest.mark.parametrize(
    ("old", "new", "operand", "fragments"),
    [
        (
            USD_CENTS,
            USD_DOLLARS,
            -1,
            ("in cents ($-0.01)", "triggers at $-1.00 (100x further below zero)", "rescale it (-1 -> -0.01)"),
        ),
        (
            USD_DOLLARS,
            USD_CENTS,
            -500,
            ("in dollars ($-500.00)", "triggers at $-5.00 (100x closer to zero)", "rescale it (-500 -> -50000)"),
        ),
        (
            USD_CENTS,
            USD_DOLLARS,
            50000,
            ("in cents ($500.00)", "triggers at $50,000.00 (100x higher)", "rescale it (50000 -> 500)"),
        ),
        (
            USD_DOLLARS,
            USD_CENTS,
            500,
            ("in dollars ($500.00)", "triggers at $5.00 (100x lower)", "rescale it (500 -> 50000)"),
        ),
    ],
)
def test_threshold_shift_wording_follows_the_sign_of_the_threshold(old, new, operand, fragments):
    change = make_change("units_changed", old=old, new=new)
    outcome = outcome_of(change, make_dependency("limit", operation="compare", operand=operand))
    assert (outcome.category, outcome.severity, outcome.confidence) == ("comparison", "high", "inferred")
    text = f"{outcome.explanation} {outcome.suggested_action}"
    for fragment in fragments:
        assert fragment in text
    if operand < 0:
        assert "higher" not in text and "lower" not in text


def test_zero_guard_does_not_displace_a_real_threshold_in_the_same_snapshot():
    change = make_change("units_changed", old=USD_CENTS, new=USD_DOLLARS)
    guard = make_dependency("a_guard", operation="compare", operand=0, snapshot="shared")
    limit = make_dependency("fraud", operation="compare", operand=50000, snapshot="shared")
    result = analyze_with_rules(change, [guard, limit])
    [risk] = result.risks
    assert (risk.severity, risk.confidence) == ("high", "inferred")
    assert risk.explanation.startswith("app/fraud.py:7 compares response.amount with 50000. ")
    assert "rescale it (50000 -> 500)" in risk.suggested_action
    assert "(0 -> 0)" not in risk.explanation + risk.suggested_action


# Reciprocal conversions (F28) -----------------------------------------------------------------


@pytest.mark.parametrize("operand", [0.01, "0.01", "1e-2", " 1E-2 "])
def test_multiplying_by_the_reciprocal_is_the_exact_minor_to_major_conversion(operand):
    change = make_change("units_changed", old=USD_CENTS, new=USD_DOLLARS)
    outcome = outcome_of(change, make_dependency("display", operation="multiply", operand=operand))
    assert (outcome.category, outcome.severity, outcome.confidence) == ("conversion", "high", "direct")
    assert outcome.explanation.startswith(
        "app/display.py:7 multiplies response.amount by 0.01 to convert cents to dollars. "
    )
    assert "the result becomes $0.10 instead of $10.00 (100x too small)" in outcome.explanation
    assert "divides" not in outcome.explanation and "uncertain" not in outcome.explanation
    assert outcome.suggested_action.startswith("Remove the *0.01 conversion at app/display.py:7 ")


def test_dividing_by_the_reciprocal_is_the_exact_major_to_minor_conversion():
    change = make_change("units_changed", old=USD_DOLLARS, new=USD_CENTS)
    outcome = outcome_of(change, make_dependency("to_cents", operation="divide", operand=0.01, confidence="inferred"))
    assert (outcome.category, outcome.severity, outcome.confidence) == ("conversion", "high", "inferred")
    assert "divides response.amount by 0.01 to convert dollars to cents." in outcome.explanation
    assert "the result becomes $1,000.00 instead of $10.00 (100x too large)" in outcome.explanation
    assert outcome.suggested_action.startswith("Remove the /0.01 conversion at app/to_cents.py:7 ")


def test_literal_factor_conversion_wording_is_unchanged():
    change = make_change("units_changed", old=USD_CENTS, new=USD_DOLLARS)
    outcome = outcome_of(change, make_dependency("display", operation="divide", operand="100"))
    assert outcome.explanation == (
        "app/display.py:7 divides response.amount by 100 to convert cents to dollars. Under the proposed "
        "contract amount is already in dollars (10 for $10.00), so the result becomes $0.10 instead of $10.00 "
        "(100x too small)."
    )
    assert outcome.suggested_action.startswith("Remove the /100 conversion at app/display.py:7 for the proposed")


@pytest.mark.parametrize(
    ("operation", "operand"),
    [
        ("multiply", 100),
        ("divide", 1000),
        ("divide", 0.01),
        ("multiply", -0.01),
        ("multiply", 0.011),
        ("multiply", 0),
        ("divide", 0),
    ],
)
def test_other_literal_arithmetic_stays_an_uncertain_conversion(operation, operand):
    change = make_change("units_changed", old=USD_CENTS, new=USD_DOLLARS)
    outcome = outcome_of(change, make_dependency("odd", operation=operation, operand=operand))
    assert (outcome.category, outcome.severity, outcome.confidence) == ("conversion", "medium", "inferred")
    assert "but this is not the /100 conversion the current contract implies" in outcome.explanation
    assert "uncertain" in outcome.explanation


def test_symbolic_operand_is_not_claimed_to_differ_from_the_conversion():
    change = make_change("units_changed", old=USD_CENTS, new=USD_DOLLARS)
    outcome = outcome_of(change, make_dependency("named", operation="divide", operand="CENTS_PER_DOLLAR"))
    assert (outcome.category, outcome.severity, outcome.confidence) == ("conversion", "medium", "inferred")
    assert "divides response.amount by CENTS_PER_DOLLAR." in outcome.explanation
    assert "is not a literal number" in outcome.explanation and "uncertain" in outcome.explanation
    assert "this is not the /100 conversion" not in outcome.explanation


# Change provenance labels (F43, F06) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "evidence_kinds", "label"),
    [
        ("type_changed", ("openapi",), "Contract change"),
        ("type_changed", ("declared_change", "openapi"), "Declared change"),
        ("field_removed", ("openapi",), "Contract change"),
        ("enum_changed", ("openapi",), "Contract change"),
        ("behavior_changed", ("openapi",), "Contract change"),
        ("behavior_changed", ("docs",), "Contract change"),
        ("behavior_changed", ("declared_change",), "Declared change"),
    ],
)
def test_semantics_are_labeled_declared_only_with_declared_change_evidence(kind, evidence_kinds, label):
    change = make_change(kind, old="before", new="after", breaking=True, evidence_kinds=evidence_kinds)
    outcome = outcome_of(change, make_dependency("usage", operation="compare", operand=5))
    assert f" {label}: before -> after." in outcome.explanation
    other = "Contract change" if label == "Declared change" else "Declared change"
    assert other not in outcome.explanation


# Untrusted text (F33) -------------------------------------------------------------------------


def test_bound_text_renders_one_clean_line():
    text = f"line one{NEWLINE}line{TAB}two {BELL}bell {BIDI_OVERRIDE}override{LINE_SEPARATOR}tail"
    assert bound_text(text, 200) == ("line one line two bell override tail", False)
    clipped, truncated = bound_text(text + " x" * 100, 30)
    assert truncated and len(clipped) <= 30 and clipped.endswith(TRUNCATION_MARK)
    assert not has_unsafe_characters(clipped)


def test_composed_explanation_is_bounded_clean_and_keeps_the_label():
    core = f"{BIDI_OVERRIDE}word " * 400
    explanation, truncated = compose_explanation(core, RULES_LABEL, [f"a{BELL}", "a ", "b"])
    assert truncated and len(explanation) <= MAX_EXPLANATION_CHARS
    assert explanation.endswith(f"{TRUNCATION_MARK} {RULES_LABEL} Missing evidence: a; b.")
    assert not has_unsafe_characters(explanation)


def test_change_semantics_and_locations_reach_risks_without_control_or_bidi_characters():
    change = make_change(
        "behavior_changed", old=f"cents{BELL} {BIDI_OVERRIDE}srallod", new=f"rounded{LINE_SEPARATOR}half-even"
    )
    outcome = outcome_of(change, make_dependency("read", operation="read"))
    assert "Declared change: cents srallod -> rounded half-even." in outcome.explanation

    hidden = make_dependency("hidden", operation="read", file=f"app/{BIDI_OVERRIDE}fdp.py")
    [risk] = analyze_with_rules(change, [hidden]).risks
    assert not has_unsafe_characters(risk.explanation) and not has_unsafe_characters(risk.suggested_action)
    assert "app/ fdp.py:7" in risk.explanation and "app/ fdp.py:7" in risk.suggested_action


def test_a_long_merged_declaration_keeps_its_structural_observation_visible():
    declared = "amount is rounded half-up to the nearest cent before it is returned to every caller"
    change = make_change(
        "behavior_changed",
        old=f"{declared}; structural: always present (required)",
        new=f"{declared}; structural: may be absent (optional)",
    )
    outcome = outcome_of(change, make_dependency("show", operation="read"))
    assert "; structural: always present (required) -> " in outcome.explanation
    assert outcome.explanation.count("; structural: may be absent (optional)") == 1
