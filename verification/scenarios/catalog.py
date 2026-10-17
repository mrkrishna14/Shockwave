"""Executable regression scenarios, control scenarios and provider response fixtures.

A scenario couples a provider response fixture pair (current vs proposed contract), an
invariant business expectation, a native test harness, and a binding to a known consumer
entry point. This module only describes scenarios; Sai's runner executes them (see
``verification.scenarios.staging`` for the runner-facing protocol).

Two kinds of scenario exist:

* ``risk`` scenarios prove a predicted Risk. Scenario selection is decided during impact
  analysis, when the change and the dependency are both known, and recorded on the Risk as
  a ``kind="test"`` Evidence pointer (``shockwave-scenario://<scenario_id>``).
  ``build_verification_plan`` later resolves that pointer, so it needs nothing beyond the
  contract's (risk, snapshot). A scenario has a category - ``divide`` usage proves a
  ``conversion`` risk, ``compare`` usage a ``comparison`` risk - and a pointer is valid
  only on a Risk of that category, minted for one of the Risk's own dependencies. Risks
  without a pointer (storage-only, unbound consumers) have no executable proof and stay
  ``not_run``.
* ``control`` scenarios are negative controls: an unaffected consumer path that must keep
  passing under both fixture variants. They are never bound to a Risk
  (``find_applicable_scenario`` never returns one and no pointer is minted for one) and
  run through ``verification.scenarios.controls``, which records no contract records.

Two harness protocols exist. The Demo Payments display scenarios (fictional provider, kept
for the original suite) use one assertion (``assertion_id``/``expected_display``) and a
faked HTTP layer. Case scenarios (``cases``) evaluate several calls of the bound entry
point; exactly one case of a risk scenario is ``relevant`` (the assertion the change is
predicted to break) and the others are guards. The Stripe scenarios use case harnesses
with the ``stripe_sdk_env`` binding: the harness starts an in-process loopback fixture
server that serves the staged fixture set, sets the consumer's Stripe key variable to a
dummy test-mode key and its API base variable to that server BEFORE importing and calling
the consumer's real entry point, which constructs the official Stripe SDK client itself.
The fixture server simulates Stripe responses; it is not Stripe and not another provider.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from analyzer.units import AmountUnit, classify_amount_unit, currency_of
from discovery.contracts import APIChange, Dependency, Evidence
from discovery.ids import EndpointIdentity, evidence_id, same_endpoint
from discovery.records import operation_from_pointer

SCENARIO_URI_PREFIX = "shockwave-scenario://"
# The catalog's current revision. A pointer records the revision of its own scenario's
# definition (ScenarioSpec.revision), so adding scenarios never changes existing pointers.
CATALOG_REVISION = "shockwave-scenarios@2026-10-17.1"
DEMO_PAYMENTS_REVISION = "shockwave-scenarios@2026-10-16.1"
STRIPE_REVISION = "shockwave-scenarios@2026-10-17.1"
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STAGING_DIR = ".shockwave/scenarios"

RunnerProfile = Literal["trusted_python", "trusted_node", "sandbox_python", "sandbox_node"]
ScenarioKind = Literal["risk", "control"]
ScenarioCategory = Literal["conversion", "comparison"]
UsageOperation = Literal["divide", "compare"]
HttpInjection = Literal["python_requests_get", "js_global_fetch", "stripe_sdk_env"]

# The Risk category a scenario proves, by the dependency usage it is bound to.
USAGE_CATEGORY: dict[str, ScenarioCategory] = {"divide": "conversion", "compare": "comparison"}
SCENARIO_CATEGORIES: tuple[ScenarioCategory, ...] = ("conversion", "comparison")

# Stripe (public contract pinned in demo/stripe-sources.lock.json). The proposed fixture set
# is a SIMULATED change (not announced by Stripe); see the demo manifest.
STRIPE_SERVICE_ID = "stripe"
STRIPE_RETRIEVE_PATH = "/v1/payment_intents/{intent}"
STRIPE_FIXTURE_BASELINE = "stripe.payment_intents.usd-whole-dollars.baseline"
STRIPE_FIXTURE_PROPOSED = "stripe.payment_intents.usd-whole-dollars.proposed"
STRIPE_CHECKOUT_SCENARIO = "stripe.payment-intent-amount-units.checkout-display.typescript"
STRIPE_FRAUD_SCENARIO = "stripe.payment-intent-amount-units.fraud-threshold.python"
STRIPE_REPORTING_CONTROL = "stripe.payment-intent.reporting-summary.control.python"
STRIPE_DEMO_PAYMENT_INTENTS = ("pi_demo_10usd", "pi_demo_4000usd", "pi_demo_5000usd", "pi_demo_6000usd", "pi_demo_25eur")
STRIPE_KEY_ENV = "STRIPE_SECRET_KEY"
STRIPE_API_BASE_ENV = "STRIPE_API_BASE"


class UnknownScenario(KeyError):
    """Raised for a scenario or fixture identifier that is not in this catalog."""


def canonical_json(value: object) -> str:
    """The one JSON text used to compare expected and actual case values (strict: True != 1)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class ResponseFixture:
    fixture_id: str
    service_id: str
    method: str
    path: str
    contract: Literal["current", "proposed"]
    filename: str  # relative to FIXTURES_DIR, or to REPO_ROOT when location == "repository"
    location: Literal["catalog", "repository"] = "catalog"
    # "single_response": {fixture_id, ..., response: {body}} (Demo Payments);
    # "response_set": {fixture_set_id, api_version, operation, responses: {<id>: body}} (Stripe).
    layout: Literal["single_response", "response_set"] = "single_response"


