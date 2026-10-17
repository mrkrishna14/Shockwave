"""Deterministic impact rules: how one matched consumer dependency is affected by one APIChange.

Rules are pure functions of (change, dependency). They read only the change record (kind,
semantics, structural_breaking, evidence kinds) and the dependency's normalized usage and
evidence locations; consumer code is never read or executed.
Arithmetic ("conversion"), threshold comparison ("comparison"), storage ("storage") and
plain reads ("read") are separate categories because they fail differently: an exact
divide-by-100 conversion has a computable consequence (100x too small) that an executable
scenario can prove, a threshold comparison shifts silently, and a storage sink is only a
potential mixed-units risk without schema/history evidence. Whatever the evidence cannot
decide is ``Unresolved``: unknown, never safe.

Rule table ("direct*" = direct when the dependency itself is direct, else inferred):

- every kind: dependency confidence "unresolved" -> unresolved; a dependency on an enclosing
  object of the changed field (e.g. pass-through of ``response``) -> unresolved, because the
  use of the changed field is not visible.
- units_changed, minor<->major with a known factor F: the conversion the current contract
  needs (divide by F or multiply by 1/F for minor->major, multiply by F or divide by 1/F for
  major->minor) -> conversion/high/direct*; any other arithmetic -> conversion/medium/
  inferred; compare with 0 -> comparison/low/inferred (a positive rescale keeps zero and
  every sign); any other compare -> comparison/high/inferred; store -> storage/medium/
  inferred; read/pass_through/unknown -> unresolved.
  An unclassifiable direction or unknown factor leaves every dependency unresolved.
- field_removed: every usage -> high/direct*, category by usage.
- type_changed: breaking -> arithmetic/compare high, others medium; non-breaking -> low
  (always inferred).
- enum_changed: breaking -> compare medium, others low; non-breaking -> low (inferred).
- behavior_changed: endpoint removal ("*" with structural_breaking, which detect_changes sets
  only when the endpoint is absent or undocumented in the proposed contract) -> high/direct*,
  category by usage, for every usage; any other behavior change, endpoint-wide or on a field:
  read/arithmetic/compare/store -> medium/inferred, pass_through/unknown -> unresolved.

Explanations are predictions: they cite the dependency's evidence location and never claim
that anything ran. A change's semantics are quoted as "Declared change" only when the change
carries declared_change evidence, otherwise as "Contract change". ``bound_text`` and
``compose_explanation`` render text through ``discovery.text``, so control, line-separator
and bidi characters never reach an explanation or action.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, cast

from analyzer.fieldpaths import (
    ENDPOINT_WIDE,
    ITEMS,
    normalize_field_path,
    relation,
    split_field_path,
)
from analyzer.units import (
    MINOR_UNITS_PER_MAJOR,
    AmountUnit,
    classify_amount_unit,
    currency_of,
    minor_to_major_factor,
)
from discovery.contracts import APIChange, Dependency
from discovery.text import clean_text, clip_text, clip_text_flagged

Category = Literal["conversion", "comparison", "storage", "read"]
Severity = Literal["critical", "high", "medium", "low", "unknown"]
Confidence = Literal["direct", "inferred", "unresolved"]

CATEGORIES: tuple[Category, ...] = ("conversion", "comparison", "storage", "read")
SEVERITY_ORDER: tuple[Severity, ...] = ("unknown", "low", "medium", "high", "critical")
CONFIDENCE_ORDER: tuple[Confidence, ...] = ("unresolved", "inferred", "direct")
USAGE_CATEGORY: dict[str, Category] = {
    "divide": "conversion",
    "multiply": "conversion",
    "compare": "comparison",
    "store": "storage",
    "read": "read",
    "pass_through": "read",
    "unknown": "read",
}
_USAGE_VERB = {
    "divide": "divides",
    "multiply": "multiplies",
    "compare": "compares",
    "store": "stores",
    "read": "reads",
    "pass_through": "passes through",
    "unknown": "uses",
}

MAX_EXPLANATION_CHARS = 600
MAX_ACTION_CHARS = 300
MAX_MISSING_EVIDENCE_CHARS = 220
# Illustration amount (major units) that makes the arithmetic concrete; it matches the demo
# fixtures' $10.00 charge. Every other number in an explanation is derived from the factor.
ILLUSTRATIVE_MAJOR_AMOUNT = 10
RULES_LABEL = "Prediction from code evidence; not executed."
STRUCTURAL_MARKER = "; structural: "  # detect_changes' join for merged declared changes
# A storage sink under a units change is a potential mixed-units risk, never confirmed
# corruption: the rules and model paths share this caveat and missing evidence.
STORAGE_CAVEAT = (
    "Potential risk, not confirmed: the destination's units, historical records and any migration plan were "
    "not examined."
)
STORAGE_MISSING_EVIDENCE = ("units/schema of the destination", "historical records", "migration plan")
MISSING_SCENARIO = "an executable scenario for this usage"
MISSING_COMPARISON_SCENARIO = "an executable comparison scenario"
# Missing-evidence items that name an absent scenario: dropped from a Risk whose dependencies a
# catalog scenario is bound to (the scenario exists; only its execution is outstanding).
SCENARIO_MISSING_EVIDENCE = frozenset({MISSING_SCENARIO, MISSING_COMPARISON_SCENARIO})

REASON_UNRESOLVED_DEPENDENCY = "the scanner could not resolve the dependency's origin or usage"
REASON_ENCLOSING_OBJECT = "it consumes an enclosing object, so use of the changed field is not visible"
REASON_USAGE_HIDES_UNIT = (
    "its usage (read, pass-through or unknown) does not show how the value's unit is interpreted"
)
REASON_USAGE_UNCLASSIFIED = "its usage (pass-through or unknown) cannot be classified against the behavior change"
REASON_NO_DIRECTION = "the declared units change does not state a classifiable minor/major direction"
REASON_NO_CURRENCY = "the change does not name a single currency, so the minor-to-major factor is unknown"


@dataclass(frozen=True)
class RuleOutcome:
    """A predicted effect of a change on one dependency."""

    category: Category
    severity: Severity
    confidence: Confidence
    explanation: str
    suggested_action: str
    missing_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unresolved:
    """The evidence does not decide the effect; ``reason`` is generic (no paths or code)."""

    reason: str


@dataclass(frozen=True)
class UnitsContext:
    old_unit: AmountUnit
    new_unit: AmountUnit
    currency: str
    factor: int

    @property
    def conversion_operation(self) -> Literal["divide", "multiply"]:
        """The arithmetic the current contract needs to reach the other unit."""
        return "divide" if self.old_unit == "minor" else "multiply"


# Rank helpers ------------------------------------------------------------------------------


def strongest_severity(values: Iterable[str]) -> Severity:
    return cast(Severity, max(values, key=SEVERITY_ORDER.index))


def weakest_confidence(values: Iterable[str]) -> Confidence:
    return cast(Confidence, min(values, key=CONFIDENCE_ORDER.index))


def cap_confidence(value: str, cap: str) -> Confidence:
    """``value`` lowered to ``cap`` when it claims more certainty than the cap allows."""
    return cast(Confidence, min(value, cap, key=CONFIDENCE_ORDER.index))


def category_for_usage(operation: str) -> Category:
    return USAGE_CATEGORY.get(operation, "read")


# Text helpers ------------------------------------------------------------------------------


def bound_text(text: str, limit: int) -> tuple[str, bool]:
    """``text`` as one clean line (``discovery.text``) cut to ``limit`` characters; returns (text, truncated)."""
    return clip_text_flagged(text, limit)


def compose_explanation(core: str, label: str, missing_evidence: Iterable[str] = ()) -> tuple[str, bool]:
    """``core`` + honesty label + missing evidence within MAX_EXPLANATION_CHARS.

    Only the core is shortened, so the prediction label always survives. Returns
    (text, core_truncated).
    """
    suffix = f" {label}"
    missing = [item for item in dict.fromkeys(clean_text(entry) for entry in missing_evidence) if item]
    if missing:
        listed, _ = bound_text("Missing evidence: " + "; ".join(missing) + ".", MAX_MISSING_EVIDENCE_CHARS)
        suffix += f" {listed}"
    body, truncated = bound_text(core, MAX_EXPLANATION_CHARS - len(suffix))
    return body + suffix, truncated


def source_location(dependency: Dependency) -> str | None:
    """``file:line`` (or ``file:start-end``) of the dependency's use site.

    Scanner evidence runs origin call -> ... -> use site, so the use site is the last located
    source item (scanner/HANDOFF.md); a single-item chain is its own use site.
    """
    for item in reversed(dependency.evidence):
        if item.kind != "source" or not item.file:
            continue
        if item.line_start is None:
            return item.file
        if item.line_end is not None and item.line_end != item.line_start:
            return f"{item.file}:{item.line_start}-{item.line_end}"
        return f"{item.file}:{item.line_start}"
    return None


def describe_usage(dependency: Dependency) -> str:
    """Short usage label such as ``divide by 100`` (used when listing extra locations)."""
    operation = dependency.usage.operation
    if operation in ("divide", "multiply"):
        return f"{operation} by {_operand_text(dependency.usage.operand)}"
    if operation == "compare":
        return f"compare with {_operand_text(dependency.usage.operand)}"
    return operation.replace("_", "-")


def _subject(dependency: Dependency) -> str:
    return source_location(dependency) or "A usage with no recorded source location"


def _place(dependency: Dependency) -> str:
    return source_location(dependency) or "the usage (no recorded source location)"


def _verb(dependency: Dependency) -> str:
    return _USAGE_VERB.get(dependency.usage.operation, "uses")


def _leaf(field_path: str) -> str:
    steps = [step for step in split_field_path(field_path) if step != ITEMS]
    return steps[-1] if steps else field_path


def _change_note(change: APIChange) -> str:
    """The change's semantics, labeled "Declared change" only when the provider declared it."""
    old, new = _semantics_text(change.old_semantics), _semantics_text(change.new_semantics)
    if not old and not new:
        return ""
    declared = any(item.kind == "declared_change" for item in change.evidence)
    label = "Declared change" if declared else "Contract change"
    return f" {label}: {old or '(unspecified)'} -> {new or '(unspecified)'}."


