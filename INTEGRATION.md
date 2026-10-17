# Integration — Shockwave MVP on `main`

Status: **local only**. `main` exists only in this clone (worktree `~/Desktop/Shockwave-main`) and
has **not been pushed**. No PR, force-push, rebase or branch deletion was done. Nothing was published.

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

## Conflicts and overlaps

No merge produced a textual conflict.

- **Sai edited three Kanishk-owned files** (`verification/scenarios/catalog.py`, `plans.py`,
  `staging.py`). The edits are additive: an optional `additional_test_files` field and Sai's simulated
  Stripe TypeScript scenario. The diff was shown to Kanishk, who chose **"keep Sai's additions"**.
- **`app/shared/` (Ishan wins).** Sai's copies of `schemas.py`, `contracts.schema.json` and
  `demo-identities.json` are byte-identical to Ishan's. Sai's extra `app/shared/PROVENANCE.md` is
  kept, since Ishan has no version of it.
- No teammate code was deleted.

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
| send_invitation | development inbox (`DemoAdapters`) | **fake, labeled** |
| publish_migration | disabled (`DemoAdapters` raises; no PR) | **not attempted** |

The default `create_app()` (the server and its `start-backend.sh`) still uses Ishan's `DemoAdapters`.
There is no environment switch for integrated mode yet.

## Test results

Run from the repository root with Python 3.13.1 and Node 24.19.0 (`nodejs-wheel`; the system Node
is x86 and does not run):

| Command | Result |
|---|---|
| `PATH=.venv/bin:$PATH .venv/bin/python -m pytest -q -p no:cacheprovider` (root `pytest.ini`: tests/, pythonpath `. app`) | **869 passed** |
| `cd app && PATH=../.venv/bin:$PATH ../.venv/bin/python -m pytest -q -p no:cacheprovider` | **27 passed, 1 failed** (see "Still broken") |
| `app/tests/e2e/test_integrated.py` (part of the line above) | **passed** |
| `npm ci && npm run typecheck && npm run build` in `app/frontend` | **passed** |

Dependencies were installed into `.venv` (uv): `app/requirements.lock`, `requirements-integration.txt`,
the scanner's tree-sitter pins, and `pyyaml`, `anthropic`, `nodejs-wheel` and `ruff`. Sai's checkout
demo also needs `npm ci --ignore-scripts --prefix demo/stripe-checkout`. There is no single root
dependency manifest yet.

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
  - Email goes to the development inbox.
  - Personas use the demo session.
- **Not attempted:** PR publication, live model calls and GitHub App installation. Execution is never
  sandboxed: only registered team fixtures run.

## Still broken or open

1. **Ishan's `test_vertical_slice_and_private_artifacts` fails on Node 24.**
   - `backend/demo.execute` counts JS tests only from TAP output (`# tests 2`).
   - Node 24's default reporter prints `ℹ tests 2`, so the run is counted as 0 tests and marked inconclusive.
   - It passes on Node 20 (Ishan's target), which can't run on this Mac.
   - Fix (Ishan): pass `--test-reporter=tap`, or parse both formats.
2. **Fixture injection mismatch between Sai's runner and Noirit's consumers.** For non-catalog
   scenarios, Sai's runner passes the response as `SW_RESPONSE` (JSON). Noirit's consumer tests read
   `SHOCKWAVE_RESPONSE_FIXTURE` (a file path). The integrated path avoids this by using catalog
   scenarios (Kanishk's harness), but the two need one convention.
3. **No live GitHub fetch or publication.** It needs Sai's GitHub App configuration (credentials,
   installation) and Ishan's integration. Publication stays disabled.
4. **No integrated-mode switch.** The running server (`start-backend.sh`) is still in fixture mode.
   Add a setting (Ishan) to select `integrated_factory()`.
5. **Proposed `ScanRequest` fields are not in `shared.schemas`.** Noirit proposed `consumer_org_id`
   and `base_urls`. They are currently passed as a keyword or taken from scanner defaults. Ishan
   decides whether to add them.
6. **Kanishk's Stripe demo environment is not on any branch.** It is uncommitted work in the
   `Kanishk` working tree. It rewrites the same `verification/scenarios` files Sai touched, and
   overlaps with Sai's `demo/stripe-checkout`. It needs reconciling before it lands on `main`.
7. **No single dependency manifest.** Dependencies are split across `app/requirements.lock`,
   `requirements-integration.txt` and `scanner/requirements.txt`, plus undeclared `pyyaml`,
   `anthropic` and `nodejs-wheel` for Kanishk's modules.
8. **Fallback contract copies.** Kanishk's and Noirit's fallback models still exist (inactive; see Contracts).
