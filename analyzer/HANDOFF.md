# Kanishk handoff — discovery, analyzer, verification scenarios

Checkpoint: 2026-10-17. **No git commit exists**: `shockwave/` is not a git repository yet,
so there is no commit hash to cite. Everything below was verified in this working tree.

Owned paths: `discovery/**`, `analyzer/**`, `verification/scenarios/**`,
`examples/provider/**`, `tests/discovery/**`, `tests/analyzer/**`. Nothing outside them was
edited. `shared/` (Ishan) does not exist yet; local contract doubles stand in (see Contracts).

## Implemented

- **Stage A — discovery** (`discovery/`)
  - Bounded OpenAPI 3.0/3.1 parser with local `$ref` expansion:
    - per-schema and document-wide node budgets, a 160-level output depth cap and
      `max_operations`;
    - YAML size and node caps, plus refusal of duplicate keys and NaN;
    - kept references are marked `x-shockwave-ref` and carry a target digest
      (`x-shockwave-ref-digest`).
  - Pinned public Stripe contract: commit `969d8c308ea6`, sha256 `9119e72c…`, giving
    644 operations over 454 paths. All Stripe endpoints are `not_compared`, because no Stripe
    server source exists.
  - Fictional **Demo Payments** FastAPI service with v1 (cents) and v2 (dollars) specs. Only
    `GET /v1/charges/{id}` `amount` changes units; refunds stay in cents.
  - Static FastAPI and Express route extraction: source is parsed, never imported or run.
  - Reconciliation statuses: `documented_and_found`, `implementation_only`,
    `documented_not_found_in_scan`, `conflict` and `not_compared`.
  - Coverage-gap vocabulary in `coverage.unsupported_patterns`.
- **Stage B — analysis** (`analyzer/`)
  - `detect_changes`: schema diff merged with declared changes.
  - `analyze_impact`: deterministic rules, plus an opt-in model adapter. Every result is
    labeled with `analysis_mode` = `rules`, `model` or `fixture`.
- **Stage C — verification scenarios** (`verification/scenarios/`)
  - `build_verification_plan`, a staging protocol, Python and Node harnesses, and the
    outcome interpreters.
  - The `$10.00` invariant: the display scenario expects `$10.00` against both contract
    fixtures.

## Entry points

| Purpose | Call |
|---|---|
| Discovery | `discovery.discover_endpoints(DiscoveryInput)` / `discovery.service.discover(...)` |
| Change detection | `analyzer.detect_changes(old, new, declared_changes, *, api_project_id=None)` |
| Impact | `analyzer.analyze_impact(change, dependencies, *, endpoints=None)` |
| Risk category | `analyzer.impact.risk_category(change, risk)` (Risk has no category field) |
| Evidence check | `analyzer.impact.validate_risk_evidence(..., endpoints=...)` |
| Plans | `verification.scenarios.build_verification_plan(risk, snapshot)`, `not_run_result(...)` |
| Staging / interpretation | `stage_scenario`, `interpret_verification`, `interpret_patched_run`, `staging.runner_argv_hint(plan)` |
| Demo CLI | `python -m analyzer.demo [--execute-local] [--write-samples DIR] [--node PATH]` |

New exceptions:
- `analyzer.changes.ProposedContractIncomplete(ValueError)` → **409**.
- `InvalidDeclaredChange` → **422** (`.problems`).
- Other `ValueError`s from `detect_changes` → 422.
- `verification.scenarios.NoExecutableScenario` and `AmbiguousScenario` → the Risk stays `not_run`.

## Fixture IDs

These are placeholders from `discovery.ids.fixture_uuid`. Keep the keys and replace the
values once Ishan publishes canonical IDs.

- `examples/provider/demo_payments/fixture_ids.json`
  - `service_id` `demo-payments`, `api_project_id` `3f35ea07-…`
  - v1 snapshot `fbda55e7-6c86-5ee6-9ed6-fe9ac0879e15`
  - v2 (proposed) snapshot `9f9eed12-9741-5e6c-890d-ab70bc01abaf`
  - Stripe snapshot `96b4a0ec-…`
- `tests/analyzer/fixtures/consumer_fixture_ids.json`: the consumer org, the enrollment, and
  the `python_storefront` / `ts_storefront` connections and snapshots.