def _semantics_text(semantics: str | None) -> str:
    # A merged declaration reads "<declared>; structural: <observed>" (detect_changes): clip each
    # part, so a long declared text never hides the structural observation.
    declared, marker, observed = (semantics or "").partition(STRUCTURAL_MARKER)
    if not marker:
        return clip_text(declared, 70)
    return f"{clip_text(declared, 70)}{STRUCTURAL_MARKER}{clip_text(observed, 70)}"


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _operand_text(operand: object) -> str:
    number = _numeric(operand)
    if number is not None:
        return _number_text(number)
    if operand is None:
        return "an unknown value"
    return clip_text(operand, 40) or "an unknown value"


def _money(major_amount: float, currency: str) -> str:
    if currency == "usd":
        return f"${major_amount:,.2f}"
    return f"{major_amount:,.2f} {currency.upper()}"


def _unit_label(unit: AmountUnit, currency: str) -> str:
    if currency == "usd":
        return "cents" if unit == "minor" else "dollars"
    return f"{unit} currency units"


def _direct_if_dependency_direct(dependency: Dependency) -> Confidence:
    return "direct" if dependency.confidence == "direct" else "inferred"


def _is_endpoint_removal(change: APIChange) -> bool:
    """True for the endpoint removal detect_changes reports: a structurally breaking "*" change.

    Any other endpoint-wide behavior change leaves the endpoint in the proposed contract.
    """
    return (
        change.kind == "behavior_changed"
        and normalize_field_path(change.field_path) == ENDPOINT_WIDE
        and change.structural_breaking
    )


