# Integration — Shockwave MVP on `main`

Status: pushed to `origin/main` after review (normal push; no force-push, rebase or branch deletion).
No PR was opened and nothing was published to consumer repositories.

How to run:
- **Stripe demo:** see `demo/README.md` and the three-minute `demo/DEMO_SCRIPT.md`.
- **Platform flow on the real modules:** see "Orchestrator wiring" below.
- **Root `README.md`** has both in one page.

## Merge order

There was no `main` anywhere. It was created locally at `1d3ee7c`, the common root of `Ishan`,
`Kanishk` and `Sai` (Kanishk's first commit), so those three merge with ordinary history.

| # | Branch (tip merged) | Merge commit | Notes |
|---|---|---|---|
| 1 | `origin/Ishan` @ `9c62bb2` | `bbd5932` | `--no-ff`; no conflicts (46 files under `app/`) |
| 2 | `origin/noirit` @ `bba076d` | `7dfc062` | `--no-ff --allow-unrelated-histories` (separate root); no conflicts |
| 3 | `origin/Kanishk` @ `ff725bd` | `a40cda7` | `--no-ff`; no conflicts |
| 4 | `origin/Sai` @ `3631fe2` | `5cab76d` | `--no-ff`; no textual conflicts (see overlap below) |

Fix commits (in order): `8250983` (after noirit), `7532804` (after Kanishk), `3f6f244` (after Sai),
then wiring `bd045e0` and `c1ed84e`.

| # | Second round | Notes |
|---|---|---|
| 5 | `Kanishk` @ `d0d86d3` (Stripe demo environment) | `--no-ff`. Conflicts in `.gitignore`, `verification/scenarios/catalog.py` and `staging.py`, all resolved by keeping both sides (see below). |

## Conflicts and overlaps

No merge produced a textual conflict.

- **Sai edited three Kanishk-owned files** (`verification/scenarios/catalog.py`, `plans.py`,
  `staging.py`). The edits are additive: an optional `additional_test_files` field and Sai's simulated
  Stripe TypeScript scenario. The diff was shown to Kanishk, who chose **"keep Sai's additions"**.
- **`app/shared/` (Ishan wins).** Sai's copies of `schemas.py`, `contracts.schema.json` and
  `demo-identities.json` are byte-identical to Ishan's. Sai's extra `app/shared/PROVENANCE.md` is
  kept, since Ishan has no version of it.
- No teammate code was deleted.

### Second-round merge (Stripe demo)

**Conflict resolution:** both sides kept.
- `catalog.py` keeps Sai's `additional_test_files` and Sai's simulated scenario, Noirit's consumer
  bindings, and Kanishk's three Stripe case scenarios.
- `staging.py` keeps the control-scenario refusal and checks Sai's extra test files.
- `.gitignore` is the union of both sides.

**Fixes the merge needed:**

| Area | Fix |
|---|---|
| Scenario binding collision | Kanishk's Stripe checkout scenario and Sai's simulated scenario were both bound to `src/checkout.ts`, so Sai's plans resolved to Kanishk's scenario (`KeyError: 'response'` in Sai's runner). Kanishk's template file is now `src/orderSummary.ts`; the fixture, ids, migration, reference results and tests were updated. Sai's code is unchanged. |
| Evidence order | Kanishk's dependency fixture listed the use site first; the scanner (and the rules) use origin → … → use site last. The fixture was reordered, ids recomputed per the documented derivation, and tests updated. |
| Real scanner in the demo | `demo.py scan` called a guessed scanner interface. It now calls Noirit's real `scan_repository` per repository and uses its records when they reproduce every expected use site; otherwise it keeps the line-verified fixture and records the misses. Fraud is a real scan; checkout and reporting expose two scanner gaps (see `demo/HANDOFF.md`). |
| `fixture-server --max-requests` race | The threading server returned before counting the request and then blocked forever. Requests are now answered synchronously in that mode (the CLI test was flaky; three consecutive passes after the fix). |
| Inferred-pointer policy | Kanishk's Stripe test still expected `inferred` to get no scenario; it now uses `unresolved`, consistent with the policy below. |
| Contracts in the demo | `demo/scripts/demo.py` puts `app/` on the path, so the demo uses `shared.schemas` too. |

**"Still broken" items fixed in this round:**

| Item | Fix |
|---|---|
| Ishan's Node 24 test count | `backend/demo.execute` also accepts the spec reporter's `ℹ tests 2`. The app suite is 28/28. |
| `SW_RESPONSE` vs `SHOCKWAVE_RESPONSE_FIXTURE` | Sai's runner also writes the response to a file and sets `SHOCKWAVE_RESPONSE_FIXTURE`. New test `tests/canonical/test_consumer_fixture_injection.py`: without the fix the result was a false `compatible_within_tested_scope`; with it, `verified_regression`. |
| No integrated-mode switch | `SHOCKWAVE_ADAPTERS=integrated` (plus `SHOCKWAVE_WORK_DIR`) selects `backend.integrated`. `/health` and `/capabilities` report `integrated` and list `real_modules`. |
| No single dependency manifest | A root `requirements.txt` holds the tested versions. |
| Stripe demo not on a branch | Committed on `Kanishk` and merged here. |

## Fixes made

All fixes are limited to imports, schema mismatches and the requested wiring.

| Commit | Owner area | Fix |
|---|---|---|
| `8250983` | Noirit `scanner/`, `tests/scanner/` | **Schema mismatch.** The canonical `ScanRequest` has no `consumer_org_id` and forbids extra fields, so every scan raised. `scan_repository(..., *, consumer_org_id=None)` now takes it as a keyword. The test fixtures and the sample generator now use UUID ids and complete `Endpoint` records. Samples were regenerated; only the ids changed. |
| `7532804` | Kanishk `tests/analyzer` | **Schema mismatch.** `ExecutionResult.artifact_ids` must be UUIDs. |
| `3f6f244` | Noirit `tests/scanner` | **Import.** A function-local `from conftest import make_request` resolved to another package's `conftest` in a root-level run. |
| `bd045e0` | Kanishk `verification/scenarios`, `analyzer/rules.py` | **Wiring.** Catalog scenarios are now bound to Noirit's consumers (`examples/consumers/python-display` `demo_display/charges.py`, `typescript-display` `src/charges.ts`). See the design change below. Rules now cite the use site (the last source evidence, matching the scanner's origin → … → use-site chain) instead of the origin call. |
| `c1ed84e` | Ishan `app/backend`, `app/pyproject.toml` | **Wiring.** Adds `backend.integrated.IntegratedAdapters` plus three guarded orchestrator hooks. `DemoAdapters` behavior is unchanged. `app/pyproject.toml` adds `..` to `pythonpath`. |

> **Design change to review (Kanishk's module).** An **inferred** dependency at a scenario's bound
> consumer file now gets an executable-scenario pointer. Previously only **direct** dependencies got
> one.
> - **Why:** Noirit's scanner labels call-following dependencies `inferred`, so the real scanner
>   never produced a plannable risk. Running the scenario is what settles an inferred link.
> - **What stays the same:** the Risk stays `inferred` until a result arrives, and `unresolved`
>   dependencies still never get a pointer.
> - **Tests:** `test_inferred_display_dependency_gets_no_scenario_pointer` was replaced by
>   tests for the new rule.

**Contracts.** Every module imports the canonical models from `app/shared/schemas.py` (`shared.schemas`).
The root `pytest.ini` puts `app/` on the path, and `app/pyproject.toml` now also exposes the root.
- Two fallback copies still exist: `discovery/_contract_doubles.py` (Kanishk) and the fallback in
  `scanner/contracts.py` (Noirit).
- They are inactive whenever `shared.schemas` is importable (verified: `CONTRACTS_SOURCE ==
  'shared.schemas'`, and `scanner.contracts.Dependency is shared.schemas.Dependency`).
- They were not deleted, since that is teammate code. Remove them once every entry point always has
  `app/` on the path.

## Orchestrator wiring

`create_app(adapters_factory=integrated_factory(work_dir))` runs the platform flow on the real modules:

| Step | Module | Status |
|---|---|---|
| discover_endpoints | Kanishk `discovery` (hand-maintained Demo Payments spec + literal FastAPI scan) | **live** |
| fetch_repository | local trusted team fixture copy (`examples/consumers/python-display`) | **fake, labeled** (Sai's GitHub transport needs App credentials) |
| scan_repository | Noirit `scanner.scan_repository` | **live** |
| detect_changes | Kanishk `analyzer.detect_changes` (provider's declared change, re-keyed to the project) | **live** |
| analyze_impact | Kanishk `analyzer.analyze_impact` (rules mode, deterministic) | **live** |
| build_verification_plan | Kanishk `verification.scenarios.build_verification_plan` | **live** |
| verify_regression | Sai `verification.runner.RunnerService` (stages Kanishk's harness) | **live** (trusted fixtures only; not a sandbox) |
| generate_migration | Sai `migration.MigrationService` (rule-based patch) | **live** |
| send_invitation | SMTP notification when `SHOCKWAVE_SMTP_*` is set, otherwise development inbox (`DemoAdapters`) | **live with SMTP**, otherwise fake, labeled |
| publish_migration | disabled (`DemoAdapters` raises; no PR) | **not attempted** |

Start the server in integrated mode with `SHOCKWAVE_ADAPTERS=integrated SHOCKWAVE_INLINE_JOBS=1
./app/scripts/start-backend.sh`. Inline jobs keep the integrated state in one process. Without the
setting, the server uses Ishan's `DemoAdapters` as before.

## Test results

Run from the repository root with Python 3.13.1 and Node 24.19.0 (`nodejs-wheel`; the system Node
is x86 and does not run):

| Command | Result |
|---|---|
| `PATH=.venv/bin:$PATH .venv/bin/python -m pytest -q -p no:cacheprovider` (root; `pytest.ini`: tests/, pythonpath `. app`) | **1108 passed** |
| `cd app && PATH=../.venv/bin:$PATH ../.venv/bin/python -m pytest -q -p no:cacheprovider` | **29 passed** (incl. `test_integrated.py`) |
| `.venv/bin/python demo/scripts/demo.py reset && .venv/bin/python demo/scripts/demo.py all` | exit 0; `results/ is byte-identical to demo/expected/results (35 files)` |
| `cd app && PYTHONPATH=.:.. python scripts/integrated_demo.py` | verified_regression, then patch_validated (output below) |
| `npm ci && npm run typecheck && npm run build` in `app/frontend` | passed |
| `SHOCKWAVE_ADAPTERS=integrated SHOCKWAVE_DEMO=1 SHOCKWAVE_INLINE_JOBS=1 uvicorn backend.main:app` | `/api/v1/health` reports `integration_mode: integrated` |

Dependencies come from the root `requirements.txt` (uv). The checkout template and Sai's checkout
demo also need `npm ci`; `demo.py prepare` does it for the template, and
`npm ci --ignore-scripts --prefix demo/stripe-checkout` does it for Sai's demo.

## Scenario output

Command: `cd app && PYTHONPATH=.:.. python scripts/integrated_demo.py`. Every stage goes through the
platform API and the real modules.

```
Shockwave integrated demo — demo-payments (fictional), cents -> dollars
repository: LOCAL TRUSTED TEAM FIXTURE (examples/consumers); not a GitHub snapshot: examples/consumers/python-display
[scan] dependency response.amount divide 100.0 (inferred) at demo_display/charges.py:11: dollars = response["amount"] / 100
[detect_changes] units_changed response.amount structural_breaking=False
[analyze_impact] risk severity=high confidence=inferred: demo_display/charges.py:11 divides response.amount by 100 to convert cents to dollars. Under the proposed contract amount is already in dollars (10 for $10.00),
[verify_regression] verified_regression: baseline exit 0 (1 test), proposed exit 1 (1 test)
    "message": "expected $10.00, actual $10.00",
    E        : expected $10.00, actual $0.10
[generate_migration] patch_validated (rule); changed ['demo_display/charges.py']; validation exit 0, tests_run 1
    -    dollars = response["amount"] / 100
    +    dollars = response["amount"]
[publication] not attempted (pr_url=None); warnings: ['Targets the proposed contract only; deployment must be coordinated. Old responses are not supported by this migration.']
[provider summary] {"coverage": "partial", "predicted_risk_count": 1, "verified_regression_count": 1, "validated_fix_count": 1, "deployment": "unknown"}
```

Every expectation holds:
- a divide-by-100 dependency;
- one `units_changed` change with `structural_breaking=false`;
- a high-severity risk;
- baseline PASS at $10.00 and proposed FAIL at $0.10;
- the patch removes only `/ 100` and the unchanged $10.00 test passes.

The $10.00 expectation was not changed. The TypeScript consumer was also run through the same
adapter chain directly (outside the API): verified_regression, then patch_validated (`src/charges.ts`).

## Live vs fake

- **Real:** discovery, scanning, change detection, impact analysis, planning, native test execution
  (pytest / `node --test`) and rule-based patch generation and validation, all run on team-owned fixtures.
- **Fake, all labeled:**
  - The Demo Payments provider is fictional.
  - Repository fetch is a local fixture copy, not GitHub.
  - Without SMTP settings, email goes to the development inbox.
  - The one-click demo personas use the demo session. Email/password accounts are real, but sign-up does not yet verify email ownership.
- **Not attempted:** PR publication, live model calls and GitHub App installation. Execution is never
  sandboxed: only registered team fixtures run.

## Still broken or open

1. **No live GitHub fetch or publication.** It needs the GitHub App to be created and installed
   (`demo/GITHUB_SETUP.md`) and Sai's transport configured. Publication stays disabled.
2. **Scanner gaps (Noirit):**
   - SDK clients injected through a parameter named `stripe` from a factory are not attributed.
   - `StripeClient(...).v1.payment_intents.*` is not mapped.
   - Both misses are silent: no coverage gap is reported.
   The demo uses the labeled fixture for those two repositories.
3. **Stripe demo verification runs on the local runner double, not Sai's `RunnerService`.** The
   fraud threshold rescale also has no rule in Sai's migration generator yet.
4. **`ScanRequest` fields.** `ScanRequest` lacks `consumer_org_id` / `base_urls`; Ishan decides.
   `consumer_org_id` is passed as a keyword for now.
5. **Fallback contract copies.** Kanishk's and Noirit's fallback models still exist (inactive when
   `app/` is on the path).
6. **Integrated-mode UI labels.** Some UI texts still read "fixture" in integrated mode.
   `capabilities.mode` and `real_modules` are correct.
7. **Teammate lint.** Teammates' modules have pre-existing ruff findings under default rules
   (style only; there is no shared ruff config). Kanishk's areas and all new files are clean.