- Scenarios:
  - `demo-payments.charge-amount-units.display.python` (`trusted_python`)
  - `demo-payments.charge-amount-units.display.typescript` (`trusted_node`)
- Response fixtures:
  - `demo-payments.charge.ch_demo.v1-cents` (`amount: 1000`)
  - `demo-payments.charge.ch_demo.v2-dollars` (`amount: 10`)
- Endpoint IDs are UUIDv5 values derived per snapshot. Cross-snapshot identity is
  `(service_id, METHOD, path)` (`discovery.ids.endpoint_identities` / `same_endpoint`).

## Analysis modes

- `rules`: the default, used by the demo and CLI. Deterministic and local: no network, no
  model, nothing executed.
- `model`: opt-in. Enable it with:

  ```python
  from analyzer.impact import ImpactAnalyzer, configure_default_analyzer
  from analyzer.model_adapter import AnthropicModelClient, ModelImpactAnalyzer

  configure_default_analyzer(
      ImpactAnalyzer(
          "model",
          model_analyzer=ModelImpactAnalyzer(
              AnthropicModelClient(),
              model_processing_allowed=<RepositoryConnection.allow_model_processing>,
          ),
          fallback_to_rules=True,
      )
  )
  ```

  - Model: `claude-opus-5-5`, overridable with `SHOCKWAVE_ANALYZER_MODEL`. The SDK reads
    credentials from its own environment; this code never reads or logs them.
  - Consent is default-deny. The evidence slice is bounded, and a bad reply gets one repair
    retry.
  - Every reply is validated: only known dependency IDs, no claims that anything was
    executed, verified or confirmed, and file:line citations must come from the evidence.
  - On failure it falls back to rules with the warning
    `model analysis failed (<ErrorType>); deterministic rules used`.
- `fixture`: replays `analyzer/fixtures/model_replay_display.json`. That reply was
  **hand-authored, not recorded**, and is labeled "Replayed fixture reply (not live inference)".

## Test evidence

Commands were run from `shockwave/` with Python 3.13.1, pytest 9.1.1, Node 24.19.0 and
ruff 0.17.1.

