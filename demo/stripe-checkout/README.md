# Shockwave Stripe checkout demo

**Know your API’s downstream impact.**

This team-owned TypeScript consumer calls the official `stripe@23.0.0` SDK's
`paymentIntents.retrieve` method. The scenario intercepts the SDK's fetch transport,
serving either a current-contract response (`amount=1000`, USD cents) or a
**hypothetical** proposed response (`amount=10`, USD dollars). The latter is not an
announced Stripe API change. Both represent a $10.00 payment.

The fixture uses a visibly fake credential and requires no Stripe account. The
harness intercepts HTTP; its in-process network guard is not a security sandbox.
Do not adapt this trusted fixture registration to arbitrary consumer code.

## Run from the repository root

Requires Python 3.11+ and Node 22.18+ (Node 24 recommended for this fixture).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-integration.txt
npm ci --ignore-scripts --no-audit --no-fund --prefix demo/stripe-checkout
PYTHONPATH=app .venv/bin/python -m migration.stripe_demo
.venv/bin/python -m pytest -q
```

The local demo writes `.shockwave-demo/result.json`, `migration.patch`, and private
execution artifacts. A nonzero demo exit means the fix was not validated. It leaves
the checked-in consumer unchanged and sends no emails or GitHub writes.

## What the run proves

1. A clearly labeled scanner-output fixture describes `payment.amount / 100` in
   `src/checkout.ts`. This is not a claim that the missing scanner is implemented.
2. Kanishk's analyzer predicts a conversion risk and selects the Stripe scenario.
3. Sai's runner stages Kanishk's native harness in a temporary copy. The real SDK
   requests `/v1/payment_intents/pi_demo`, with responses intercepted by the harness.
4. With the current response, the checkout renders `$10.00`. With the simulated
   response, it renders `$0.10`, proving the regression in this scenario.
5. The migration removes only the approved `payment.amount / 100` conversion.
6. Both the display invariant and the unchanged local-cents test pass against the
   simulated proposed response. Unrelated `amount / 100` arithmetic remains intact.
7. Every request/result uses Ishan's actual Pydantic schemas.

The patch targets only the proposed contract: applying it while still receiving
current Stripe responses produces the wrong amount. It must not be deployed to a
real Stripe consumer as a production migration.

## Deliberate limits

- The dependency record and all-`a` commit SHA are local demo placeholders, not a
  real scanner result or fetched GitHub commit.
- No live GitHub App, invitation delivery, dashboard wiring, or publication is
  configured by this script. Publication tests use an HTTP double.
- Verification covers this checkout display and its local-currency control only.
  Fraud thresholds and reporting/storage transformations need separate scenarios.
- Installing dependencies is a setup step, outside execution. The runner never
  installs packages. The package lock pins the official SDK.
- Untrusted/sandbox execution remains blocked. Revocation and artifact visibility
  remain responsibilities of the backend's authorization callbacks.