# Matching-level decisions --------------------------------------------------------------------


def match_unresolved_reason(change: APIChange, dependency: Dependency) -> str | None:
    """Why a dependency that matches the change cannot be analysed at all, else None.

    Independent of the change's semantic text, so the matcher and both analysis modes
    (rules and model) agree on which dependencies are analyzable.
    """
    if dependency.confidence == "unresolved":
        return REASON_UNRESOLVED_DEPENDENCY
    # Never true for "*": it contains every field path.
    if relation(dependency.field_path, change.field_path) == "ancestor":
        return REASON_ENCLOSING_OBJECT
    operation = dependency.usage.operation
    if change.kind == "units_changed" and operation in ("read", "pass_through", "unknown"):
        return REASON_USAGE_HIDES_UNIT
    # A removal breaks every call to the endpoint, so only it needs no visible use of the field.
    removal = _is_endpoint_removal(change)
    if change.kind == "behavior_changed" and not removal and operation in ("pass_through", "unknown"):
        return REASON_USAGE_UNCLASSIFIED
    return None


def units_context(change: APIChange) -> UnitsContext | str:
    """Direction and factor of a declared units change, or the reason they are unknown."""
    old_unit = classify_amount_unit(change.old_semantics)
    new_unit = classify_amount_unit(change.new_semantics)
    if old_unit is None or new_unit is None or old_unit == new_unit:
        return REASON_NO_DIRECTION
    currency = currency_of(change.old_semantics, change.new_semantics)
    factor = minor_to_major_factor(currency)
    if factor is None:
        if currency is None:
            return REASON_NO_CURRENCY
        known = ", ".join(sorted(code.upper() for code in MINOR_UNITS_PER_MAJOR))
        return f"the minor-to-major factor for {currency.upper()} is not known to the rules (known: {known})"
    return UnitsContext(old_unit=old_unit, new_unit=new_unit, currency=cast(str, currency), factor=factor)