@dataclass(frozen=True)
class ConsumerBinding:
    """A known consumer entry point that consumes the dependent value."""

    language: Literal["python", "typescript", "javascript"]
    consumer_file: str  # consumer-repository-relative file containing the dependency
    module: str  # Python dotted module, or JS/TS file path relative to the repository root
    function: str
    args: tuple[str, ...]  # display protocol only; case scenarios carry arguments per case
    http_injection: HttpInjection
    api_key_env: str | None = None  # stripe_sdk_env: the variable the consumer reads its Stripe key from
    api_base_env: str | None = None  # stripe_sdk_env: the variable the consumer reads its API base URL from


@dataclass(frozen=True)
class ScenarioCase:
    """One evaluated call of the bound entry point.

    JSON values are kept as canonical JSON text so a spec stays immutable and hashable;
    ``args``/``expected`` decode them. ``result_path`` reads attributes or keys of the
    returned value (empty: the value itself). ``resource_ids`` are the path parameter values
    the call must request: each at least once, and nothing else (the run's preconditions).
    """

    case_id: str
    relevant: bool
    description: str
    args_json: str
    result_path: tuple[str, ...]
    expected_json: str
    resource_ids: tuple[str, ...]

    @property
    def args(self) -> list:
        return json.loads(self.args_json)

    @property
    def expected(self) -> object:
        return json.loads(self.expected_json)


