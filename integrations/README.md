> Stripe integration update: see [the current handoff](../migration/STRIPE_HANDOFF.md)
> and [runnable demo](../demo/stripe-checkout/README.md). The sections below describe
> the original scaffold; the new checkout scenario now supports a simulated Stripe
> change and canonical schema integration is tested. Live setup remains incomplete.

# Sai adapters — integration notes

This is an owned-module scaffold, not the Shockwave app. The workspace was empty.
No root manifests, shared schemas, routes, scanner, analyzer, or dashboards were created.

## Wiring

Production result construction imports canonical Pydantic classes from
`shared.schemas` at call time. `integrations.fakes.fake_models` is a deliberately
unvalidated test double, never a production fallback. Public service methods retain
the CONTRACTS.md field/argument names, with dependencies supplied in constructors:

- `GitHubAdapter.fetch_repository(connection, commit_sha)`
- `EmailService.send_invitation(recipient_email, acceptance_url, idempotency_key)`
- `RunnerService.verify_regression(plan, repo_root)`
- `MigrationService.generate_migration(request, risk, verification, repo_root)`
- `PublicationService.publish_migration(migration, connection, authorization)`

Bind these instance methods into backend orchestration. No global credential or
service singleton is created. Source roots and target registrations are internal
server inputs; never accept them from request bodies.

`GitHubHTTPTransport` provides bounded HTTPS I/O. Supply a short-lived app JWT signer
as `app_jwt`; private-key storage and app configuration are deployment concerns.
`verify_installation_binding` requires an authenticated user's access token and an
atomic `consume_state` callback binding state to user, organization, session and expiry.
It checks that user's accessible installations and then checks the app installation.
Ishan must persist the verified binding; a callback installation ID is insufficient.

Every fetch checks current consent and live repository access. Cache
keys are numeric repository ID plus full SHA. Each fetch gets a separate copied
snapshot, whose hash is rechecked through `adapter.snapshot_root`; do not serialize
these paths. Cache metadata is currently process-local and re-downloads after restart.
Invoke `purge_repository` on revocation, including after a restart, to remove copied source,
and delete corresponding findings/artifacts under backend retention policy. Use a
single worker per cache until backend locking is added.

`verify_webhook` verifies the signature over original raw bytes. The route must bound
body size, deduplicate delivery IDs and update/cancel connections/jobs. Signature
validation does not itself perform lifecycle actions.

`EmailService` uses a durable SQLite claim/receipt ledger. Configure either
`DevelopmentInbox(environment='development'|'test')` or `TransactionalTransport`
with a provider callable returning a genuine receipt and honoring idempotency keys.
Transport exceptions are sanitized. A crash/ambiguous send remains failed/unknown;
reconcile at the provider before a new backend-authorized retry key. The development
inbox is private local storage, not an HTTP-accessible static directory. Raw URLs
are present only in that inbox/message payload, never the ledger or ordinary logs.

## Trusted execution and migration

Only register explicitly team-controlled fixtures with
`TrustedSnapshot.register_team_fixture`. Never register downloaded customer code as
trusted merely to bypass missing sandbox support. Hashes are rechecked before every
run. Both sandbox profiles fail closed with `sandbox_unavailable`; no container
execution has been implemented or certified. The host runner is not a security
sandbox: network, memory and filesystem access are not isolated. It strips inherited
service variables, bounds logs/time, disables pytest plugin autoload, uses temporary
copies and attempts process-group cleanup. Some host sandboxes deny group signals;
direct process termination is the fallback for these trusted fixtures only.

Kanishk supplies backend-owned `Scenario` records with fixture IDs, responses,
unchanged test files, relevant assertion names and target-contract IDs. This is
internal execution configuration, not an addition to the frozen public models.
Tests read `SW_RESPONSE` as JSON. Python requires pytest; Node uses native node:test.
Provide all relevant native test files in the registered plan. A passing subset
cannot establish compatibility elsewhere. JS is demonstrated; TS must be prepared
into an approved executable form separately, with no installation during tests.

Noirit/Ishan supply `ApprovedTarget` records from scanner evidence, including exact
file, line, expression, file hash, snapshot, commit and target contract. The current
rule is exclusively Demo Payments amount-access `/ 100`, not a generic codemod and
never a claim about Stripe. The optional runtime callable receives only that compact
expression, requires separate model-processing consent and may return only the
allowed replacement. Attempts are capped at two; artifacts from failed validations
are retained. No general model provider client is configured.

Validation receipts are internal and process-local. After restart, revalidate rather
than reconstructing trusted receipts from HTTP input. Backend persistence of receipts
is a future integration task. All response artifacts are consumer-private and must
be authorized by organization before reads/downloads.

## Publication gate

Publication is disabled unless `live_enabled=True` is explicitly configured.
The required `authorize(authorization, connection, receipt)` callback must recheck
consumer membership, explicit publication action, owning organization/repository,
snapshot-to-connection linkage, expected commit and current consent. Backend must
serialize publication jobs per repository. Do not use the permissive test callback.

The adapter independently requires a validated receipt, successful logs, write grant,
active connection, live installation permissions, exact repository identity and
unchanged base head. GitHub's Git data API writes blobs/tree/commit/ref without a
credential-bearing local git command; stable branch identity and PR lookup recover
replays. A concurrent branch creation fails without creating a second branch;
backend retries the same job. There is no atomic GitHub compare-and-create PR API;
keep the base quiescent for the demo and surface stale-head/conflict failures.
Publication never merges or deploys. Return URLs only from GitHub's PR API result.

## Dependencies / external setup

Runtime: Python 3.11+ standard library; pytest for Python fixture execution; Node with
native test/JUnit reporter support (tested Node 24.1.0). Production also needs Ishan's
Pydantic schema package. No root dependencies were edited. Tests used Python 3.14.5
and pytest 9.1.1 in `/tmp/shockwave-sai-venv`.

Live setup requires a GitHub App, JWT signer, authenticated user token flow,
installation/selected repo, contents read and separately contents+PR write,
webhook secret, consent enforcement, transactional email configuration, and explicit
user authorization for the concrete live destination/action. None was exercised.

API references checked against GitHub documentation:
- https://docs.github.com/en/rest/apps
- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