def evaluate_dependency(change: APIChange, dependency: Dependency) -> RuleOutcome | Unresolved:
    """The rule outcome for one dependency that matches ``change`` (see the module table)."""
    reason = match_unresolved_reason(change, dependency)
    if reason is not None:
        return Unresolved(reason)
    rule = _KIND_RULES.get(change.kind)
    if rule is None:
        return Unresolved(f"no deterministic rules for change kind {change.kind!r}")
    return rule(change, dependency)


def classify_dependency(change: APIChange, dependency: Dependency) -> RuleOutcome | None:
    """``RuleOutcome`` for an analyzable dependency, None when it stays unresolved."""
    outcome = evaluate_dependency(change, dependency)
    return outcome if isinstance(outcome, RuleOutcome) else None


# units_changed -------------------------------------------------------------------------------


def _units_rule(change: APIChange, dependency: Dependency) -> RuleOutcome | Unresolved:
    context = units_context(change)
    if isinstance(context, str):
        return Unresolved(context)
    operation = dependency.usage.operation
    if operation in ("divide", "multiply"):
        if _is_exact_conversion(dependency, context):
            return _exact_conversion(change, dependency, context)
        return _uncertain_conversion(change, dependency, context)
    if operation == "compare":
        return _threshold_shift(change, dependency, context)
    if operation == "store":
        return _mixed_units_storage(change, dependency, context)
    return Unresolved(REASON_USAGE_HIDES_UNIT)


def _is_exact_conversion(dependency: Dependency, context: UnitsContext) -> bool:
    """Whether the arithmetic is the conversion the current contract needs (x / F is x * 1/F)."""
    operand = _numeric(dependency.usage.operand)
    if operand is None or operand == 0:
        return False
    if dependency.usage.operation == context.conversion_operation:
        return operand == context.factor
    # A decimal 1/F such as 0.01 has no exact binary float, hence the tolerance.
    return math.isclose(operand * context.factor, 1.0, rel_tol=1e-9)