def scenario_case(
    case_id: str,
    *,
    relevant: bool,
    description: str,
    args: Sequence[object],
    result_path: Sequence[str],
    expected: object,
    resource_ids: Sequence[str],
) -> ScenarioCase:
    return ScenarioCase(
        case_id=case_id,
        relevant=relevant,
        description=description,
        args_json=canonical_json(list(args)),
        result_path=tuple(result_path),
        expected_json=canonical_json(expected),
        resource_ids=tuple(resource_ids),
    )


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    title: str
    service_id: str
    method: str
    path: str
    field_path: str  # the changed field (for a control: the field its unaffected path does not read)
    change_kind: Literal["units_changed"]
    old_unit: AmountUnit
    new_unit: AmountUnit
    currency: str
    usage_operation: UsageOperation | None  # None for controls
    usage_operand: int | None
    binding: ConsumerBinding
    old_fixture_id: str
    new_fixture_id: str
    assertion_id: str  # the relevant assertion (case scenarios: the relevant case; controls: the control case)
    expected_display: str | None  # display protocol only
    expected_behavior: str
    harness_asset: str  # file under ASSETS_DIR
    staged_test_name: str  # file name used inside the staged scenario directory
    trusted_profile: RunnerProfile
    sandbox_profile: RunnerProfile
    timeout_seconds: int
    additional_test_files: tuple[str, ...] = ()
    kind: ScenarioKind = "risk"
    cases: tuple[ScenarioCase, ...] = ()
    revision: str = CATALOG_REVISION  # catalog revision of this scenario's definition

    def __post_init__(self) -> None:
        if self.kind == "risk" and self.usage_operation not in USAGE_CATEGORY:
            raise ValueError(f"{self.scenario_id}: a risk scenario needs a divide or compare usage")
        if self.kind == "control" and (self.usage_operation is not None or not self.cases):
            raise ValueError(f"{self.scenario_id}: a control has no usage binding and at least one case")
        if not self.cases:
            if self.expected_display is None:
                raise ValueError(f"{self.scenario_id}: a display scenario needs expected_display")
            return
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError(f"{self.scenario_id}: case ids must be unique")
        relevant = [case.case_id for case in self.cases if case.relevant]
        if self.kind == "risk" and relevant != [self.assertion_id]:
            raise ValueError(f"{self.scenario_id}: exactly one relevant case, named by assertion_id")
        if self.kind == "control" and (relevant or self.assertion_id not in case_ids):
            raise ValueError(f"{self.scenario_id}: a control has no relevant case; assertion_id names a case")
        if self.binding.http_injection == "stripe_sdk_env" and not (
            self.binding.api_key_env and self.binding.api_base_env
        ):
            raise ValueError(f"{self.scenario_id}: stripe_sdk_env names the key and API base variables")

    @property
    def category(self) -> ScenarioCategory | None:
        """The Risk category this scenario proves (None for a control)."""
        if self.kind != "risk" or self.usage_operation is None:
            return None
        return USAGE_CATEGORY[self.usage_operation]

    @property
    def relevant_case(self) -> ScenarioCase | None:
        return next((case for case in self.cases if case.relevant), None)

    @property
    def guard_cases(self) -> tuple[ScenarioCase, ...]:
        return tuple(case for case in self.cases if not case.relevant) if self.kind == "risk" else ()

    def case_role(self, case: ScenarioCase) -> Literal["relevant", "guard", "control"]:
        if self.kind == "control":
            return "control"
        return "relevant" if case.relevant else "guard"

    def fixture_for(self, variant: str) -> str:
        """The fixture of a variant: "baseline" -> current contract, "proposed" -> proposed."""
        if variant == "baseline":
            return self.old_fixture_id
        if variant == "proposed":
            return self.new_fixture_id
        raise ValueError(f"variant must be baseline or proposed, got {variant!r}")

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
        # Canonical demo fixture sets (synthetic responses shaped by Stripe's published
        # PaymentIntent schema; the proposed set is the SIMULATED change). Not recorded from Stripe.
        ResponseFixture(
            fixture_id=STRIPE_FIXTURE_BASELINE,
            service_id=STRIPE_SERVICE_ID,
            method="GET",
            path=STRIPE_RETRIEVE_PATH,
            contract="current",
            filename="demo/stripe-fixtures/baseline/payment_intents.json",
            location="repository",
            layout="response_set",
        ),
        ResponseFixture(
            fixture_id=STRIPE_FIXTURE_PROPOSED,
            service_id=STRIPE_SERVICE_ID,
            method="GET",
            path=STRIPE_RETRIEVE_PATH,
            contract="proposed",
            filename="demo/stripe-fixtures/proposed/payment_intents.json",
            location="repository",
            layout="response_set",
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
        revision=DEMO_PAYMENTS_REVISION,
    )


def _stripe_binding(
    language: Literal["python", "typescript"], consumer_file: str, module: str, function: str
) -> ConsumerBinding:
    return ConsumerBinding(
        language=language,
        consumer_file=consumer_file,
        module=module,
        function=function,
        args=(),
        http_injection="stripe_sdk_env",
        api_key_env=STRIPE_KEY_ENV,
        api_base_env=STRIPE_API_BASE_ENV,
    )


def _stripe_scenario(**fields: object) -> ScenarioSpec:
    """A Stripe PaymentIntent scenario; ``fields`` adds the scenario-specific values."""
    python = fields.pop("python")
    common: dict[str, object] = {
        "service_id": STRIPE_SERVICE_ID,
        "method": "GET",
        "path": STRIPE_RETRIEVE_PATH,
        "field_path": "response.amount",
        "change_kind": "units_changed",
        "old_unit": "minor",
        "new_unit": "major",
        "currency": "usd",
        "old_fixture_id": STRIPE_FIXTURE_BASELINE,
        "new_fixture_id": STRIPE_FIXTURE_PROPOSED,
        "expected_display": None,
        "harness_asset": "python_stripe_harness.py" if python else "node_stripe_harness.mjs",
        "staged_test_name": "test_shockwave_stripe_cases.py" if python else "shockwave_stripe_cases.test.mjs",
        "trusted_profile": "trusted_python" if python else "trusted_node",
        "sandbox_profile": "sandbox_python" if python else "sandbox_node",
        "timeout_seconds": 60,
        "revision": STRIPE_REVISION,
    }
    return ScenarioSpec(**common, **fields)  # type: ignore[arg-type]