| Command | Result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q` | **774 passed**, 0 failed (21.7 s) |
| `.venv/bin/ruff check --no-cache . --extend-exclude "tests/*/fixtures"` | All checks passed |
| `pytest tests/analyzer/test_contract_strictness.py tests/discovery/test_discovery_gaps_e2e.py` | 12 passed |
| `python -m analyzer.demo --execute-local --write-samples <tmp>` then `diff -r <tmp> examples/provider/demo_payments/samples` | exit 0, byte-identical |
| `python -m analyzer.demo` / `--execute-local` | exit 0 (output below) |
| Review probe `p3_ref_amplification.py 1200 2900` (241 KB spec) | 357 MB peak RSS and a document-budget warning (before the fix: 1.06 GB, no warning) |

`test_contract_strictness.py` runs the whole pipeline in subprocesses against
`tests/analyzer/fixtures/strict_contracts/shared/schemas.py`, a literal reading of
CONTRACTS.md. That reading has UUID-typed IDs, a required `schema_version`, required-nullable
optional fields and differently named nested models. Older unit tests assert `str` IDs and
use lenient builders, so they are meant to run on the doubles only.

Sample artifacts are in `examples/provider/demo_payments/samples/`:
- `demo_payments_analysis.json` is an `AnalysisResult` (`analysis_mode: "rules"`).
- `demo_payments_verification_results.local_double.json` holds local-double results, each
  labeled `LOCAL RUNNER DOUBLE`.

Excerpt of `python -m analyzer.demo --execute-local`:

```
PREDICTED  risk 1dc12d45  conversion high/direct  python_storefront storefront/pricing.py:18
  baseline  exit 0: passed - displayed_price_usd (expected $10.00, actual $10.00)
  proposed  exit 1: relevant_fail - expected $10.00, actual $0.10
  result: verified_regression - LOCAL RUNNER DOUBLE (...not a sandbox; not Sai's runner...)
  patched (remove the '/ 100' conversion; simulated edit): validated - Validated within the
    tested scope only ... pinned to the proposed contract ... coordinate with the cutover.
PREDICTED  risk 96e3219f  comparison high/inferred  -> NOT_RUN (no executable scenario)
PREDICTED  risk 9c72babf  storage medium/inferred   -> NOT_RUN (no executable scenario)
UNRESOLVED (unknown, not safe)  storefront/export.py:8 (pass-through)
```

The TypeScript storefront (`risk 7ee48ef7`) gives the same result.

## Outcome table (`verification/scenarios/outcomes.py`)

- Baseline passes and proposed hits the relevant assertion failure: `verified_regression`.
- Both pass: `compatible_within_tested_scope`.
- Baseline fails, timeout, infrastructure error, no tests run, or an unrelated failure:
  `inconclusive`.
- No executable scenario (comparison, storage, unbound or ambiguous): `not_run`.
- A patched run that passes against the proposed fixture is validated within the tested
  scope only, and carries `interpret.PINNED_PATCH_CAVEAT`.

## Interface changes (additive unless noted)

1. **`detect_changes(..., *, api_project_id=None)`.** If a structural change has no declared
   change and no project ID is given, it raises `ValueError("api_project_id required…")`.
   Proposed CONTRACTS.md amendment.
2. **`ProposedContractIncomplete`.**
   - It is raised instead of claiming an endpoint removal when the proposed snapshot's
     coverage contains `openapi:unparseable <ref>`, `openapi:unread-document <ref>`,
     `openapi:no-documents` or `openapi:unresolved-path-item <path>`.
   - **Persist `coverage.unsupported_patterns` verbatim**; if it is dropped, removals are
     claimed again.
   - A spec whose `paths` is a `$ref` or not a mapping is reported as `openapi:unparseable`.
3. **`analyze_impact(..., *, endpoints=None)`.** The same keyword exists on
   `validate_risk_evidence`, `match_dependencies`, `ImpactAnalyzer.analyze` and
   `ModelImpactAnalyzer.analyze`. Without it, a dependency whose `endpoint_id` belongs to
   another snapshot is dropped, with a counted warning.
4. **Endpoint removal.** It is a `behavior_changed` on field `*` with
   `structural_breaking=True`. A `*` change that is not breaking is an endpoint-wide behavior
   change (medium/inferred).
5. **Conversion Risk IDs.** They are keyed per bound scenario,
   `risk_id(change, enrollment, snapshot, "conversion:<scenario_id>")`, so a repo with Python
   and TypeScript display code yields two plannable Risks. Use `risk_category`.
6. **Scenario pointers.** They are `kind="test"` evidence with
   `source_uri = shockwave-scenario://<id>`, minted **only** by `analyze_impact` and bound to
   a dependency ID. Declared evidence that uses the scheme is rejected (422); scanner evidence
   that uses it is dropped with a warning.
7. **Contracts.** `schema_version` is required on every top-level result, optional values
   are passed as explicit nulls, and IDs may be `str` or `uuid.UUID` (always converted with
   `str()`).
8. **Source-only endpoints.** Their request and response schemas carry `x-shockwave-schema`,
   which means unknown, not "no fields".
9. **URIs.** Evidence URIs, warnings and coverage go through `discovery.reconcile.display_uri`,
   which drops userinfo, query and fragment.
10. **Truncation mark.** It is `...` (`discovery.text.TRUNCATION_MARK`).
11. **Python runner argv.** It is
    `python -m pytest -q -p no:cacheprovider --noconftest -c .shockwave/scenarios/<id>/pytest.ini --rootdir .shockwave/scenarios/<id> <test_file>`
    with cwd set to the checkout, and env `StagedRun.env`. That env holds the `SHOCKWAVE_*`
    keys plus `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and `PYTHONSAFEPATH=1`. The Node argv is
    `node --test <test_file>`.
12. **Declared-change evidence.** It is copied verbatim into every enrolled consumer's
    `Risk.evidence`, including any provider_owned docs or source excerpts. That is a policy
    decision for Ishan.

## Runtime settings (no secrets)

- `SHOCKWAVE_CONTRACTS=doubles` forces the local doubles. Otherwise `shared.schemas` is used
  automatically once it is importable.
- `SHOCKWAVE_ANALYZER_MODEL` sets the model (model mode only).
- `DEMO_PAYMENTS_CONTRACT=v1|v2` selects the contract the fictional service serves.
- Dependencies:
  - Python ≥ 3.11 (3.13 tested), pydantic 2, pyyaml, fastapi, httpx, requests and pytest;
  - `anthropic`, only for model mode;
  - ruff, for development;
  - Node ≥ 22.18 for the TS scenario. Here it comes from the `nodejs-wheel` package at
    `.venv/bin/node`, because the system Node is x86-only.

## Live vs demo status

- **Live (real inputs):**
  - Static OpenAPI and source discovery runs on any checkout.
  - The pinned Stripe spec is the authentic public contract.
- **Local double only:**
  - Verification has executed only on `dev_runner_double`: a plain subprocess, trusted
    fixtures only, **not a sandbox and not Sai's runner**.
  - Consumer dependencies are contract doubles of Noirit's scanner output
    (`tests/analyzer/fixtures/demo_dependencies.json`).
- **Never run:**
  - No live model call: the model path was exercised with scripted clients, a fake SDK and the
    hand-authored replay.
  - `examples/provider/stripe/refresh_pin.py` has never been run against the network.
  - Nothing has run on Sai's runner or in a sandbox.

## Known limitations and unresolved coverage

- **Comparison and storage risks** have no executable scenario and stay predictions
  (`not_run`).
- **Unresolved dependencies:** pass-through and unknown usages are reported as unresolved
  (unknown, not safe).
- **Harness network guards** are best-effort in-process interception. The runner must isolate
  the network for **every** profile, trusted ones included.
- **Patched validation** assumes a non-adversarial patch. Configuration-only forgery is
  blocked, but code imported in-process could forge a passing report.
- **Stripe `$ref` digests:** the component graph is dense, so one deep edit marks thousands of
  references as "referenced schema changed, not structurally compared" (non-breaking).
- **OpenAPI 3.1 sibling chains:** `allOf` nesting deeper than one level is not compared field
  by field.
- **Unresolvable path items** make the whole path unknown, and the remote `$ref` is never
  fetched.
- **Express scanner gaps:**
  - Registrations on computed members or on call results are not reported.
  - Cross-file routers are reported but never resolved.
- **Endpoint catalog in the demo:** `analysis_endpoints` passes only the v1 catalog. A real
  scan keyed to another snapshot must pass that snapshot's endpoint records too.

## Blocked prerequisites

- **Ishan:** `shared/schemas.py` and canonical fixture IDs.
- **Sai:** the runner.
- **Noirit:** real consumer scans and the snapshot their `endpoint_catalog` comes from.

## Next owner actions

- **Ishan (backend / contracts)**
  1. Land `shared.schemas`.
  2. Run `pytest tests/analyzer/test_contract_strictness.py` with it on the path, and delete
     the strict shim once it passes.
  3. Call `detect_changes(old, new, declared, api_project_id=<route project id>)`. Map
     `ProposedContractIncomplete` → 409 (catch it before `ValueError`), and
     `InvalidDeclaredChange` / other `ValueError` → 422.
  4. Persist `DiscoveryResult.coverage` verbatim.
  5. Call `analyze_impact(change, deps, endpoints=[*old_snapshot.endpoints, *scan_catalog])`.
  6. Use `risk_category` for display.
  7. Decide the policy for provider evidence in consumer Risks.
  8. Never show `*.local_double.json` results as runner evidence.
- **Sai (runner / migration)**
  1. Build the argv from `staging.runner_argv_hint(plan)`, replacing argv[0] with your
     interpreter, and merge `StagedRun.env`.
  2. Isolate the network for all profiles.
  3. Allowlist `MigrationResult.changed_files` to the Risk's dependency-evidence files, and
     refuse patches that touch config files (`conftest.py`, `pytest.ini`, `pyproject.toml`,
     `package.json`, `node_modules/`, `.shockwave/**`, …).
  4. Copy `PINNED_PATCH_CAVEAT` and `Risk.suggested_action` into the warnings and the PR text.
- **Noirit (consumer scanner)**
  1. Set `Dependency.endpoint_id` from the `ScanRequest.endpoint_catalog` actually used, and
     report which snapshot that catalog came from.
  2. Never emit `shockwave-scenario://` evidence.
  3. Keep the binding paths the scenarios expect (`storefront/pricing.py`,
     `src/pricing.ts`).
- **Resuming agent:** run the commands in Test evidence. Expect 774 passed, clean lint and
  byte-identical samples.
