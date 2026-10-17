# Stripe demo handoff (Kanishk → Noirit, Sai, Ishan)

Checkpoint 2026-10-17 on `main`. To verify, run from the repository root:
`.venv/bin/python demo/scripts/demo.py reset && .venv/bin/python demo/scripts/demo.py all`.
It exits 0 and the results are byte-identical to `demo/expected/results`.

## Exact results (offline, verified)

**Inventory**
- Stripe OpenAPI `969d8c3`, API `2026-09-30.endive`, sha256 `9119e72c…` (matches the lock).
- 644 operations, 454 paths, 1701 component schemas; provider source unavailable.

**Consumers**

| Consumer | Commit | SDK | Own suite |
|---|---|---|---|
| checkout (TypeScript) | `1a691698659e` | stripe-node 23.0.0 | 4/4 |
| fraud (Python) | `91e1d97097bc` | stripe-python 16.0.0 | 5/5 |
| reporting (Python) | `ba15df9068a5` | stripe-python 16.0.0 | 3/3 |

**Scan**
- Fraud is a real scan: Noirit's scanner reproduces all 3 expected use sites.
- Checkout and reporting use the FIXTURE. The scanner missed 3 of 3 and 5 of 5 sites (see Noirit's
  section below).

**Change**
- A schema-only diff finds 0 changes; the declared change produces one `units_changed`
  (`structural_breaking=false`).
- Rules mode predicts 3 risks with 0 unresolved, and `validate_risk_evidence` finds no problems.

**Execute** (LOCAL RUNNER DOUBLE)
- Checkout: `verified_regression`, "expected $10.00, actual $0.10". The `tax_note_unchanged` guard passes.
- Fraud: `verified_regression`, `above_threshold_flagged` expected True, actual False. The
  below-threshold and at-threshold guards pass.
- Reporting storage: `not_run`, a potential risk with its missing evidence listed.
- Reporting control: passes on both variants, 5 requests each with test-mode auth and
  `Stripe-Version: 2026-09-30.endive`.

**Migrations**
- Both are validated within the tested scope, with the pinned-contract caveat.
- Run against the baseline, patched code fails: checkout shows $1000.00, and fraud's two guards fail.
  This is recorded as deployment-assumption evidence.
- The consumers' own suites pass on the migrated copies (4/4, 5/5).

**Tests**

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q -p no:cacheprovider` (root) | all pass; see `INTEGRATION.md` for the current count |
| `cd app && ../.venv/bin/python -m pytest -q -p no:cacheprovider` | 28 passed |

## Real vs simulated vs temporary

**Real**
- The pinned Stripe spec and its measured inventory.
- The official SDKs and the HTTP requests they make.
- The consumers' code and their own tests.
- Kanishk's discovery, analysis, scenarios and interpretation.
- Noirit's scanner, for the fraud repository.

**Simulated**
- The Stripe change, which Stripe has not announced.
- The proposed responses (`demo/stripe-fixtures/proposed`).
- The fixture server, which stands in for Stripe.

**Temporary adapters, all labeled**
- **Dependency FIXTURE** (`demo/expected/dependencies.fixture.json`) for repositories where the
  scanner misses sites. Every evidence line is verified against the commit.
- **LOCAL RUNNER DOUBLE** (`verification/scenarios/dev_runner_double.py`): a plain subprocess, not
  a sandbox, and not Sai's runner.
- **Prepared migration patches** (`demo/migrations/`) in place of Sai's generator.
- **Local git repositories** in place of GitHub snapshots.

**Not done**
- Live Stripe test mode (`live-check`) has never been run.
- No GitHub repositories, App installations or PRs exist (see `demo/GITHUB_SETUP.md`).

## Next integration step per teammate

### Noirit (scanner)

1. **Parameter-injected client (checkout).** `getOrderSummary(id, stripe: Stripe =
   createStripeClient())` is missed: the parameter named `stripe` is dropped by the shadowing rule,
   and the client comes from a factory in another file. Support SDK-typed or factory-created clients.
2. **`StripeClient` style (reporting).** `StripeClient(...).v1.payment_intents.retrieve` (the
   current stripe-python client style) is not mapped.
3. **Silent misses.** Both misses produce no `unsupported_patterns` entry. Report unattributed SDK
   calls as coverage gaps.
4. **Check against the fixture.** When 1 and 2 are fixed, `demo.py scan` switches those
   repositories to real scans automatically: a repository switches when the scanner reproduces
   every fixture site. `tests/demo/test_dependency_fixture.py` documents every expected site.
5. **Evidence order.** The analyzer relies on your evidence order (origin first, use site last).
   Keep it.

### Sai (runner, migration, GitHub)

1. **Use your RunnerService in the Stripe demo.** Run the Stripe case scenarios through
   `RunnerService` instead of the local runner double. Your runner already stages catalog
   scenarios and classifies them with `verification.scenarios.interpret.classify_phase`. The case
   reports are multi-case (`relevant` + `guard` cases).
2. **Fraud migration rule.** `MigrationService` / `migration.patches` only accept the
   `x.amount / 100` rule. The fraud migration is a threshold rescale (`> 500000` → `> 5000`) and
   needs its own narrowly scoped rule. Until then it is the prepared patch in `demo/migrations/`.
3. **Runner change made today (please review).** Your runner now also writes the response to a
   file and sets `SHOCKWAVE_RESPONSE_FIXTURE`, which is what Noirit's consumers read. Before this,
   a proposed run silently used the consumer's old fixture and reported
   `compatible_within_tested_scope`. Covered by `tests/canonical/test_consumer_fixture_injection.py`.
4. **Scenario binding collision.** Your simulated scenario and Kanishk's Stripe checkout scenario
   were both bound to `src/checkout.ts`, and catalog bindings can't distinguish repositories. The
   template file was renamed to `src/orderSummary.ts`. A durable fix needs repository identity in
   bindings.
5. **GitHub App.** Create and install it per `demo/GITHUB_SETUP.md`: a read-only App, plus a separate
   PR-writer App. Publication stays disabled until then.

### Ishan (platform)

1. **Dashboard data.** `demo/expected/results/` is dashboard-ready JSON: the contract records
   (APIChange, AnalysisResult, VerificationPlan, VerificationResult), private per-consumer findings
   and `provider/restricted-summary.json` (counts only). Shapes are documented in
   `demo/reports/README.md`.
2. **Integrated mode.** `SHOCKWAVE_ADAPTERS=integrated` (with `SHOCKWAVE_INLINE_JOBS=1`) runs
   your platform flow on the real modules (`backend/integrated.py`), with Demo Payments as the
   project. The UI labels still read "fixture" in places; `capabilities.mode` reports
   `integrated`, and `real_modules` lists the steps.
3. **`ScanRequest` fields.** Decide whether `ScanRequest` gains `consumer_org_id` / `base_urls`
   (Noirit's proposal). For now `consumer_org_id` is passed as a keyword.
4. **Remaining fallbacks.** The fallback contract doubles (`discovery/_contract_doubles.py`,
   `scanner/contracts.py`) are inactive whenever `app/` is on the path. Remove them once every
   entry point guarantees that.
5. **Dependency manifest.** A root `requirements.txt` now lists everything the repository needs.
   Fold it into your manifest if you prefer.
