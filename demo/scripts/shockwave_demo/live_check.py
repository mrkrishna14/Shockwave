"""``live-check``: OPTIONAL Stripe TEST-MODE check of the three consumers' business functions.

It establishes only that the integrations work against Stripe's CURRENT behavior. It cannot
exercise the hypothetical change: Stripe test mode returns current-contract amounts (USD minor
units), never the simulated proposed responses.

Guards (every one is checked before any network access):

* the explicit flag ``--authorize-live-test-mode`` (the operating user's authorization);
* ``STRIPE_SECRET_KEY`` from the environment, starting with ``sk_test_``; ``sk_live_``/``rk_live_``
  keys, any other prefix and a missing key are refused. The key is never printed, logged or
  written; outputs are scanned and redacted;
* an existing test-mode PaymentIntent (``--payment-intent pi_...``), or the opt-in
  ``--create-test-payment-intent`` which creates one (USD 10.00, confirmed with Stripe's test
  payment method ``pm_card_visa``, automatic payment methods without redirects) after checking
  every parameter name, nested ones included, against the pinned OpenAPI spec's
  ``POST /v1/payment_intents`` request body (that API version has no ``payment_method_types``
  create parameter). The created object must report
  ``livemode: false``.

To create a test PaymentIntent yourself: Stripe Dashboard -> enable Test mode -> Payments ->
Create payment (USD 10.00, test card 4242 4242 4242 4242), or with the Stripe CLI logged in to a
test-mode account: ``stripe payment_intents create -d amount=1000 -d currency=usd
-d payment_method=pm_card_visa -d confirm=true -d "automatic_payment_methods[enabled]=true"
-d "automatic_payment_methods[allow_redirects]=never"`` (check the flags with
``stripe payment_intents create --help``). Only synthetic test data; no real payments.
The consumers run from the prepared repositories without STRIPE_API_BASE (so they reach
api.stripe.com in test mode); agents never run this command.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import replace

from shockwave_demo.common import (
    CONSUMER_MANIFEST_PATH,
    LOCK_PATH,
    DemoError,
    Layout,
    base_env,
    read_json,
    redact,
    run_process,
    scratch_copy,
    write_json,
)

AUTHORIZE_FLAG = "--authorize-live-test-mode"
KEY_ENV = "STRIPE_SECRET_KEY"
PAYMENT_INTENT_ID = re.compile(r"pi_[A-Za-z0-9]{6,64}")
CREATE_OPERATION = ("post", "/v1/payment_intents")
# Checked against the pinned spec before use (create_parameter_problems): in API 2026-09-30.endive the
# create form has no payment_method_types; automatic_payment_methods with allow_redirects=never confirms
# a card PaymentIntent without a return_url.
CREATE_PARAMETERS = {
    "amount": 1000,
    "currency": "usd",
    "payment_method": "pm_card_visa",
    "confirm": True,
    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
}
CANNOT_EXERCISE = (
    "Live test mode shows only that the integrations work with Stripe's CURRENT behavior (amounts in USD minor "
    "units). It cannot exercise the hypothetical change: Stripe has not proposed it, and test mode never returns "
    "the simulated proposed responses."
)
NODE_CALL = (
    "--input-type=module",
    "-e",
    (
        "import { getOrderSummary } from './src/orderSummary.ts';"
        "const summary = await getOrderSummary(process.env.SHOCKWAVE_PAYMENT_INTENT);"
        "console.log(JSON.stringify({displayTotal: summary.displayTotal, status: summary.status}));"
    ),
)
PYTHON_CALLS = {
    "shockwave-stripe-fraud": (
        "import json, os\n"
        "from fraud_service.review import review_payment\n"
        "decision = review_payment(os.environ['SHOCKWAVE_PAYMENT_INTENT'])\n"
        "print(json.dumps({'needs_manual_review': decision.needs_manual_review, 'reason': decision.reason}))\n"
    ),
    "shockwave-stripe-reporting": (
        "import json, os\n"
        "from reporting_app.summary import summarize_payment_intents\n"
        "print(json.dumps(summarize_payment_intents([os.environ['SHOCKWAVE_PAYMENT_INTENT']])))\n"
    ),
}


class LiveCheckRefused(DemoError):
    """A guard refused the live test-mode check; nothing reached the network."""


def checked_key(environ: Mapping[str, str]) -> str:
    key = environ.get(KEY_ENV, "")
    if not key:
        raise LiveCheckRefused(f"{KEY_ENV} is not set; export a Stripe TEST-mode secret key (sk_test_...)")
    if key.startswith(("sk_live_", "rk_live_")):
        raise LiveCheckRefused(f"{KEY_ENV} holds a LIVE-mode key; live-check only ever uses test-mode keys (sk_test_...)")
    if not key.startswith("sk_test_"):
        raise LiveCheckRefused(f"{KEY_ENV} must be a Stripe test-mode secret key starting with sk_test_")
    return key


def create_parameter_problems(parameters) -> list[str]:
    """Parameter names (nested ones too) missing from the pinned spec's POST /v1/payment_intents form body."""
    from shockwave_demo.inventory import raw_document

    method, path = CREATE_OPERATION
    operation = raw_document().get("paths", {}).get(path, {}).get(method, {})
    content = operation.get("requestBody", {}).get("content", {})
    schema = content.get("application/x-www-form-urlencoded", {}).get("schema", {})
    properties = schema.get("properties", {})
    if not properties:
        return [f"the pinned spec has no form body for {method.upper()} {path}"]
    problems = []
    values = parameters if isinstance(parameters, Mapping) else dict.fromkeys(parameters)
    for name, value in values.items():
        schema = properties.get(name)
        if schema is None:
            problems.append(f"{name} is not a {method.upper()} {path} parameter in the pinned spec")
            continue
        for child in value if isinstance(value, Mapping) else ():
            if child not in schema.get("properties", {}):
                problems.append(f"{name}[{child}] is not a {method.upper()} {path} parameter in the pinned spec")
    return problems


