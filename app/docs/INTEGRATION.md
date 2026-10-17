# Platform integration boundary

## Current modes

`SHOCKWAVE_DEMO=1` enables explicitly labeled local modes, restricted to loopback clients. `SHOCKWAVE_ADAPTERS=fixture` runs platform-owned doubles; `SHOCKWAVE_ADAPTERS=integrated` calls the owners' discovery, scanner, analyzer, verification, and migration modules against team-owned local repositories. Stripe is a selectable provider using a pinned public contract and an explicitly hypothetical change. Both modes use demo identities and simulated repository selection and delivery. Neither supports live GitHub publication or arbitrary repository execution. `SHOCKWAVE_DEMO=0` rejects existing demo sessions and all private routes with `PRODUCTION_UNCONFIGURED` until production identity and adapters are implemented. `GET /api/v1/capabilities` reports actual availability and prerequisites.

The repository-root `discovery.service` and the scanner, analyzer, runner, and migration modules are wired through `backend.integrated` for trusted local sources. `integrations.github` and `integrations.email` have transport components but are not connected to platform routes as live services. Do not infer live availability from their importability.

## Typed seams

`shared/schemas.py` defines `shockwave.v1`. `backend/adapters.py` records discovery, scanner, analysis, and I/O protocols. `backend/orchestration.py` validates typed adapter outputs, service identity, enrollment/organization, and pinned snapshot IDs before persistence. Both local adapters receive a trusted consumer identity. The orchestrator passes `Path('.')` as an unused placeholder; integrated adapters resolve registered local copies themselves. A future live adapter must receive a server-resolved checkout root and never treat that placeholder as a user selection.

Separate production protocols now define verified identity, installation binding, email delivery, isolated execution, and publication. They have no registered implementations or fallback to demo services. A deployment must bind each service explicitly and keep private routes unavailable until the identity and isolation requirements are satisfied.

The backend currently restricts discovery HTTP input to `demo-fixture`; it does not fetch arbitrary URLs. Assessment is restricted to canonical demo snapshots. The fictional `demo-payments` cents-to-dollars change is separate from any Stripe inventory. The provider only receives `ProviderImpactSummary` and sanitized onboarding status. Private risk evidence, paths, logs, patch diff, and PR URLs remain consumer-scoped.

Extra platform routes under `/api/v1` include `GET /capabilities`, `GET /jobs`, `DELETE /jobs/{id}`, `POST /jobs/{id}/retry`, `GET /enrollments/{id}/repositories`, `GET /risks/{id}/snapshot`, and `GET /api-projects/{id}/consumers`. List routes return `{items,next_cursor}` and accept a cursor and limit from 1 to 100. Cursors identify the last item in the ordered result, so appends do not shift the next page. Lists are still assembled in memory; high-volume deployments should move filtering and keyset selection into SQL.

## Persistence and jobs

SQLite `user_version` migrations add the lease table, attempt history, outbox, enrollment-specific repository grants, a claim-status index, and durable source-cleanup intents without dropping old records. A migration copies legacy connection grants into existing enrollment mappings. `Store.read()` does not reserve a writer; `Store.tx()` is for short mutations. Each claim gets a random owner and increasing generation. Heartbeats extend its lease. Completion checks owner, generation, expiry, cancellation, and job state in the same write transaction as result persistence. A stale worker cannot commit a successful result. The worker filters eligible jobs before limiting a claim batch, so old completed jobs cannot starve new work. Queued jobs survive an app restart; expired running jobs are claimable up to three attempts, with abandoned attempts recorded. Job retries create a new job only for retryable failures and require an idempotency key. `/api/v1/job-metrics` reports organization-scoped queue counts and the oldest queued timestamp.

The API can run inline fixture work for single-process development. `SHOCKWAVE_INLINE_JOBS=0` leaves jobs for `scripts/start-worker.sh`, which also dispatches pending invitation outbox records. The worker may repeat computation after a crash. Invitation delivery uses a stable operation key. A transport exception after possible delivery, or a dispatch abandoned for five minutes, marks `needs_reconciliation` and is not automatically resent. Provider onboarding shows this attention state. Live publication is unavailable; no outbox implementation should imply an actual PR or exactly-once external effect.

Consent revocation cancels queued/running jobs, clears scoped artifact contents, and queues deletion of integrated source copies. The API attempts deletion immediately; a later startup or worker pass retries committed cleanup intents after a crash. Abandoned copies with no SQLite snapshot are removed after a 24-hour grace period when no scan is active. A running fixture computation may finish locally, but its fenced result write is blocked after cancellation. Structured findings remain in SQLite and become inaccessible to the revoked enrollment. Physical finding deletion and retention periods require a policy before live source processing.

## Readiness policy

`backend/readiness.py` counts a risk as current only when it belongs to the latest selected scan snapshot for its repository. Verification must target that snapshot; validation must target the same snapshot and latest verified result, exit successfully without timeout/infrastructure failure, and run tests. A rescan or newer failed verification invalidates prior green counts. Approval, merge, and deployment are separate states; deployment remains unknown in the fixture.

## Next owner actions

1. **Identity/platform:** provide issuer/audience/email verification and real membership provisioning before private-source access. Keep demo identities isolated.
2. **Discovery/analysis owner:** finish `discovery.service`, parser/source reconciliation, change analysis, and scenario construction; agree on canonical identities and bounded fetched inputs.
3. **Scanner owner:** provide normalized Python and JavaScript/TypeScript dependencies over a trusted pinned checkout.
4. **Transport/runner owner:** supply signed GitHub installation callbacks and webhooks, server-verified repository lists, pinned checkout and freshness checks, email delivery reconciliation, isolated test execution, targeted patch generation, and consumer-authorized PR publication.
5. **Integration owner:** wire those implementations through explicit factories, add live-mode tests around the effect boundaries, and retain provider redaction and stale-result policy.

For live external I/O, add operation-specific outbox processing, remote reconciliation, non-retryable effect policy, bounded URL/archive/patch checks, and resource isolation. A timeout and stripped environment around trusted fixtures is not a sandbox.