def _exact_conversion(change: APIChange, dependency: Dependency, context: UnitsContext) -> RuleOutcome:
    old = _unit_label(context.old_unit, context.currency)
    new = _unit_label(context.new_unit, context.currency)
    factor = context.factor
    to_major = context.conversion_operation == "divide"
    # The code's own arithmetic (e.g. "/100" or "*0.01"); the consequence depends only on the factor.
    symbol = "/" if dependency.usage.operation == "divide" else "*"
    operand = _operand_text(dependency.usage.operand)
    amount = ILLUSTRATIVE_MAJOR_AMOUNT
    expected = _money(amount, context.currency)
    proposed_raw = amount if context.new_unit == "major" else amount * factor
    # Applying the old conversion to the proposed value: off by exactly the factor.
    wrong = _money(amount / factor if to_major else amount * factor, context.currency)
    direction = "too small" if to_major else "too large"
    explanation = (
        f"{_subject(dependency)} {_verb(dependency)} {change.field_path} by {operand} to convert {old} to {new}. "
        f"Under the proposed contract {_leaf(change.field_path)} is already in {new} "
        f"({_number_text(proposed_raw)} for {expected}), so the result becomes {wrong} instead of "
        f"{expected} ({factor}x {direction})."
    )
    action = (
        f"Remove the {symbol}{operand} conversion at {_place(dependency)} for the proposed contract (pin to it) "
        f"or branch on the contract version; keep the {expected} expectation; coordinate deployment, as the "
        "fix is not backward compatible."
    )
    return RuleOutcome("conversion", "high", _direct_if_dependency_direct(dependency), explanation, action)


def _uncertain_conversion(change: APIChange, dependency: Dependency, context: UnitsContext) -> RuleOutcome:
    old = _unit_label(context.old_unit, context.currency)
    new = _unit_label(context.new_unit, context.currency)
    symbol = "/" if context.conversion_operation == "divide" else "*"
    conversion = f"{symbol}{context.factor} conversion the current contract implies"
    if _numeric(dependency.usage.operand) is None:
        # A named or unknown operand may well be the factor; only its value is not visible.
        verdict = (
            f"its operand is not a literal number, so whether this is the {conversion}, and how much the result "
            "changes, is uncertain"
        )
    else:
        verdict = f"this is not the {conversion}, so whether and how much the result changes is uncertain"
    explanation = (
        f"{_subject(dependency)} {_verb(dependency)} {change.field_path} by "
        f"{_operand_text(dependency.usage.operand)}. The proposed contract changes "
        f"{_leaf(change.field_path)} from {old} to {new} (factor {context.factor}), but {verdict}."
    )
    action = (
        f"Review the arithmetic at {_place(dependency)} against the proposed contract ({new}) and add a test "
        "with equivalent current and proposed responses."
    )
    missing = ("purpose and unit of this arithmetic", MISSING_SCENARIO)
    return RuleOutcome("conversion", "medium", "inferred", explanation, action, missing)


