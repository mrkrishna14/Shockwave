"""Consumer impact analysis: match dependencies to an APIChange and build predicted Risks.

Contract entry point: ``analyze_impact(change, dependencies, *, endpoints=None)``. It runs the
default analyzer, which is the deterministic rules path unless the backend explicitly installs
another one at startup with ``configure_default_analyzer``. No environment variable alone
enables live model calls.

Enrollment context: the backend passes only dependencies of active, opted-in enrollments for
``change.api_project_id``. Dependencies carry no enrollment status, so this module cannot and
does not re-check it. It never mixes enrollments: one Risk covers exactly one consumer
organization, enrollment and repository snapshot, per category.

Matching: a dependency matches when it names the change's service and endpoint, its field
path is an API field (``response...``/``request.<location>...``, not a local variable) and the
field is related to the changed field (``*`` matches every field of the endpoint). Endpoint
record ids are snapshot-specific. Without ``endpoints`` a dependency must name the change's
own record (``Dependency.endpoint_id`` from the catalog of ``change.old_snapshot_id``); with
``endpoints`` (the Endpoint records of the change's old snapshot and of the catalog the
consumer scan was keyed to) records of the same (service_id, METHOD, path) match across
snapshots. Anything else is dropped entirely - neither a risk nor unresolved - which keeps
negative controls (another service, another endpoint, a local ``amount``) out; when nothing
matched although dependencies of the service name endpoint records this call cannot
identify, a counted warning says to pass ``endpoints``. Matched dependencies the evidence
cannot decide are reported as unresolved: unknown, never safe.

Evidence: a Risk's evidence is assembled here, never by a model: every evidence item of its
dependencies verbatim, the change's evidence (shared with every enrolled consumer's Risk,
provider_owned declared, docs or source items included), and - for conversion and comparison
risks - the catalog's pointer to the executable scenario bound to its dependencies, only when
that scenario's category is the Risk's category (a divide scenario proves a conversion, a
compare scenario a comparison). Input evidence that uses the reserved ``shockwave-scenario://``
scheme is never copied (a counted warning says so), so pointers come only from the catalog. A
conversion or comparison Risk holds the dependencies of exactly one scenario (its id is keyed by
``<category>:<scenario_id>``) or only unbound ones, so a verdict never covers a location its
scenario did not run; ``risk_category`` recovers the category from a Risk id. Storage and read
risks never get a pointer, and neither does a usage no scenario is bound to, so they stay
predictions until their own proof exists.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

from analyzer.fieldpaths import is_api_field_path, relation
from analyzer.rules import (
    CATEGORIES,
    CONFIDENCE_ORDER,
    MAX_ACTION_CHARS,
    RULES_LABEL,
    SCENARIO_MISSING_EVIDENCE,
    SEVERITY_ORDER,
    Category,
    RuleOutcome,
    Unresolved,
    compose_explanation,
    describe_usage,
    evaluate_dependency,
    match_unresolved_reason,
    source_location,
    strongest_severity,
    weakest_confidence,
)
from discovery.contracts import (
    SCHEMA_VERSION,
    AnalysisResult,
    APIChange,
    Dependency,
    Endpoint,
    Evidence,
    Risk,
)
from discovery.ids import EndpointIdentity, risk_id, same_endpoint
from discovery.ids import endpoint_identities as identity_map
from discovery.text import clip_text_flagged
from verification.scenarios.catalog import (
    SCENARIO_CATEGORIES,
    SCENARIOS,
    find_applicable_scenario,
    is_scenario_pointer,
    pointer_scenario_id,
    scenario_pointer_evidence,
)

if TYPE_CHECKING:
    from analyzer.model_adapter import ModelImpactAnalyzer

AnalysisMode = Literal["rules", "model", "fixture"]
Identities = Mapping[str, EndpointIdentity]

RULES_PREDICTION_WARNING = "predictions only: no tests have run (deterministic rules, not model inference)"
NO_MATCH_WARNING = (
    "no supplied dependency matched this change; that does not mean consumers are safe "
    "(unscanned repositories remain unknown)"
)


# Matching ----------------------------------------------------------------------------------


def match_dependencies(
    change: APIChange, dependencies: list[Dependency], *, endpoints: Iterable[Endpoint] | None = None
) -> tuple[list[Dependency], list[Dependency]]:
    """(analyzable matches, unresolved matches), each sorted by id; non-matching are dropped.

    ``endpoints`` (additive): the Endpoint records of the change's old snapshot and of the
    catalog the consumer scan was keyed to, so a dependency naming another snapshot's record of
    the same (service_id, METHOD, path) matches. Raises ValueError for contradictory input: two
    different dependencies with one id, or one enrollment naming two consumer organizations
    (that would merge their risks).
    """
    return _match(change, dependencies, _identities(endpoints))


def _identities(endpoints: Iterable[Endpoint] | None) -> dict[str, EndpointIdentity] | None:
    return None if endpoints is None else identity_map(endpoints)


def _match(
    change: APIChange, dependencies: Iterable[Dependency], identities: Identities | None
) -> tuple[list[Dependency], list[Dependency]]:
    matched = [dependency for dependency in _unique_by_id(dependencies) if _matches(change, dependency, identities)]
    _check_enrollment_organizations(matched)
    analyzable: list[Dependency] = []
    unresolved: list[Dependency] = []
    for dependency in sorted(matched, key=lambda item: str(item.id)):
        target = unresolved if match_unresolved_reason(change, dependency) else analyzable
        target.append(dependency)
    return analyzable, unresolved


def _matches(change: APIChange, dependency: Dependency, identities: Identities | None) -> bool:
    return (
        dependency.service_id == change.service_id
        and same_endpoint(dependency.endpoint_id, change.endpoint_id, identities)
        and is_api_field_path(dependency.field_path)
        and relation(dependency.field_path, change.field_path) != "unrelated"
    )


def _unique_by_id(dependencies: Iterable[Dependency]) -> list[Dependency]:
    unique: dict[str, Dependency] = {}
    for dependency in dependencies:
        existing = unique.setdefault(str(dependency.id), dependency)
        if existing is not dependency and existing.model_dump(mode="json") != dependency.model_dump(mode="json"):
            raise ValueError(f"conflicting dependencies share id {dependency.id}")
    return list(unique.values())


def _owner(dependency: Dependency) -> tuple[str, str, str]:
    return (str(dependency.consumer_org_id), str(dependency.enrollment_id), str(dependency.repository_snapshot_id))


def _check_enrollment_organizations(dependencies: Iterable[Dependency]) -> None:
    organizations: dict[str, str] = {}
    for dependency in dependencies:
        organization = organizations.setdefault(str(dependency.enrollment_id), str(dependency.consumer_org_id))
        if organization != str(dependency.consumer_org_id):
            raise ValueError(
                f"dependencies of enrollment {dependency.enrollment_id} name different consumer organizations"
            )


def no_match_warnings(
    change: APIChange, dependencies: Iterable[Dependency], *, endpoint_identities: Identities | None = None
) -> list[str]:
    """Warnings for a call in which no dependency matched (counts only: no ids, paths or code).

    Besides NO_MATCH_WARNING, counts the service's dependencies that name an endpoint record
    this call cannot identify: they may name the change's endpoint in another contract snapshot,
    which only ``endpoints`` can tell apart from another endpoint.
    """
    known = endpoint_identities or {}
    change_known = str(change.endpoint_id) in known
    unidentified = {
        str(dependency.id)
        for dependency in dependencies
        if dependency.service_id == change.service_id
        and str(dependency.endpoint_id) != str(change.endpoint_id)
        and not (change_known and str(dependency.endpoint_id) in known)
    }
    if not unidentified:
        return [NO_MATCH_WARNING]
    count = len(unidentified)
    if count == 1:
        subject, verb = "dependency of this service names", "was"
    else:
        subject, verb = "dependencies of this service name", "were"
    hint = (
        f"{count} supplied {subject} an endpoint record other than the change's and {verb} not analyzed; if the "
        "scan was keyed to another contract snapshot's catalog, pass endpoints (the Endpoint records of the "
        "change's old snapshot and of that catalog) so the same (service, method, path) matches"
    )
    return [NO_MATCH_WARNING, hint]


# Risk assembly (shared by the rules and model paths) -----------------------------------------


def _bound_scenario(
    change: APIChange, dependency: Dependency, identities: Identities | None, category: str | None = None
) -> str | None:
    """The scenario bound to (change, dependency); with ``category``, only one of that category."""
    spec = find_applicable_scenario(change, dependency, endpoint_identities=identities)
    if spec is None or (category is not None and spec.category != category):
        return None
    return spec.scenario_id


def scenario_pointers(
    change: APIChange,
    dependencies: Iterable[Dependency],
    *,
    endpoint_identities: Identities | None = None,
    category: str | None = None,
) -> list[Evidence]:
    """Catalog pointers to executable scenarios bound to (change, dependency), sorted by id.

    With ``category`` only scenarios of that category count: a pointer belongs only on a Risk of
    its scenario's category.
    """
    pointers = {}
    for dependency in dependencies:
        spec = find_applicable_scenario(change, dependency, endpoint_identities=endpoint_identities)
        if spec is not None and (category is None or spec.category == category):
            pointer = scenario_pointer_evidence(spec, dependency)
            pointers[str(pointer.id)] = pointer
    return [pointers[key] for key in sorted(pointers)]


def split_by_scenario(
    change: APIChange,
    dependencies: Sequence[Dependency],
    category: Category,
    *,
    endpoint_identities: Identities | None = None,
) -> list[list[Dependency]]:
    """``dependencies`` as Risk groups: conversion/comparison dependencies split by their bound scenario.

    Each conversion or comparison group holds the dependencies of exactly one executable scenario
    of that category, or only unbound ones, so a scenario verdict never covers a location the
    scenario did not run (and a repository with Python and TypeScript display code gets one
    plannable Risk per scenario). Other categories stay one group.
    """
    if category not in SCENARIO_CATEGORIES:
        return [list(dependencies)]
    groups: dict[str, list[Dependency]] = {}
    for dependency in dependencies:
        key = _bound_scenario(change, dependency, endpoint_identities, category) or ""
        groups.setdefault(key, []).append(dependency)
    return [groups[key] for key in sorted(groups)]


def risk_evidence(
    change: APIChange,
    dependencies: Sequence[Dependency],
    category: Category,
    *,
    endpoint_identities: Identities | None = None,
) -> list[Evidence]:
    """Dependency evidence verbatim, then change evidence, then (conversion/comparison) scenario pointers.

    Input evidence using the reserved ``shockwave-scenario://`` scheme is never copied: scenario
    pointers come only from the catalog, and only for scenarios of the Risk's own category.
    """
    ordered = sorted(dependencies, key=lambda item: str(item.id))
    items = [item for dependency in ordered for item in dependency.evidence if not is_scenario_pointer(item)]
    items.extend(item for item in change.evidence if not is_scenario_pointer(item))
    if category in SCENARIO_CATEGORIES:
        items.extend(
            scenario_pointers(change, ordered, endpoint_identities=endpoint_identities, category=category)
        )
    unique: dict[str, Evidence] = {}
    for item in items:
        unique.setdefault(str(item.id), item)
    return [item.model_copy(deep=True) for item in unique.values()]


def reserved_evidence_warning(change: APIChange, dependencies: Iterable[Dependency]) -> str | None:
    """Counted warning when input evidence uses the reserved scenario scheme (never copied into a Risk)."""
    reserved = {str(item.id) for item in change.evidence if is_scenario_pointer(item)}
    reserved.update(
        str(item.id) for dependency in dependencies for item in dependency.evidence if is_scenario_pointer(item)
    )
    if not reserved:
        return None
    count = len(reserved)
    subject = "evidence item uses" if count == 1 else "evidence items use"
    return (
        f"{count} change or dependency {subject} the reserved shockwave-scenario:// scheme and "
        f"{'was' if count == 1 else 'were'} not copied into any risk; scenario pointers come only from the "
        "scenario catalog"
    )


def _scenario_key(category: str, scenario_id: str) -> str:
    return f"{category}:{scenario_id}"


def _risk_key(
    change: APIChange, dependencies: Sequence[Dependency], category: Category, identities: Identities | None
) -> str:
    if category not in SCENARIO_CATEGORIES:
        return category
    scenarios = {_bound_scenario(change, dependency, identities, category) for dependency in dependencies}
    if len(scenarios) != 1:
        raise ValueError(
            f"a {category} Risk holds the dependencies of exactly one executable scenario, or only unbound ones "
            "(group them with split_by_scenario)"
        )
    scenario_id = scenarios.pop()
    return category if scenario_id is None else _scenario_key(category, scenario_id)


def build_risk(
    change: APIChange,
    dependencies: Sequence[Dependency],
    *,
    category: Category,
    severity: str,
    confidence: str,
    explanation: str,
    suggested_action: str,
    endpoint_identities: Identities | None = None,
) -> Risk:
    """One Risk for dependencies of a single (consumer org, enrollment, repository snapshot).

    A conversion or comparison Risk whose dependencies are bound to one executable scenario of
    its category gets an id keyed by ``<category>:<scenario_id>`` and that scenario's pointers;
    mixing scenarios, or bound with unbound dependencies, raises ValueError
    (``split_by_scenario`` groups them).
    """
    if not dependencies:
        raise ValueError("a Risk needs at least one dependency")
    owners = {_owner(item) for item in dependencies}
    if len(owners) != 1:
        raise ValueError("a Risk covers exactly one consumer organization, enrollment and repository snapshot")
    consumer_org_id, enrollment_id, repository_snapshot_id = owners.pop()
    key = _risk_key(change, dependencies, category, endpoint_identities)
    return Risk(
        id=risk_id(change.id, enrollment_id, repository_snapshot_id, key),
        change_id=change.id,
        consumer_org_id=consumer_org_id,
        enrollment_id=enrollment_id,
        repository_snapshot_id=repository_snapshot_id,
        dependency_ids=sorted({str(item.id) for item in dependencies}),
        severity=severity,
        confidence=confidence,
        explanation=explanation,
        suggested_action=suggested_action,
        evidence=risk_evidence(change, dependencies, category, endpoint_identities=endpoint_identities),
    )


def risk_category(change: APIChange, risk: Risk) -> Category | None:
    """The category a Risk id was derived from, or None (the contract has no category field).

    Scenario-keyed Risks (``conversion:<scenario_id>``, ``comparison:<scenario_id>``) report
    their category.
    """
    key = _derived_key(change, risk)
    return None if key is None else cast(Category, key.partition(":")[0])


def _derived_key(change: APIChange, risk: Risk) -> str | None:
    scenario_keys = (
        _scenario_key(spec.category, scenario_id)
        for scenario_id, spec in sorted(SCENARIOS.items())
        if spec.category is not None
    )
    keys = (*CATEGORIES, *scenario_keys)
    for key in keys:
        if risk_id(change.id, risk.enrollment_id, risk.repository_snapshot_id, key) == str(risk.id):
            return key
    return None


def unresolved_summary(reasons: Sequence[str], *, stage: str = "") -> str | None:
    """Aggregated warning: how many matched dependencies are unresolved and why (no paths/code)."""
    if not reasons:
        return None
    counts = Counter(reasons)
    noun = "dependency is" if len(reasons) == 1 else "dependencies are"
    detail = "; ".join(f"{reason} ({count})" for reason, count in sorted(counts.items()))
    return f"{len(reasons)} matched {noun} unresolved{stage} (unknown, not safe): {detail}"


def finalize_result(
    change: APIChange,
    *,
    risks: Iterable[Risk],
    unresolved_ids: Iterable[object],
    warnings: Iterable[str | None],
    mode: AnalysisMode,
) -> AnalysisResult:
    """AnalysisResult with deterministic ordering: severity, then confidence, then owner, then id."""
    ordered = sorted(
        risks,
        key=lambda risk: (
            -SEVERITY_ORDER.index(risk.severity),
            -CONFIDENCE_ORDER.index(risk.confidence),
            str(risk.consumer_org_id),
            str(risk.enrollment_id),
            str(risk.repository_snapshot_id),
            str(risk.id),
        ),
    )
    return AnalysisResult(
        schema_version=SCHEMA_VERSION,
        change_id=change.id,
        risks=ordered,
        unresolved_dependency_ids=sorted({str(item) for item in unresolved_ids}),
        warnings=list(dict.fromkeys(warning for warning in warnings if warning)),
        analysis_mode=mode,
    )


# Rules analysis ------------------------------------------------------------------------------


def _rules_result(
    change: APIChange,
    analyzable: list[Dependency],
    unresolved: list[Dependency],
    *,
    endpoint_identities: Identities | None = None,
    extra_warnings: Sequence[str] = (),
    no_match: Sequence[str] = (),
) -> AnalysisResult:
    reasons = [match_unresolved_reason(change, dependency) or "" for dependency in unresolved]
    unresolved_ids = [str(dependency.id) for dependency in unresolved]
    groups: dict[tuple[str, str, str, Category], list[tuple[Dependency, RuleOutcome]]] = {}
    for dependency in analyzable:
        outcome = evaluate_dependency(change, dependency)
        if isinstance(outcome, Unresolved):
            unresolved_ids.append(str(dependency.id))
            reasons.append(outcome.reason)
            continue
        groups.setdefault((*_owner(dependency), outcome.category), []).append((dependency, outcome))
    risks: list[Risk] = []
    for key, members in groups.items():
        category = key[3]
        outcomes = {str(dependency.id): outcome for dependency, outcome in members}
        dependencies = [dependency for dependency, _ in members]
        for group in split_by_scenario(change, dependencies, category, endpoint_identities=endpoint_identities):
            grouped = [(dependency, outcomes[str(dependency.id)]) for dependency in group]
            risks.append(_rules_risk(change, category, grouped, endpoint_identities))
    warnings = [
        RULES_PREDICTION_WARNING,
        *extra_warnings,
        reserved_evidence_warning(change, analyzable),
        unresolved_summary(reasons),
        *no_match,
    ]
    return finalize_result(change, risks=risks, unresolved_ids=unresolved_ids, warnings=warnings, mode="rules")


def _rules_risk(
    change: APIChange,
    category: Category,
    members: list[tuple[Dependency, RuleOutcome]],
    identities: Identities | None,
) -> Risk:
    members = sorted(
        members,
        key=lambda member: (
            -SEVERITY_ORDER.index(member[1].severity),
            source_location(member[0]) or "",
            str(member[0].id),
        ),
    )
    primary = members[0][1]
    dependencies = [dependency for dependency, _ in members]
    core = primary.explanation
    others = [
        f"{source_location(dependency) or dependency.id} ({describe_usage(dependency)})"
        for dependency in dependencies[1:]
    ]
    if others:
        core += f" Also affected: {', '.join(others)}."
    bound = category in SCENARIO_CATEGORIES and bool(
        scenario_pointers(change, dependencies, endpoint_identities=identities, category=category)
    )
    if bound:
        core += " An executable scenario exists for this usage; it has not run."
    missing = [item for _, outcome in members for item in outcome.missing_evidence]
    if bound:  # the scenario exists; only its execution is outstanding
        missing = [item for item in missing if item not in SCENARIO_MISSING_EVIDENCE]
    explanation, _ = compose_explanation(core, RULES_LABEL, missing)
    suggested_action, _ = clip_text_flagged(primary.suggested_action, MAX_ACTION_CHARS)
    return build_risk(
        change,
        dependencies,
        category=category,
        severity=strongest_severity(outcome.severity for _, outcome in members),
        confidence=weakest_confidence(outcome.confidence for _, outcome in members),
        explanation=explanation,
        suggested_action=suggested_action,
        endpoint_identities=identities,
    )


def analyze_with_rules(
    change: APIChange, dependencies: list[Dependency], *, endpoints: Iterable[Endpoint] | None = None
) -> AnalysisResult:
    """Deterministic rules analysis (``analysis_mode="rules"``), whatever the default analyzer is."""
    dependencies = list(dependencies)
    identities = _identities(endpoints)
    analyzable, unresolved = _match(change, dependencies, identities)
    no_match = _unmatched(change, dependencies, analyzable, unresolved, identities)
    return _rules_result(change, analyzable, unresolved, endpoint_identities=identities, no_match=no_match)


def _unmatched(
    change: APIChange,
    dependencies: Sequence[Dependency],
    analyzable: Sequence[Dependency],
    unresolved: Sequence[Dependency],
    identities: Identities | None,
) -> list[str]:
    if analyzable or unresolved:
        return []
    return no_match_warnings(change, dependencies, endpoint_identities=identities)


# Analyzer selection ----------------------------------------------------------------------------


class ImpactAnalyzer:
    """Rules (default) or model-assisted impact analysis with a truthful ``analysis_mode``.

    The deterministic matcher runs once and decides which dependencies are analyzable. In model
    mode consent is checked for exactly those (what may be sent) and the model only judges
    them, one request per repository snapshot. A ModelError is re-raised unless
    ``fallback_to_rules`` is set, in which case the rules result is returned
    (``analysis_mode="rules"``) with a warning naming the failure's type (never its text).
    """

    def __init__(
        self,
        mode: Literal["rules", "model"] = "rules",
        *,
        model_analyzer: ModelImpactAnalyzer | None = None,
        fallback_to_rules: bool = False,
    ) -> None:
        if mode not in ("rules", "model"):
            raise ValueError(f"mode must be 'rules' or 'model', got {mode!r}")
        if mode == "model" and model_analyzer is None:
            raise ValueError("model mode requires a ModelImpactAnalyzer")
        if mode == "rules" and model_analyzer is not None:
            raise ValueError("model_analyzer is only used in model mode")
        self.mode = mode
        self.model_analyzer = model_analyzer
        self.fallback_to_rules = fallback_to_rules

    def analyze(
        self, change: APIChange, dependencies: list[Dependency], *, endpoints: Iterable[Endpoint] | None = None
    ) -> AnalysisResult:
        dependencies = list(dependencies)
        identities = _identities(endpoints)
        analyzable, unresolved = _match(change, dependencies, identities)
        no_match = _unmatched(change, dependencies, analyzable, unresolved, identities)
        if self.mode == "rules" or self.model_analyzer is None:
            return _rules_result(change, analyzable, unresolved, endpoint_identities=identities, no_match=no_match)
        # Imported here: model_adapter imports this module for the shared Risk assembly.
        from analyzer.model_adapter import ModelError

        try:
            self.model_analyzer.check_consent(analyzable)
            return self.model_analyzer._analyze_matched(
                change, analyzable, unresolved, endpoint_identities=identities, extra_warnings=no_match
            )
        except ModelError as error:
            if not self.fallback_to_rules:
                raise
            # The type only: an error's text may carry model output, and these warnings are shared.
            warning = f"model analysis failed ({type(error).__name__}); deterministic rules used"
            return _rules_result(
                change,
                analyzable,
                unresolved,
                endpoint_identities=identities,
                extra_warnings=[warning],
                no_match=no_match,
            )


_default_analyzer = ImpactAnalyzer("rules")


def configure_default_analyzer(analyzer: ImpactAnalyzer) -> None:
    """Backend startup hook: the analyzer ``analyze_impact`` uses (rules until called)."""
    global _default_analyzer
    if not isinstance(analyzer, ImpactAnalyzer):
        raise TypeError("configure_default_analyzer expects an ImpactAnalyzer")
    _default_analyzer = analyzer


def default_analyzer() -> ImpactAnalyzer:
    return _default_analyzer


def analyze_impact(
    change: APIChange, dependencies: list[Dependency], *, endpoints: Iterable[Endpoint] | None = None
) -> AnalysisResult:
    """Contract function: predicted risks of ``change`` for the supplied dependencies.

    ``dependencies`` must come from active, opted-in enrollments of ``change.api_project_id``
    (the backend's responsibility; status is not re-checked here). Each ``endpoint_id`` must be
    a record of ``change.old_snapshot_id``'s catalog, unless ``endpoints`` (additive keyword)
    supplies the Endpoint records of that snapshot and of the catalog the scan was keyed to.
    Risks are grouped per enrollment, repository snapshot and category (conversion and
    comparison also per bound scenario); nothing here is executed or verified.
    """
    return _default_analyzer.analyze(change, dependencies, endpoints=endpoints)


# Evidence validation -----------------------------------------------------------------------------


def _same(left: Evidence, right: Evidence) -> bool:
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def validate_risk_evidence(
    result: AnalysisResult,
    change: APIChange,
    dependencies: list[Dependency],
    *,
    endpoints: Iterable[Endpoint] | None = None,
) -> list[str]:
    """Problems with the references in ``result``; [] when every reference is real.

    Checks that each Risk names matched input dependencies of its own owner, carries their
    evidence verbatim, and otherwise holds only the change's evidence and - on a conversion or
    comparison Risk keyed by a scenario of its category, whose dependencies are all bound to
    it - that scenario's catalog pointers. A pointer to a scenario of another category (or to a
    control) is rejected. Pass the ``endpoints`` the analysis was given.
    """
    identities = _identities(endpoints)
    problems: list[str] = []
    if str(result.change_id) != str(change.id):
        problems.append(f"result change_id {result.change_id} is not change {change.id}")
    analyzable, unresolved = _match(change, dependencies, identities)
    matched = {str(dependency.id): dependency for dependency in [*analyzable, *unresolved]}
    change_evidence = {str(item.id): item for item in change.evidence}
    owner_of: dict[str, str] = {}
    for risk in result.risks:
        problems.extend(_risk_problems(risk, change, matched, change_evidence, owner_of, identities))
    for dependency_id in (str(item) for item in result.unresolved_dependency_ids):
        if dependency_id not in matched:
            problems.append(f"unresolved dependency {dependency_id} is not a matched input dependency")
        elif dependency_id in owner_of:
            problems.append(f"dependency {dependency_id} is both unresolved and in risk {owner_of[dependency_id]}")
    return problems


def _risk_problems(
    risk: Risk,
    change: APIChange,
    matched: dict[str, Dependency],
    change_evidence: dict[str, Evidence],
    owner_of: dict[str, str],
    identities: Identities | None,
) -> list[str]:
    label = f"risk {risk.id}"
    problems: list[str] = []
    if str(risk.change_id) != str(change.id):
        problems.append(f"{label}: change_id {risk.change_id} is not change {change.id}")
    key = _derived_key(change, risk)
    if key is None:
        problems.append(f"{label}: id is not derived from (change, enrollment, snapshot, category)")
    category, _, scenario_id = (key or "").partition(":")
    if not risk.dependency_ids:
        problems.append(f"{label}: names no dependency")
    owner = (str(risk.consumer_org_id), str(risk.enrollment_id), str(risk.repository_snapshot_id))
    members: list[Dependency] = []
    bound_members: list[Dependency] = []
    for dependency_id in (str(item) for item in risk.dependency_ids):
        dependency = matched.get(dependency_id)
        if dependency is None:
            problems.append(f"{label}: dependency {dependency_id} is not a matched input dependency")
            continue
        if _owner(dependency) != owner:
            problems.append(f"{label}: dependency {dependency_id} belongs to another organization/enrollment/snapshot")
        if owner_of.setdefault(dependency_id, str(risk.id)) != str(risk.id):
            problems.append(f"{label}: dependency {dependency_id} is also in risk {owner_of[dependency_id]}")
        members.append(dependency)
        if category in SCENARIO_CATEGORIES:
            bound = _bound_scenario(change, dependency, identities, category)
            if bound == (scenario_id or None):
                bound_members.append(dependency)
            elif scenario_id:  # the scenario's verdict would cover a location it never ran
                problems.append(f"{label}: dependency {dependency_id} is not bound to scenario {scenario_id}")
            else:
                problems.append(f"{label}: dependency {dependency_id} is bound to scenario {bound}, not to this risk")
    dependency_evidence = {
        str(item.id): item for dependency in members for item in dependency.evidence if not is_scenario_pointer(item)
    }
    pointers = (
        {
            str(item.id): item
            for item in scenario_pointers(change, bound_members, endpoint_identities=identities, category=category)
        }
        if scenario_id
        else {}
    )
    present = {str(item.id) for item in risk.evidence}
    for item_id in dependency_evidence:
        if item_id not in present:
            problems.append(f"{label}: omits evidence {item_id} of its dependencies")
    for item in risk.evidence:
        problem = _evidence_problem(item, category or None, dependency_evidence, change_evidence, pointers)
        if problem:
            problems.append(f"{label}: evidence {item.id} ({item.kind}) {problem}")
    return problems


def _evidence_problem(
    item: Evidence,
    category: str | None,
    dependency_evidence: dict[str, Evidence],
    change_evidence: dict[str, Evidence],
    pointers: dict[str, Evidence],
) -> str | None:
    key = str(item.id)
    # The reserved scheme first: whatever copied it in, only the catalog's pointer for one of this
    # conversion/comparison risk's own dependencies (and its scenario, of the risk's category) is
    # executable proof.
    if is_scenario_pointer(item):
        if category not in SCENARIO_CATEGORIES:
            kind = category or "unknown"
            return (
                f"is a scenario pointer on a risk of category {kind} (only conversion and comparison risks have "
                "executable proof)"
            )
        named = SCENARIOS.get(pointer_scenario_id(item) or "")
        if named is not None and named.category != category:
            scenario_category = named.category or "control"
            return (
                f"points to a {scenario_category} scenario, but the risk's category is {category} (a scenario proves "
                "only risks of its own category)"
            )
        expected = pointers.get(key)
        if expected is None or not _same(expected, item):
            return "points to a scenario that is not applicable to this risk's dependencies"
        return None
    for original in (dependency_evidence.get(key), change_evidence.get(key)):
        if original is not None and _same(original, item):
            return None
    if item.kind == "source":
        return "is not a dependency location verbatim"
    return "is neither dependency, change nor applicable scenario evidence"


__all__ = [
    "NO_MATCH_WARNING",
    "ImpactAnalyzer",
    "analyze_impact",
    "analyze_with_rules",
    "build_risk",
    "configure_default_analyzer",
    "default_analyzer",
    "finalize_result",
    "match_dependencies",
    "no_match_warnings",
    "reserved_evidence_warning",
    "risk_category",
    "risk_evidence",
    "scenario_pointers",
    "split_by_scenario",
    "unresolved_summary",
    "validate_risk_evidence",
]
