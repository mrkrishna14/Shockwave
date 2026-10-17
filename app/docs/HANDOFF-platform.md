# Platform handoff — October 17, 2026

## Checkpoint

Repository: `/Users/ishanmysore/Shockwave`, branch `Ishan`. This upgrade remains **uncommitted** for review. `plan/` is local planning material and was not changed. No push, PR, email, merge, or deployment was performed. The older implementation handoff described a separate nested repository; this file now describes the outer Shockwave repository.

## Implemented in this upgrade

- Additive SQLite migrations through schema version 4. Existing records and demo data are retained. Read-only routes no longer reserve the writer slot.
- Durable queued jobs with atomic claims, lease ownership, monotonically increasing fencing generation, heartbeat, capped attempts, persisted attempt history, cancellation, explicit retry, and a separate worker entry point. Adapter computation runs outside write transactions; a stale or cancelled worker cannot commit a result.
- Invitation outbox dispatch after transaction commit, with a stable operation key. An exception after possible delivery or an abandoned dispatch becomes `needs_reconciliation`; no blind resend occurs. Provider onboarding exposes that attention state.
- Enrollment-specific repository model and PR write grants, validated at publication request time. Reusing a connection does not transfer another enrollment's write grant.
- Typed adapter output checks for service, organization, enrollment, repository, and pinned snapshot consistency. Live adapter modules remain unavailable. Fixture context is supplied at adapter construction rather than mutated during a job.
- Current evidence policy: only the latest selected scan snapshot, its latest verified regression, and a patch validated against that verification can contribute to readiness. Rescans and newer failures make old results stale.
- Capability, enrollment repository, consumer onboarding, and risk snapshot endpoints. Bounded list pagination and explicit record order. Job attempts/cancel/retry APIs.
- A modular provider/consumer dashboard with explicit organization, project, enrollment, and repository selection; URL-backed navigation; scoped fetch cancellation; active-job polling; private finding history; server-derived publication revision; and responsive layouts.
- Updated API, worker, frontend startup instructions and integration boundaries.

## Verification completed

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest -q -p no:cacheprovider` | 27 passed; one Starlette TestClient/httpx deprecation warning |
| `npm run typecheck --prefix frontend` | Passed |
| `npm run build --prefix frontend` | Passed, Next.js 16.4.2 webpack build |
| Browser flow at isolated `127.0.0.1:3001` / API `127.0.0.1:8001` | Provider project + fixture inventory + invitation; consumer acceptance + repository selection + scan; provider assessment; consumer regression verification and validated patch |
| Responsive inspection | Default viewport and 390px mobile viewport; fixed a measured 48px horizontal overflow; document width became 390px |
| Browser navigation | Back navigation restored selected private finding and its current result |

The browser test used a separate temporary demo database, not the existing local server's database. It displayed one documented fixture endpoint, two predicted risks, baseline exit 0, proposed exit 1, two validation tests with exit 0, publication unavailable, and deployment unknown. Console error/warning check returned none. Existing screenshots in `docs/` are from the earlier implementation; no new screenshot file was written by this upgrade.

## Runtime settings

`SHOCKWAVE_DEMO=1` enables local simulated identities and fixture capabilities. `SHOCKWAVE_DB` selects the SQLite file. `SHOCKWAVE_ORIGIN` controls accepted browser mutation origin. `SHOCKWAVE_SECURE_COOKIE=1` sets the cookie Secure flag. `SHOCKWAVE_INLINE_JOBS=0` leaves work for the standalone worker. The frontend uses `SHOCKWAVE_API_URL` to override the proxy target when needed. No credential values belong in these files.

Run `./scripts/start-backend.sh`, `./scripts/start-worker.sh`, and `npm run dev --prefix frontend` in separate terminals from the app root. The API and worker must share settings and database path. The frontend starts on port 3000 by default.

## Limits and next actions

This remains a local fixture implementation. No real Stripe inventory, provider source scan, arbitrary consumer repository scan, signed GitHub connection, email delivery, isolated execution of user code, runtime model call, or PR publication is implemented. `discovery.service` is absent in the current teammate-owned package. The demo subprocess uses a timeout and stripped environment, which is not general sandbox isolation.

The worker prevents stale local result writes. It cannot undo an external effect already accepted by a remote service. The invitation outbox marks uncertain delivery for reconciliation; live publication needs its own durable outbox, remote SHA freshness verification, reconciliation policy, and explicit authorization before being enabled. List pagination uses offset cursors; high-volume concurrent writes need keyset pagination. The generic records table still relies on platform checks for several semantic relationships. Structured findings become inaccessible after revocation but remain stored; define physical retention/deletion policy before processing real source.

Next integration action: receive the discovery, scanner, authenticated repository/transport, isolated runner, and migration adapters from their owners; wire them through explicit factories and trusted paths; add tests for real external effect boundaries. Keep provider response redaction, consumer consent, and current-result policy as release gates.