def check_request(*, authorized: bool, payment_intent: str | None, create: bool, environ: Mapping[str, str]) -> str:
    """Every guard, in order; returns the key (never shown). Raises LiveCheckRefused."""
    if not authorized:
        raise LiveCheckRefused(
            f"live-check talks to Stripe TEST mode and runs only with {AUTHORIZE_FLAG} (the operating user's explicit "
            "authorization). " + CANNOT_EXERCISE
        )
    key = checked_key(environ)
    if payment_intent and create:
        raise LiveCheckRefused("pass either --payment-intent or --create-test-payment-intent, not both")
    if not payment_intent and not create:
        raise LiveCheckRefused(
            "name an existing test-mode PaymentIntent with --payment-intent pi_... (create one in the Stripe Dashboard "
            "in Test mode, or with the Stripe CLI), or opt in to --create-test-payment-intent"
        )
    if payment_intent and not PAYMENT_INTENT_ID.fullmatch(payment_intent):
        raise LiveCheckRefused("--payment-intent must look like pi_ followed by letters and digits")
    if create:
        problems = create_parameter_problems(CREATE_PARAMETERS)
        if problems:
            raise LiveCheckRefused("refusing to create a PaymentIntent: " + "; ".join(problems))
    return key


def _create_payment_intent(key: str) -> str:
    """Create a confirmed USD 10.00 test-mode PaymentIntent (network; only after every guard passed)."""
    from stripe import StripeClient

    client = StripeClient(key, stripe_version=read_json(LOCK_PATH)["api_version"], max_network_retries=0)
    intent = client.v1.payment_intents.create(params=dict(CREATE_PARAMETERS))
    if getattr(intent, "livemode", True) is not False:
        raise LiveCheckRefused("Stripe reported livemode != false for the created PaymentIntent; stopping")
    return intent.id


def run_live_check(
    layout: Layout,
    *,
    authorized: bool,
    payment_intent: str | None,
    create: bool,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    environ = os.environ if environ is None else environ
    key = check_request(authorized=authorized, payment_intent=payment_intent, create=create, environ=environ)
    from shockwave_demo.prepare import load_snapshots

    _, repositories = load_snapshots(layout)
    guarded = replace(layout, extra_redactions=(*layout.extra_redactions, key))
    return run_consumers(guarded, key, repositories, payment_intent or _create_payment_intent(key))


def run_consumers(layout: Layout, key: str, repositories: Mapping, payment_intent: str) -> list[str]:
    """Each consumer's business function against Stripe test mode (no STRIPE_API_BASE), on a scratch copy."""
    lines = ["Live check - Stripe TEST mode (authorized by the operating user)", f"  {CANNOT_EXERCISE}"]
    results = {}
    manifest = {entry["name"]: entry for entry in read_json(CONSUMER_MANIFEST_PATH)["repositories"]}
    scratch = layout.work_dir / "live-check"
    for name in repositories:
        copy = scratch_copy(layout.repo(name), scratch / name, inside=layout.work_dir)
        if manifest[name]["language"] == "typescript":
            argv = [layout.node, *NODE_CALL]
        else:
            argv = [sys.executable, "-c", PYTHON_CALLS[name]]
        env = base_env({KEY_ENV: key, "SHOCKWAVE_PAYMENT_INTENT": payment_intent}, home=scratch)
        completed = run_process(argv, copy, env=env, timeout=120)
        output, _ = redact(completed.stdout, layout)
        results[name] = {"exit_code": completed.returncode, "output_tail": output[-2000:]}
        last = output.strip().splitlines()[-1] if output.strip() else ""
        lines.append(f"  {name}: exit {completed.returncode}: {last[:200]}")
    write_json(
        scratch / "live-check.json",
        {
            "kind": "shockwave-demo.live-check@1",
            "payment_intent": payment_intent,
            "note": CANNOT_EXERCISE,
            "results": results,
        },
    )
    lines.append("  wrote $WORK_DIR/live-check/live-check.json (the key is never written)")
    return lines
