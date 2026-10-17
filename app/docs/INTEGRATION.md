# Platform integration boundary

## Current modes

`SHOCKWAVE_DEMO=1` enables an explicitly labeled local fixture. Demo accounts, repositories, discovery, scanning, analysis, test code, and patches are embedded. The fixture's native Python and Node assertions execute only trusted embedded code. `SHOCKWAVE_DEMO=0` disables demo sign-in and cannot process live source until real identity and adapters are configured. `GET /api/v1/capabilities` reports actual availability; it never advertises PR publication.

The repository-root `discovery/` package currently lacks its `service` entry point. Scanner, analyzer, GitHub/email, runner, and migration modules are not wired as live implementations. The platform must not infer availability from an importable package name alone. Do not replace an owner's module with a competing implementation in this platform tree.

## Typed seams

`shared/schemas.py` defines `shockwave.v1`. `backend/adapters.py` records discovery, scanner, analysis, and I/O protocols. `backend/orchestration.py` validates typed adapter outputs, service identity, enrollment/organization, and pinned snapshot IDs before persistence. The demo adapter receives its trusted organization context at construction; live adapters need an explicit factory and a server-resolved checkout root. `Path('.')` is only passed in fixture mode and must never be interpreted as a selected user repository.

The backend currently restricts discovery HTTP input to `demo-fixture`; it does not fetch arbitrary URLs. Assessment is restricted to canonical demo snapshots. The fictional `demo-payments` cents-to-dollars change is separate from any Stripe inventory. The provider only receives `ProviderImpactSummary` and sanitized onboarding status. Private risk evidence, paths, logs, patch diff, and PR URLs remain consumer-scoped.

Extra platform routes under `/api/v1` include `GET /capabilities`, `GET /jobs`, `DELETE /jobs/{id}`, `POST /jobs/{id}/retry`, `GET /enrollments/{id}/repositories`, `GET /risks/{id}/snapshot`, and `GET /api-projects/{id}/consumers`. List routes return `{items,next_cursor}` and accept a cursor and limit from 1 to 100. Cursors encode an offset over deterministic row order; concurrent insertions can shift pages, so replace with keyset cursors for high-volume use.

## Persistence and jobs

SQLite `user_version` migrations add the lease table, attempt history, outbox, and enrollment-specific repository grants without dropping old records. A migration copies legacy connection grants into existing enrollment mappings. `Store.read()` does not reserve a writer; `Store.tx()` is for short mutations. Each claim gets a random owner and increasing generation. Heartbeats extend its lease. Completion checks owner, generation, expiry, cancellation, and job state in the same write transaction as result persistence. A stale worker cannot commit a successful result. Queued jobs survive an app restart; expired running jobs are claimable up to three attempts, with abandoned attempts recorded. Job retries create a new job only for retryable failures and require an idempotency key.

The API can run inline fixture work for single-process development. `SHOCKWAVE_INLINE_JOBS=0` leaves jobs for `scripts/start-worker.sh`, which also dispatches pending invitation outbox records. The worker may repeat computation after a crash. Invitation delivery uses a stable operation key. A transport exception after possible delivery, or a dispatch abandoned for five minutes, marks `needs_reconciliation` and is not automatically resent. Provider onboarding shows this attention state. Live publication is unavailable; no outbox implementation should imply an actual PR or exactly-once external effect.

Consent revocation cancels queued/running jobs and clears scoped artifact contents. A running fixture computation may finish locally, but its fenced result write is blocked after cancellation. Structured findings remain in SQLite and become inaccessible to the revoked enrollment. Physical finding deletion and retention periods require a policy before live source processing.

## Readiness policy

`backend/readiness.py` counts a risk as current only when it belongs to the latest selected scan snapshot for its repository. Verification must target that snapshot; validation must target the same snapshot and latest verified result, exit successfully without timeout/infrastructure failure, and run tests. A rescan or newer failed verification invalidates prior green counts. Approval, merge, and deployment are separate states; deployment remains unknown in the fixture.

## Next owner actions

1. **Identity/platform:** provide issuer/audience/email verification and real membership provisioning before private-source access. Keep demo identities isolated.
2. **Discovery/analysis owner:** finish `discovery.service`, parser/source reconciliation, change analysis, and scenario construction; agree on canonical identities and bounded fetched inputs.
3. **Scanner owner:** provide normalized Python and JavaScript/TypeScript dependencies over a trusted pinned checkout.
4. **Transport/runner owner:** supply signed GitHub installation callbacks and webhooks, server-verified repository lists, pinned checkout and freshness checks, email delivery reconciliation, isolated test execution, targeted patch generation, and consumer-authorized PR publication.
5. **Integration owner:** wire those implementations through explicit factories, add live-mode tests around the effect boundaries, and retain provider redaction and stale-result policy.

For live external I/O, add operation-specific outbox processing, remote reconciliation, non-retryable effect policy, bounded URL/archive/patch checks, and resource isolation. A timeout and stripped environment around trusted fixtures is not a sandbox.