_SUMMARY_EXPECTED = {"eur": {"requires_capture": 1}, "usd": {"processing": 1, "succeeded": 3}}

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
        # Noirit's team-owned demo-payments consumers (examples/consumers), bound at the file
        # holding the '/ 100' use site that the scanner reports.
        _display_scenario(
            scenario_id="demo-payments.charge-amount-units.display.python-display",
            title="Noirit's Python display consumer survives the cents-to-dollars change",
            binding=ConsumerBinding(
                language="python",
                consumer_file="demo_display/charges.py",
                module="demo_display.charges",
                function="fetch_charge_display",
                args=("ch_demo",),
                http_injection="python_requests_get",
            ),
            harness_asset="python_display_harness.py",
            staged_test_name="test_shockwave_display_invariant.py",
            trusted_profile="trusted_python",
            sandbox_profile="sandbox_python",
        ),
        _display_scenario(
            scenario_id="demo-payments.charge-amount-units.display.typescript-display",
            title="Noirit's TypeScript display consumer survives the cents-to-dollars change",
            binding=ConsumerBinding(
                language="typescript",
                consumer_file="src/charges.ts",
                module="src/charges.ts",
                function="fetchChargeDisplay",
                args=("ch_demo",),
                http_injection="js_global_fetch",
            ),
            harness_asset="node_display_harness.mjs",
            staged_test_name="shockwave_display_invariant.test.mjs",
            trusted_profile="trusted_node",
            sandbox_profile="sandbox_node",
        ),
        _stripe_scenario(
            python=False,
            scenario_id=STRIPE_CHECKOUT_SCENARIO,
            title="Checkout order summary keeps showing the PaymentIntent total in dollars (stripe-node)",
            usage_operation="divide",
            usage_operand=100,
            binding=_stripe_binding("typescript", "src/orderSummary.ts", "src/orderSummary.ts", "getOrderSummary"),
            assertion_id="displayed_total_usd",
            expected_behavior=(
                "GET /v1/payment_intents/{intent} through the official stripe-node SDK: getOrderSummary('pi_demo_10usd') "
                "displays $10.00 for both the baseline response (amount=1000, USD minor units) and the equivalent "
                "SIMULATED proposed response (amount=10, USD major units); guard: the unrelated basis-points tax note "
                "stays 'Includes 8.25% sales tax'."
            ),
            cases=(
                scenario_case(
                    "displayed_total_usd",
                    relevant=True,
                    description="getOrderSummary('pi_demo_10usd').displayTotal is $10.00 (a USD $10.00 PaymentIntent)",
                    args=["pi_demo_10usd"],
                    result_path=["displayTotal"],
                    expected="$10.00",
                    resource_ids=["pi_demo_10usd"],
                ),
                scenario_case(
                    "tax_note_unchanged",
                    relevant=False,
                    description=(
                        "getOrderSummary('pi_demo_10usd').taxNote keeps the basis-points conversion (825 / 100 = 8.25%), "
                        "an unrelated division by 100"
                    ),
                    args=["pi_demo_10usd"],
                    result_path=["taxNote"],
                    expected="Includes 8.25% sales tax",
                    resource_ids=["pi_demo_10usd"],
                ),
            ),
        ),
        _stripe_scenario(
            python=True,
            scenario_id=STRIPE_FRAUD_SCENARIO,
            title="Fraud review keeps flagging payments above USD $5,000 (stripe-python)",
            usage_operation="compare",
            usage_operand=500000,
            binding=_stripe_binding("python", "fraud_service/review.py", "fraud_service.review", "review_payment"),
            assertion_id="above_threshold_flagged",
            expected_behavior=(
                "GET /v1/payment_intents/{intent} through the official stripe-python SDK: review_payment flags USD "
                "$6,000.00 (pi_demo_6000usd) for manual review and flags neither $4,000.00 nor exactly $5,000.00, for "
                "both the baseline responses (USD minor units: 600000/400000/500000) and the equivalent SIMULATED "
                "proposed responses (USD major units: 6000/4000/5000)."
            ),
            cases=(
                scenario_case(
                    "below_threshold_not_flagged",
                    relevant=False,
                    description="review_payment('pi_demo_4000usd') does not flag USD $4,000.00",
                    args=["pi_demo_4000usd"],
                    result_path=["needs_manual_review"],
                    expected=False,
                    resource_ids=["pi_demo_4000usd"],
                ),
                scenario_case(
                    "at_threshold_not_flagged",
                    relevant=False,
                    description="review_payment('pi_demo_5000usd') does not flag exactly USD $5,000.00 (not above)",
                    args=["pi_demo_5000usd"],
                    result_path=["needs_manual_review"],
                    expected=False,
                    resource_ids=["pi_demo_5000usd"],
                ),
                scenario_case(
                    "above_threshold_flagged",
                    relevant=True,
                    description="review_payment('pi_demo_6000usd') flags USD $6,000.00 for manual review",
                    args=["pi_demo_6000usd"],
                    result_path=["needs_manual_review"],
                    expected=True,
                    resource_ids=["pi_demo_6000usd"],
                ),
            ),
        ),
        _stripe_scenario(
            python=True,
            kind="control",
            scenario_id=STRIPE_REPORTING_CONTROL,
            title="Reporting currency/status summary is unaffected by the amount units (control)",
            usage_operation=None,
            usage_operand=None,
            binding=_stripe_binding(
                "python", "reporting_app/summary.py", "reporting_app.summary", "summarize_payment_intents"
            ),
            assertion_id="summary_counts_by_currency_and_status",
            expected_behavior=(
                "CONTROL, not tied to any risk: summarize_payment_intents over the five demo PaymentIntents (official "
                "stripe-python StripeClient) counts payments by currency and status, reads neither amount nor units, "
                "and returns the same summary for the baseline and the SIMULATED proposed fixture set."
            ),
            cases=(
                scenario_case(
                    "summary_counts_by_currency_and_status",
                    relevant=False,
                    description="summarize_payment_intents(<5 demo PaymentIntents>) counts by currency and status",
                    args=[list(STRIPE_DEMO_PAYMENT_INTENTS)],
                    result_path=[],
                    expected=_SUMMARY_EXPECTED,
                    resource_ids=STRIPE_DEMO_PAYMENT_INTENTS,
                ),
            ),
        ),
    )
}

