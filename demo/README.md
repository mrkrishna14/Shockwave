# Shockwave Stripe demo

Shockwave shows a provider what a proposed API change would break downstream, before it ships. This
demo uses **Stripe** as the provider.

**What's real:**
- Stripe's public OpenAPI contract, pinned and unmodified.
- The official Stripe SDKs, sending real HTTP requests to a local fixture server.
- Three team-created consumer apps whose own code and tests are executed.

**What's simulated, and labeled everywhere:**
- **The change.** For USD, PaymentIntent `amount` switches from cents to dollars. This is
  **hypothetical**: Stripe has not proposed or announced it, Stripe is not a Shockwave customer,
  and Stripe has not endorsed Shockwave.
- **The responses.** A local fixture server stands in for Stripe.

Everything runs offline, with no Stripe credentials and no LLM.

## Setup (once)

From the repository root:

```sh
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python demo/scripts/demo.py prepare      # first run only: needs network for npm (stripe@23.0.0)
```

`prepare` does the following:
- builds the three consumer apps as local git repositories (fixed commits, no remote);
- installs `stripe@23.0.0` for the TypeScript app;
- runs each app's own test suite.

After that, everything runs offline from the npm cache. Node 24 comes from `.venv/bin/node`
(`nodejs-wheel`), because the system Node may be too old or the wrong architecture.

## Run the whole demo

```sh
.venv/bin/python demo/scripts/demo.py all      # about 20 s
```

`all` runs every step and ends with `results/ is byte-identical to demo/expected/results`. Expected
results:

| Step | What you see |
|---|---|
| inventory | The pinned Stripe spec (commit `969d8c3`, API `2026-09-30.endive`, sha256 matches the lock). It reports the measured **644 operations over 454 paths** and 1701 schemas, and **provider source access: unavailable**. |
| prepare | Three apps with their commits. The apps' own suites pass: checkout 4/4, fraud 5/5, reporting 3/3. |
| scan | Fraud: **real scan** by Noirit's scanner. Checkout and reporting: the line-verified **FIXTURE**, with the use sites the scanner missed listed. |
| submit-change | Schema diff alone: **0 changes**; with the declaration: 1 change. Predicted risks: conversion high (checkout), comparison high (fraud), storage medium (reporting). |
| execute | Checkout: **verified_regression**, "expected $10.00, actual $0.10". Fraud: **verified_regression**, a $6,000 payment is no longer flagged. Reporting summary: the **control passes** on both response versions. Ledger storage: **not_run** (a potential risk, with the missing evidence listed). |
| validate-migrations | Both patches are **validated** with the unchanged expectations. Run against today's responses, the patched code fails ($1000.00), which is recorded as the deployment caveat, not a regression. |

## Step by step

| Command | What it does |
|---|---|
| `demo.py fetch-sources [--network]` | Checks the pinned Stripe sources offline. `--network` is a read-only shallow fetch from public GitHub that re-verifies commits, licenses and versions. |
| `demo.py prepare [--offline]` | Builds the consumer repositories and runs their own suites. |
| `demo.py inventory` | Parses and measures the authentic Stripe contract. |
| `demo.py fixture-server --variant baseline\|proposed [--port 8765]` | Serves the fixture responses on 127.0.0.1 for live poking with `curl` or the SDKs. |
| `demo.py scan [--use-fixture]` | Collects consumer dependencies (scanner, or the labeled fixture). |
| `demo.py submit-change` | Submits the simulated change manifest: `detect_changes` + `analyze_impact`. |
| `demo.py execute` | Runs the old and proposed scenarios, plus the reporting control. |
| `demo.py validate-migrations` | Applies and tests the prepared migration patches. |
| `demo.py reset [--dry-run]` | Deletes only generated state (`demo/.work`, generated `demo/reports`). |
| `demo.py live-check --authorize-live-test-mode ...` | Optional Stripe **test mode** check. Needs `STRIPE_SECRET_KEY=sk_test_...` and refuses live keys. It can't exercise the hypothetical change. |

Outputs:
- `demo/reports/results/` holds the dashboard-compatible JSON: the contract records, private
  per-consumer findings and the provider's restricted summary (no paths, code or logs).
- `demo/reports/runs/` holds the run records: commands, test counts, assertion evidence, commits,
  fixture hashes and bounded logs.

The file shapes are documented in `demo/reports/README.md`.

## What's in here

| Path | Contents |
|---|---|
| `stripe-sources.lock.json` | Verified pins for `stripe/openapi`, `stripe-python` 16.0.0, `stripe-node` 23.0.0, `stripe-mock` (evaluated and rejected: hardcoded responses) and `accept-a-payment` (reference only; it pins the older API `2023-10-16`). |
| `templates/` | The three consumer apps: `checkout` (TypeScript, `stripe.paymentIntents.retrieve`), `fraud` (Python, `stripe.PaymentIntent.retrieve`) and `reporting` (Python, `StripeClient.v1.payment_intents.retrieve`). |
| `stripe-fixtures/` | The baseline and proposed response sets. Whole-dollar USD values keep Stripe's integer type; EUR is unchanged. |
| `scenarios/` | The simulated-change manifest: provider, status, endpoint, field, old/new semantics, USD scope, schema unchanged, limits. |
| `expected/` | The ID file, the dependency fixture (with negative controls) and the reference results. |
| `migrations/` | The two targeted patches and their metadata. |
| `github/`, `GITHUB_SETUP.md` | Private GitHub onboarding, prepared but not executed. |
| `DEMO_SCRIPT.md` | The three-minute walkthrough. |
| `HANDOFF.md` | What's real, what's temporary, and next steps for each teammate. |

## Limits

- **Whole-dollar fixtures.** They don't establish a practical dollars representation for arbitrary
  amounts or other currencies.
- **Not a sandbox.** Runs use the local runner double: a plain subprocess on trusted fixtures, not
  Sai's isolated runner.
- **No identity isolation proof.** This is a local, single-machine demo, so it doesn't prove
  cross-company identity isolation.
