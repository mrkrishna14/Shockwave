# Sai Stripe integration handoff

This supersedes the Demo Payments-only statements in the earlier checkpoint.
The original Demo Payments fixtures and tests remain supported.

## Implemented

- A narrowly scoped hypothetical Stripe PaymentIntent amount-unit migration for
  the TypeScript checkout fixture. Source hash, line, expression, selected scenario,
  target contract, snapshot, and consent checks still apply.
- A real official Stripe SDK call, using its fetch HTTP client so the existing
  scenario harness can intercept responses. No real Stripe credential is needed.
- Catalog scenario staging in `RunnerService`: native JUnit results are combined
  with bounded JSON scenario evidence. Missing, tampered, unrelated, or erroneous
  evidence remains inconclusive. Harness staging occurs only in temporary copies.
- Catalog-approved additional native tests. The Stripe checkout's local-cents test
  runs in baseline, proposed, and patched phases; migrations cannot edit it.
- Canonical schemas imported unchanged from Ishan under `app/shared`. Production
  constructors are exercised directly, including GitHub snapshot, email delivery,
  verification, migration, and publication authorization/result models.
- GitHub ID normalization: canonical string IDs now match numeric GitHub API IDs
  for installation binding, repository fetch, and publication.
- A runnable local demo, lockfile, integration requirements, and GitHub Actions
  configuration. The workflow needs a push before hosted execution is possible.

## Backend integration

Use the existing service interfaces. After the backend has verified enrollment,
current consent, repository ownership, and a pinned snapshot:

1. Pass scanner dependencies and the declared change to `analyze_impact`.
2. Build a verification plan with `build_verification_plan`. Trust configuration
   is backend-owned and must identify only explicitly registered team fixtures.
3. Register `Scenario.from_catalog(plan, target_contract_snapshot_id)` in the
   runner's scenario map. Register the matching trusted snapshot separately.
4. Call `runner.verify_regression(plan, root)` and persist the canonical result.
5. Construct `ApprovedTarget` from exact scanner evidence, then call
   `MigrationService.generate_migration` with the stored verification result.
6. On the consumer's explicit publication action, recheck authorization and call
   `PublicationService.publish_migration`. Never accept a client-supplied receipt
   or a client assertion that tests passed.

`migration.stripe_demo` illustrates the flow with clearly labeled scanner evidence
fixtures and placeholder identities. It is a local demo entry point, not an HTTP
handler or a production authorization implementation.

## Live acceptance still required

Use a team-owned private consumer repo containing the checkout fixture. Configure a
GitHub App, signer, authenticated-user installation flow, selected-repository grant,
webhook secret, and backend state/consent callbacks. These are not supplied by this
branch. The application also still needs Ishan's routes bound to the real services.

Validate selected-repo access, a pinned fetch, and revocation. A GitHub CLI login
alone does not demonstrate consumer GitHub App authorization. Then, with the
consumer's explicit approval, publish one validated migration PR and repeat the
request to confirm the same PR is reused. The local HTTP-double tests do not count
as live acceptance. Invitation sending likewise needs a configured mail transport.

Snapshot indexes and validation receipts remain process-local. After restart,
refetch/revalidate before publication. Durable job/receipt persistence, locking,
worker cancellation, arbitrary-repository isolation, and consumer/private artifact
routing remain outstanding integration work. Do not bypass these limits by marking
customer repositories trusted.

## Reproducible verification

See `demo/stripe-checkout/README.md` for setup. Run `python -m pytest -q` using the
integration environment, then `PYTHONPATH=app python -m migration.stripe_demo`.
Tests cover the real SDK regression and patch, local-currency negative control,
old-contract incompatibility after patching, evidence scope, sandbox refusal,
missing SDK, tampered report, Python/TypeScript legacy catalog compatibility,
canonical GitHub/email results, and canonical publication/retry/authorization.

## Local validation on October 17, 2026

75 tests passed using Python 3.14.5 and the pinned Python dependencies. The local
Stripe demo returned `verified_regression` then `patch_validated`, with two native
tests per phase. Source files remained unchanged. Hosted CI has not run yet.
GitHub transport/publication were tested with doubles, not live credentials.