def _threshold_shift(change: APIChange, dependency: Dependency, context: UnitsContext) -> RuleOutcome:
    old = _unit_label(context.old_unit, context.currency)
    new = _unit_label(context.new_unit, context.currency)
    factor = context.factor
    threshold = _numeric(dependency.usage.operand)
    subject = f"{_subject(dependency)} compares {change.field_path} with {_operand_text(dependency.usage.operand)}."
    if threshold == 0:  # also -0.0
        explanation = (
            f"{subject} A positive rescale ({old} to {new}, factor {factor}) keeps zero at zero and preserves every "
            "sign, so this comparison gives the same result under the proposed contract unless the value is "
            "rounded, truncated or combined with other values before it is compared."
        )
        action = (
            f"No threshold rescale is needed at {_place(dependency)}; confirm that {change.field_path} is compared "
            "as received and keep a comparison test across the cutover."
        )
        missing = ("whether the value is transformed before the comparison", MISSING_COMPARISON_SCENARIO)
        return RuleOutcome("comparison", "low", "inferred", explanation, action, missing)
    if threshold is None:
        explanation = (
            f"{subject} A threshold written in {old} would shift by {factor}x under the proposed contract ({new})."
        )
        rescale = f"rescale it by {factor}"
    else:
        minor_to_major = context.old_unit == "minor"
        intended = _money(threshold / factor if minor_to_major else threshold, context.currency)
        triggers = _money(threshold if minor_to_major else threshold / factor, context.currency)
        # minor->major moves the boundary away from zero by the factor, major->minor toward it.
        if threshold > 0:
            shift = "higher" if minor_to_major else "lower"
        else:
            shift = "further below zero" if minor_to_major else "closer to zero"
        rescaled = threshold / factor if minor_to_major else threshold * factor
        explanation = (
            f"{subject} If that threshold is in {old} ({intended}), under the proposed contract ({new}) it only "
            f"triggers at {triggers} ({factor}x {shift})."
        )
        rescale = f"rescale it ({_number_text(threshold)} -> {_number_text(rescaled)})"
    action = (
        f"Confirm the unit of the threshold at {_place(dependency)}; if it is in {old}, {rescale} for the proposed "
        "contract and add a comparison test before cutover."
    )
    missing = ("intended unit of the threshold", MISSING_COMPARISON_SCENARIO)
    return RuleOutcome("comparison", "high", "inferred", explanation, action, missing)


def _mixed_units_storage(change: APIChange, dependency: Dependency, context: UnitsContext) -> RuleOutcome:
    old = _unit_label(context.old_unit, context.currency)
    new = _unit_label(context.new_unit, context.currency)
    destination = clip_text(dependency.usage.destination or "", 80)
    where = f" (destination: {destination})" if destination else ""
    explanation = (
        f"{_subject(dependency)} stores {change.field_path}{where}. After cutover new values would be in {new} "
        f"while earlier ones are in {old}, so mixed units become possible. {STORAGE_CAVEAT}"
    )
    action = (
        f"Check the units of the destination at {_place(dependency)} and its existing rows; convert at the write "
        f"boundary or plan a backfill/unit marker before cutover so {old} and {new} do not mix."
    )
    return RuleOutcome("storage", "medium", "inferred", explanation, action, STORAGE_MISSING_EVIDENCE)


# Structural and behavioral kinds ---------------------------------------------------------------


def _field_removed_rule(change: APIChange, dependency: Dependency) -> RuleOutcome:
    explanation = (
        f"{_subject(dependency)} {_verb(dependency)} {change.field_path}, which the proposed contract removes; "
        f"this usage would fail or see a missing value.{_change_note(change)}"
    )
    action = (
        f"Stop relying on {change.field_path} at {_place(dependency)} (or handle its absence) before the proposed "
        "contract ships; agree on a replacement with the provider."
    )
    category = category_for_usage(dependency.usage.operation)
    return RuleOutcome(category, "high", _direct_if_dependency_direct(dependency), explanation, action)


def _type_changed_rule(change: APIChange, dependency: Dependency) -> RuleOutcome:
    operation = dependency.usage.operation
    severity: Severity
    if not change.structural_breaking:
        severity = "low"
        detail = "its declared type changes without a structural break, so impact is unlikely but not ruled out"
    elif operation in ("divide", "multiply", "compare"):
        severity = "high"
        detail = "its type changes in a breaking way; arithmetic or comparison on the new type is likely to fail"
    else:
        severity = "medium"
        detail = "its type changes in a breaking way; code or storage expecting the old type may misbehave"
    explanation = f"{_subject(dependency)} {_verb(dependency)} {change.field_path}; {detail}.{_change_note(change)}"
    action = (
        f"Handle the new type of {change.field_path} explicitly at {_place(dependency)} and add a test with a "
        "proposed-contract response."
    )
    category = category_for_usage(operation)
    return RuleOutcome(category, severity, "inferred", explanation, action, (MISSING_SCENARIO,))


