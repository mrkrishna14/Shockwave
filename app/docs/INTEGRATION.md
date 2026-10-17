# Integration boundary and recorded contract additions

## Ownership

Only `shared/`, `backend/`, `frontend/`, `tests/e2e/`, root config, `scripts/` bootstrap tooling, and `docs/` were created. No `scanner/`, `discovery/`, `analyzer/`, `integrations/`, `migration/`, or teammate fixture modules were implemented. The platform-owned `backend/demo.py` contains labeled doubles and embedded trusted fixtures, not a general scanner/runner.

Canonical schemas match the proposed `shockwave.v1` names and fields. `PublicationAuthorization` concretizes the requested internal fields as user_id, consumer_org_id, repository_connection_id, expected_commit, consent_timestamp. `DeliveryResult` follows CONTRACTS.md. Python Protocols in `backend/adapters.py` preserve the named module call signatures. Demo scanner organization context is supplied only inside the server-owned double, never a client field.

Additive platform REST routes (recorded here, not proposed renames):
- GET `/health`, `/api-projects`, `/jobs`, `/enrollments`, `/repository-connections`, `/api-projects/{id}/changes`.
- DELETE `/session`, `/enrollments/{id}`.
- Demo-only POST `/demo/session`, GET `/demo/inbox`.
All use the `/api/v1` prefix. Listing returns `{items,next_cursor:null}`; pagination is not implemented yet.

The local discovery HTTP input accepts only `spec_reference=demo-fixture`; it does **not** fetch URLs. The structured assessment HTTP input accepts canonical demo old/new IDs and optional canonical declared changes. These are deliberately restricted HTTP boundaries, not unrestricted discovery commands.

## Next owner actions

1. **Ishan + team:** agree on these canonical types and `shared/demo-identities.json`, then replace adapters with owners' typed implementations. Current app is intentionally gated to fixture operation; `demo=False` fails closed instead of silently invoking demo pipelines.
2. **Sai:** GitHub App account/installation verification and repository listing; pinned checkout resolution; email delivery; signed and deduplicated webhook processing; sandbox runner; migration generation/validation; publication with fresh remote SHA and repository write grant. Backend adapters are currently synchronous.
3. **Noirit:** scanner using `ScanRequest`, `RepositorySnapshot`, trusted repo root. Current fake claims partial fixture coverage and emits Python/JavaScript dependency shapes only.
4. **Kanishk:** real pinned Stripe OpenAPI parsing, authorized provider-source discovery, change detection, analysis, and plan construction. Real source/spec discovery was not implemented in someone else's module. Stripe is never conflated with the fictional units scenario.
5. **Ishan:** replace local sessions with an OIDC/session integration that verifies issuer/audience/email; handle real organization membership provisioning. Do not enable live private-source access behind the persona switcher.
6. Before live I/O: add durable email/publication outbox leases, authenticated install callback helpers, bounded SSRF-safe fetches, archive/patch path hardening, real cache retention cleanup, and resource-isolated test execution. The demo never reads or executes user-selected code, so these paths fail closed today.

## Storage and jobs

SQLite transactions enforce foreign keys for organizations, memberships, records, mappings, and idempotency. Parent record ownership is checked in backend queries; the generic records table does not enforce every semantic relationship in SQL. Bodies are validated by Pydantic and explicit ownership checks. User and organization are resolved from the session/membership, not trusted JSON. Tokens are random and hashed for invitation/session lookup; only the transport inbox contains the acceptance URL. The development inbox is recipient-scoped.

`BackgroundTasks` dispatches local jobs. Each job uses independent DB sessions and atomically changes queued → running → terminal. SQLite serializes execution; a worker transaction spans bounded local adapter work. This favors simple revocation ordering over concurrent read performance. Queued jobs are cancelled on revocation; running local work finishes before a competing revocation transaction commits. New work checks consent. On restart, interrupted queued/running jobs are explicitly failed with retry instructions; they are not silently replayed. Long-running live integrations need a durable outbox/lease design before production use.

Consent revocation clears scoped artifact contents immediately. The double creates no persistent source checkouts; temporary fixture directories are automatically removed. Structured evidence/findings remain in SQLite but become inaccessible after revocation. Define retention and full finding deletion policy before onboarding real source.

## Truthfulness

Discovery, dependency extraction, assessment, and patch selection are fixture-driven. Python unittest and Node's native test runner really execute the embedded scenario. Validation tests the exact displayed source transformation plus an unrelated conversion check. It is not validation against a real consumer repository or its existing suite. Subprocess timeouts and stripped environment are **not** sandbox isolation; arbitrary repository execution remains unavailable. No runtime model calls exist. Passing results cover only these fixture assertions.

No live publication returns success. PR, merge, approval, and deployment state never derive from job success. Provider serialization uses an explicit `ProviderImpactSummary` allowlist and never includes private risk details or artifact URLs.
