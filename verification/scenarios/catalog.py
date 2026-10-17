"""Executable regression scenarios and provider response fixtures.

A scenario couples a provider response fixture pair (current vs proposed contract),
an invariant business expectation, a native test harness, and a binding to a known
consumer entry point. This module only describes scenarios; Sai's runner executes
them (see ``verification.scenarios.staging`` for the runner-facing protocol).

Scenario selection is decided during impact analysis, when the change and the
dependency are both known, and recorded on the Risk as a ``kind="test"`` Evidence
pointer (``shockwave-scenario://<scenario_id>``). ``build_verification_plan`` later
resolves that pointer, so it needs nothing beyond the contract's (risk, snapshot).
Risks without a pointer (comparison-only, storage-only, unbound consumers) have no
executable proof and stay ``not_run``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from analyzer.units import AmountUnit, classify_amount_unit, currency_of
from discovery.contracts import APIChange, Dependency, Evidence
from discovery.ids import EndpointIdentity, evidence_id, same_endpoint
from discovery.records import operation_from_pointer

SCENARIO_URI_PREFIX = "shockwave-scenario://"
CATALOG_REVISION = "shockwave-scenarios@2026-10-16.1"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STAGING_DIR = ".shockwave/scenarios"

RunnerProfile = Literal["trusted_python", "trusted_node", "sandbox_python", "sandbox_node"]


class UnknownScenario(KeyError):
    """Raised for a scenario or fixture identifier that is not in this catalog."""


@dataclass(frozen=True)
class ResponseFixture:
    fixture_id: str
    service_id: str
    method: str
    path: str
    contract: Literal["current", "proposed"]
    filename: str


@dataclass(frozen=True)
class ConsumerBinding:
    """A known consumer entry point that renders the dependent value."""

    language: Literal["python", "typescript", "javascript"]
    consumer_file: str  # consumer-repository-relative file containing the dependency
    module: str  # Python dotted module, or JS/TS file path relative to the repository root
    function: str
    args: tuple[str, ...]
    http_injection: Literal["python_requests_get", "js_global_fetch"]


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    title: str
    service_id: str
    method: str
    path: str
    field_path: str
    change_kind: Literal["units_changed"]
    old_unit: AmountUnit
    new_unit: AmountUnit
    currency: str
    usage_operation: Literal["divide"]
    usage_operand: int
    binding: ConsumerBinding
    old_fixture_id: str
    new_fixture_id: str
    assertion_id: str
    expected_display: str
    expected_behavior: str
    harness_asset: str  # file under ASSETS_DIR
    staged_test_name: str  # file name used inside the staged scenario directory
    trusted_profile: RunnerProfile
    sandbox_profile: RunnerProfile
    timeout_seconds: int
    additional_test_files: tuple[str, ...] = ()

    @property
    def staged_dir(self) -> str:
        return f"{STAGING_DIR}/{self.scenario_id}"

    @property
    def staged_test_file(self) -> str:
        return f"{self.staged_dir}/{self.staged_test_name}"


FIXTURES: dict[str, ResponseFixture] = {
    fixture.fixture_id: fixture
    for fixture in (
        ResponseFixture(
            fixture_id="demo-payments.charge.ch_demo.v1-cents",
            service_id="demo-payments",
            method="GET",
            path="/v1/charges/{id}",
            contract="current",
            filename="demo-payments.charge.ch_demo.v1-cents.json",
        ),
        ResponseFixture(
            fixture_id="demo-payments.charge.ch_demo.v2-dollars",
            service_id="demo-payments",
            method="GET",
            path="/v1/charges/{id}",
            contract="proposed",
            filename="demo-payments.charge.ch_demo.v2-dollars.json",
        ),
    )
}

_DISPLAY_BEHAVIOR = (
    "GET /v1/charges/{id}: the displayed price is $10.00 for both the current response "
    "(amount=1000, USD minor units) and the equivalent proposed response (amount=10, USD major units)."
)


def _display_scenario(
    *,
    scenario_id: str,
    title: str,
    binding: ConsumerBinding,
    harness_asset: str,
    staged_test_name: str,
    trusted_profile: RunnerProfile,
    sandbox_profile: RunnerProfile,
) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id=scenario_id,
        title=title,
        service_id="demo-payments",
        method="GET",
        path="/v1/charges/{id}",
        field_path="response.amount",
        change_kind="units_changed",
        old_unit="minor",
        new_unit="major",
        currency="usd",
        usage_operation="divide",
        usage_operand=100,
        binding=binding,
        old_fixture_id="demo-payments.charge.ch_demo.v1-cents",
        new_fixture_id="demo-payments.charge.ch_demo.v2-dollars",
        assertion_id="displayed_price_usd",
        expected_display="$10.00",
        expected_behavior=_DISPLAY_BEHAVIOR,
        harness_asset=harness_asset,
        staged_test_name=staged_test_name,
        trusted_profile=trusted_profile,
        sandbox_profile=sandbox_profile,
        timeout_seconds=60,
    )


SCENARIOS: dict[str, ScenarioSpec] = {
    spec.scenario_id: spec
    for spec in (
        _display_scenario(
            scenario_id="demo-payments.charge-amount-units.display.python",
            title="Python storefront price display survives the cents-to-dollars change",
            binding=ConsumerBinding(
                language="python",
                consumer_file="storefront/pricing.py",
                module="storefront.pricing",
                function="display_charge_price",
                args=("ch_demo",),
                http_injection="python_requests_get",
            ),
            harness_asset="python_display_harness.py",
            staged_test_name="test_shockwave_display_invariant.py",
            trusted_profile="trusted_python",
            sandbox_profile="sandbox_python",
        ),
        _display_scenario(
            scenario_id="demo-payments.charge-amount-units.display.typescript",
            title="TypeScript storefront price display survives the cents-to-dollars change",
            binding=ConsumerBinding(
                language="typescript",
                consumer_file="src/pricing.ts",
                module="src/pricing.ts",
                function="displayChargePrice",
                args=("ch_demo",),
                http_injection="js_global_fetch",
            ),
            harness_asset="node_display_harness.mjs",
            staged_test_name="shockwave_display_invariant.test.mjs",
            trusted_profile="trusted_node",
            sandbox_profile="sandbox_node",
        ),
    )
}


def get_scenario(scenario_id: str) -> ScenarioSpec:
    try:
        return SCENARIOS[scenario_id]
    except KeyError:
        raise UnknownScenario(scenario_id) from None


def get_fixture(fixture_id: str) -> ResponseFixture:
    try:
        return FIXTURES[fixture_id]
    except KeyError:
        raise UnknownScenario(fixture_id) from None


def fixture_path(fixture_id: str) -> Path:
    return FIXTURES_DIR / get_fixture(fixture_id).filename


def load_fixture(fixture_id: str) -> dict:
    """The fixture document: metadata plus ``response.body`` (the provider response)."""
    return json.loads(fixture_path(fixture_id).read_text(encoding="utf-8"))


def _operand_equals(operand: object, expected: int) -> bool:
    if isinstance(operand, bool) or operand is None:
        return False
    try:
        return float(operand) == float(expected)
    except (TypeError, ValueError):
        return False


def find_applicable_scenario(
    change: APIChange,
    dependency: Dependency,
    *,
    endpoint_identities: Mapping[str, EndpointIdentity] | None = None,
) -> ScenarioSpec | None:
    """The executable scenario proving this (change, dependency) pair, if one is bound.

    ``endpoint_identities`` (``discovery.ids.endpoint_identities``) lets a dependency keyed
    to another contract snapshot's Endpoint record match the change's endpoint.
    """
    endpoints = {
        operation_from_pointer(item.json_pointer) for item in change.evidence if item.kind == "openapi"
    }
    old_unit = classify_amount_unit(change.old_semantics)
    new_unit = classify_amount_unit(change.new_semantics)
    currency = currency_of(change.old_semantics, change.new_semantics)
    dependency_files = {item.file for item in dependency.evidence if item.kind == "source" and item.file}
    for spec in SCENARIOS.values():
        if (
            change.kind == spec.change_kind
            and change.service_id == spec.service_id == dependency.service_id
            and same_endpoint(change.endpoint_id, dependency.endpoint_id, endpoint_identities)
            and change.field_path == spec.field_path == dependency.field_path
            and (spec.method, spec.path) in endpoints
            and (old_unit, new_unit) == (spec.old_unit, spec.new_unit)
            and currency == spec.currency
            and dependency.confidence == "direct"
            and dependency.language == spec.binding.language
            and dependency.usage.operation == spec.usage_operation
            and _operand_equals(dependency.usage.operand, spec.usage_operand)
            and spec.binding.consumer_file in dependency_files
        ):
            return spec
    return None


def scenario_pointer_evidence(spec: ScenarioSpec, dependency: Dependency) -> Evidence:
    """Risk evidence recording that an executable scenario exists (it has not run yet)."""
    return Evidence(
        id=scenario_pointer_id(spec.scenario_id, dependency.id),
        kind="test",
        visibility="consumer_private",
        source_uri=f"{SCENARIO_URI_PREFIX}{spec.scenario_id}",
        revision=CATALOG_REVISION,
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=None,
        excerpt=f"Executable scenario available, not yet run: {spec.expected_behavior}",
    )


def scenario_pointer_id(scenario_id: str, dependency_id: object) -> str:
    return evidence_id("scenario-pointer", scenario_id, str(dependency_id))


def is_scenario_pointer(item: Evidence) -> bool:
    """True for evidence using the reserved ``shockwave-scenario://`` scheme (any kind)."""
    return item.source_uri.startswith(SCENARIO_URI_PREFIX)


def bound_scenario_ids(evidence: Iterable[Evidence], dependency_ids: Iterable[object]) -> list[str]:
    """Scenario ids of pointers minted by ``scenario_pointer_evidence`` for ``dependency_ids``.

    A pointer whose id is not the one this catalog derives for one of the Risk's own
    dependencies (copied from another Risk, smuggled in through declared-change
    evidence, or hand-written) proves nothing and is ignored.
    """
    dependencies = [str(item) for item in dependency_ids]
    bound: set[str] = set()
    for item in evidence:
        if item.kind != "test" or not is_scenario_pointer(item):
            continue
        scenario_id = item.source_uri[len(SCENARIO_URI_PREFIX) :]
        if any(str(item.id) == scenario_pointer_id(scenario_id, dependency) for dependency in dependencies):
            bound.add(scenario_id)
    return sorted(bound)


def scenario_ids_from_evidence(evidence: list[Evidence]) -> list[str]:
    """Scenario identifiers referenced by ``kind="test"`` pointer evidence, sorted, unique."""
    return sorted(
        {
            item.source_uri[len(SCENARIO_URI_PREFIX) :]
            for item in evidence
            if item.kind == "test" and item.source_uri.startswith(SCENARIO_URI_PREFIX)
        }
    )