def _enum_changed_rule(change: APIChange, dependency: Dependency) -> RuleOutcome:
    operation = dependency.usage.operation
    severity: Severity
    if change.structural_breaking and operation == "compare":
        severity = "medium"
        detail = (
            "its allowed values change in a breaking way; comparisons against removed or renamed values may "
            "stop matching"
        )
    elif change.structural_breaking:
        severity = "low"
        detail = "its allowed values change in a breaking way; impact depends on how unexpected values are handled"
    else:
        severity = "low"
        detail = "its allowed values change without a structural break; unexpected values may still need handling"
    explanation = f"{_subject(dependency)} {_verb(dependency)} {change.field_path}; {detail}.{_change_note(change)}"
    action = (
        f"Review which values of {change.field_path} the code at {_place(dependency)} expects and handle new or "
        "removed values explicitly."
    )
    missing = ("values this usage actually receives", MISSING_SCENARIO)
    return RuleOutcome(category_for_usage(operation), severity, "inferred", explanation, action, missing)


def _behavior_changed_rule(change: APIChange, dependency: Dependency) -> RuleOutcome:
    category = category_for_usage(dependency.usage.operation)
    if _is_endpoint_removal(change):
        explanation = (
            f"{_subject(dependency)} {_verb(dependency)} {dependency.field_path} from an endpoint the proposed "
            f"contract removes; this call would fail.{_change_note(change)}"
        )
        action = (
            f"Migrate {_place(dependency)} off this endpoint before it is removed and coordinate timing with the "
            "provider."
        )
        return RuleOutcome(category, "high", _direct_if_dependency_direct(dependency), explanation, action)
    missing: tuple[str, ...] = (MISSING_SCENARIO,)
    if normalize_field_path(change.field_path) == ENDPOINT_WIDE:
        # The endpoint stays (e.g. caching, rate limits, consistency): whether this usage notices is unknown.
        explanation = (
            f"{_subject(dependency)} {_verb(dependency)} {dependency.field_path} from an endpoint whose documented "
            f"behavior changes; the effect on this usage is a prediction.{_change_note(change)}"
        )
        action = (
            f"Review {_place(dependency)} against the endpoint's new behavior and add a test with a "
            "proposed-contract response."
        )
        missing = ("whether this usage depends on the changed endpoint behavior", MISSING_SCENARIO)
    else:
        explanation = (
            f"{_subject(dependency)} {_verb(dependency)} {change.field_path}, whose documented behavior changes; "
            f"the effect on this usage is a prediction.{_change_note(change)}"
        )
        action = (
            f"Review {_place(dependency)} against the new behavior of {change.field_path} and add a test with a "
            "proposed-contract response."
        )
    return RuleOutcome(category, "medium", "inferred", explanation, action, missing)


_KIND_RULES: dict[str, Callable[[APIChange, Dependency], RuleOutcome | Unresolved]] = {
    "units_changed": _units_rule,
    "field_removed": _field_removed_rule,
    "type_changed": _type_changed_rule,
    "enum_changed": _enum_changed_rule,
    "behavior_changed": _behavior_changed_rule,
}

__all__ = [
    "CATEGORIES",
    "CONFIDENCE_ORDER",
    "MAX_ACTION_CHARS",
    "MAX_EXPLANATION_CHARS",
    "MISSING_COMPARISON_SCENARIO",
    "MISSING_SCENARIO",
    "RULES_LABEL",
    "SCENARIO_MISSING_EVIDENCE",
    "SEVERITY_ORDER",
    "STORAGE_CAVEAT",
    "STORAGE_MISSING_EVIDENCE",
    "USAGE_CATEGORY",
    "Category",
    "Confidence",
    "RuleOutcome",
    "Severity",
    "UnitsContext",
    "Unresolved",
    "bound_text",
    "cap_confidence",
    "category_for_usage",
    "classify_dependency",
    "compose_explanation",
    "describe_usage",
    "evaluate_dependency",
    "match_unresolved_reason",
    "source_location",
    "strongest_severity",
    "units_context",
    "weakest_confidence",
]