CONTROL_SCENARIO_IDS: tuple[str, ...] = tuple(
    scenario_id for scenario_id, spec in SCENARIOS.items() if spec.kind == "control"
)


# Hypothetical demo change. These fixtures do not describe an announced Stripe change.
for _contract, _suffix in [('current', 'current-cents'), ('proposed', 'simulated-dollars')]:
    _id = f'stripe.payment-intent.pi_demo.{_suffix}'
    FIXTURES[_id] = ResponseFixture(_id, 'stripe', 'GET', '/v1/payment_intents/{intent}',
                                   _contract, _id + '.json')

_STRIPE = ScenarioSpec(
    scenario_id='stripe.payment-intent-amount-units.simulated.typescript',
    title='SIMULATED Stripe amount-unit change breaks a TypeScript checkout',
    service_id='stripe', method='GET', path='/v1/payment_intents/{intent}',
    field_path='response.amount', change_kind='units_changed', old_unit='minor',
    new_unit='major', currency='usd', usage_operation='divide', usage_operand=100,
    binding=ConsumerBinding(language='typescript', consumer_file='src/checkout.ts',
        module='src/checkout.ts', function='displayPaymentIntent', args=('pi_demo',),
        http_injection='js_global_fetch'),
    old_fixture_id='stripe.payment-intent.pi_demo.current-cents',
    new_fixture_id='stripe.payment-intent.pi_demo.simulated-dollars',
    assertion_id='displayed_price_usd', expected_display='$10.00',
    expected_behavior='SIMULATED change: the official Stripe SDK retrieves a PaymentIntent; '
        'the checkout must display $10.00 for current amount=1000 and hypothetical amount=10. '
        'This is not an announced Stripe API change.',
    harness_asset='node_display_harness.mjs',
    staged_test_name='shockwave_display_invariant.test.mjs',
    trusted_profile='trusted_node', sandbox_profile='sandbox_node', timeout_seconds=30,
    additional_test_files=('test/local.test.mjs',),
)
SCENARIOS[_STRIPE.scenario_id] = _STRIPE


