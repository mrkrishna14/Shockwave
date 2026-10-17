# Shockwave platform

The platform is a permissioned, local demonstration of API change impact. It stores provider projects, invitations, consumer enrollment and repository consent, jobs, private evidence, and provider summaries. The integrated mode calls the owners' real discovery, scanner, analyzer, verification, and migration modules on trusted local copies, including Stripe consumer examples. Stripe's proposed change is hypothetical. It does **not** scan arbitrary repositories, connect GitHub, publish a PR, or provide production authentication. Email/password accounts and SMTP invitation notices are available (see [Accounts and email](#accounts-and-email)).

## Install

From `/Users/ishanmysore/Shockwave/app`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
```

Requires Python 3.12+ and Node 20.9+. Python and frontend versions are pinned in `requirements.lock` and `frontend/package-lock.json`.

## Start locally

Use three terminals in this directory. The API and worker must share `SHOCKWAVE_DB` and `SHOCKWAVE_DEMO` settings.

```sh
./scripts/start-backend.sh
```

```sh
./scripts/start-worker.sh
```

```sh
npm run dev --prefix frontend
```

Open `http://127.0.0.1:3000`; API documentation is at `http://127.0.0.1:8000/docs`. Both servers bind loopback. `start-backend.sh` sets `SHOCKWAVE_INLINE_JOBS=0`, so queued jobs are claimed by the worker. If using only the API for a quick fixture check, set `SHOCKWAVE_INLINE_JOBS=1`; that runs bounded demo work in FastAPI background tasks. `SHOCKWAVE_API_URL` controls the frontend proxy when the API uses a different local port. See `.env.example` for other settings. Do not expose the demo persona switcher publicly.

The worker uses SQLite leases and fencing. A second worker may run against the same database. A healthy lease is not reclaimed; an abandoned lease can be reclaimed up to three attempts. Job completion, cancellation, and consent checks happen in short transactions. Adapter calls run outside those transactions. A new worker may repeat local computation after a crash, so external effects require their own idempotency and reconciliation.

## Accounts and email

Anyone can create a **provider** or **consumer** account at the sign-in screen. Each sign-up creates a user (email + scrypt-hashed password) and a new organization of that type, with the user as admin. Emails are unique, so you can test with as many distinct providers and consumers as you like. The one-click demo personas are still offered when `SHOCKWAVE_DEMO=1`.

Invitations are addressed to an email. The invitation email is a **notification only**. It names the provider and API and tells the recipient to sign in. It never contains an acceptance link or token. The invited consumer signs in (or signs up) with that exact email and accepts under **Invitations**. Other accounts cannot see or accept it.

To send real email, set the SMTP variables (see `.env.example`):

```sh
SHOCKWAVE_SMTP_HOST=smtp.example.com
SHOCKWAVE_SMTP_PORT=587            # 465 uses implicit TLS; otherwise STARTTLS unless SHOCKWAVE_SMTP_STARTTLS=0
SHOCKWAVE_SMTP_USERNAME=...
SHOCKWAVE_SMTP_PASSWORD=...
SHOCKWAVE_SMTP_FROM="Shockwave <noreply@example.com>"
SHOCKWAVE_APP_URL=http://127.0.0.1:3000   # link to the app shown in the email; defaults to SHOCKWAVE_ORIGIN
```

Without SMTP, demo mode puts the message in the development inbox. The consumer can read it under Invitations → Development inbox.

Limits: sign-up does not yet send an email-verification message. The first person to register an address owns it, which is fine for local testing but not for a public deployment. There is no password reset. Sign-in and sign-up are rate-limited per client IP.

## Fixture walkthrough

1. Sign in as Provider (demo persona or a new provider account). Create Demo Payments and load its fixture endpoint.
2. Invite the consumer's email (`consumer@example.test` for the demo persona). They get a notification email, or a development inbox entry without SMTP.
3. Sign out and sign in as that consumer. Review the invitation under Invitations and grant enrollment consent.
4. Connect the fixture installation, select a repository, and grant its separate processing permission. Scan it.
5. Return to Provider and assess the fictional cents-to-dollars change. It is **not** a Stripe change.
6. Return to Consumer. Inspect a finding, run old/new verification, and generate a patch. Old tests pass, proposed tests fail on `$0.10`, and the validated patch passes two fixture tests.
7. Publication remains disabled; no PR or deployment is claimed.

The browser uses the server's capability report to label unavailable actions. Repo permissions are stored per enrollment. A rescan makes evidence from an older snapshot stale.

## Checks

```sh
.venv/bin/python -m pytest -q -p no:cacheprovider
npm run typecheck --prefix frontend
npm run build --prefix frontend
```

The isolated command line demo remains available:

```sh
PYTHONPATH=. .venv/bin/python scripts/demo.py
```

It writes `docs/sample-demo-output.json`; use a copy if you need to keep that tracked sample unchanged. The demo uses a temporary SQLite database and no external transport.

## Integration boundaries

The API lives in `backend/main.py`, storage and additive migrations in `backend/store.py`, durable claims in `backend/jobs.py`, adapter execution in `backend/orchestration.py`, invitation delivery state in `backend/outbox.py`, and current-result policy in `backend/readiness.py`. Pydantic contracts in `shared/schemas.py` remain canonical; `frontend/contracts.ts` and the JSON schema are generated.

Integrated mode has real scanner, discovery, and analysis wiring for trusted team-owned examples. Signed GitHub installation and snapshots, live email, sandboxed execution, migration publication, and production identity integration are still needed. The application fails closed when demo mode is disabled. See `docs/INTEGRATION.md` and `docs/HANDOFF-platform.md` for exact seams and limitations.