def get_scenario(scenario_id: str) -> ScenarioSpec:
    try:
        return SCENARIOS[scenario_id]
    except KeyError:
        raise UnknownScenario(scenario_id) from None


def get_control(scenario_id: str) -> ScenarioSpec:
    """A control scenario; UnknownScenario for anything else (risk scenarios included)."""
    spec = get_scenario(scenario_id)
    if spec.kind != "control":
        raise UnknownScenario(f"{scenario_id} is a risk scenario, not a control")
    return spec


def get_fixture(fixture_id: str) -> ResponseFixture:
    try:
        return FIXTURES[fixture_id]
    except KeyError:
        raise UnknownScenario(fixture_id) from None


def fixture_path(fixture_id: str) -> Path:
    fixture = get_fixture(fixture_id)
    return (REPO_ROOT if fixture.location == "repository" else FIXTURES_DIR) / fixture.filename


def load_fixture(fixture_id: str) -> dict:
    """The fixture document (Demo Payments: ``response.body``; Stripe sets: ``responses``)."""
    return json.loads(fixture_path(fixture_id).read_text(encoding="utf-8"))


def fixture_sha256(fixture_id: str) -> str:
    """SHA-256 of the canonical fixture file: the fixture revision a staged run must report."""
    return hashlib.sha256(fixture_path(fixture_id).read_bytes()).hexdigest()


def fixture_api_version(fixture_id: str) -> str | None:
    """The provider API version a response-set fixture simulates (None when it names none)."""
    version = load_fixture(fixture_id).get("api_version")
    return version if isinstance(version, str) and version else None


def case_request_paths(spec: ScenarioSpec, case: ScenarioCase) -> tuple[str, ...]:
    """The concrete request paths a case must produce: the operation path per resource id."""
    segments = spec.path.split("/")
    templated = [index for index, segment in enumerate(segments) if segment.startswith("{") and segment.endswith("}")]
    if len(templated) != 1:
        raise ValueError(f"{spec.scenario_id}: the operation path needs exactly one path parameter")
    paths = []
    for resource_id in case.resource_ids:
        concrete = list(segments)
        concrete[templated[0]] = resource_id
        paths.append("/".join(concrete))
    return tuple(paths)


def _operand_equals(operand: object, expected: int | None) -> bool:
    if isinstance(operand, bool) or operand is None or expected is None:
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
    """The executable risk scenario proving this (change, dependency) pair, if one is bound.

    ``endpoint_identities`` (``discovery.ids.endpoint_identities``) lets a dependency keyed
    to another contract snapshot's Endpoint record match the change's endpoint. Controls
    never match: they are not tied to Risks.
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
            spec.kind == "risk"
            and change.kind == spec.change_kind
            and change.service_id == spec.service_id == dependency.service_id
            and same_endpoint(change.endpoint_id, dependency.endpoint_id, endpoint_identities)
            and change.field_path == spec.field_path == dependency.field_path
            and (spec.method, spec.path) in endpoints
            and (old_unit, new_unit) == (spec.old_unit, spec.new_unit)
            and currency == spec.currency
            and dependency.confidence in ("direct", "inferred")  # running the scenario settles an inferred link
            and dependency.language == spec.binding.language
            and dependency.usage.operation == spec.usage_operation
            and _operand_equals(dependency.usage.operand, spec.usage_operand)
            and spec.binding.consumer_file in dependency_files
        ):
            return spec
    return None


def scenario_pointer_evidence(spec: ScenarioSpec, dependency: Dependency) -> Evidence:
    """Risk evidence recording that an executable scenario exists (it has not run yet)."""
    if spec.kind != "risk":
        raise ValueError(f"{spec.scenario_id} is a control scenario; controls are never bound to a Risk")
    return Evidence(
        id=scenario_pointer_id(spec.scenario_id, dependency.id),
        kind="test",
        visibility="consumer_private",
        source_uri=f"{SCENARIO_URI_PREFIX}{spec.scenario_id}",
        revision=spec.revision,
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


def pointer_scenario_id(item: Evidence) -> str | None:
    """The scenario id a reserved-scheme evidence item names (None for other evidence)."""
    return item.source_uri[len(SCENARIO_URI_PREFIX) :] if is_scenario_pointer(item) else None


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
