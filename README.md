# Shockwave: know what breaks before you ship

API providers can't see how their customers use their API. A change can keep every field name and
type the same and still break customers: if Stripe's `amount` switched from cents to dollars, a
store that divides by 100 would show a $10.00 order as $0.10, and a fraud rule written as
`amount > 500000` would stop flagging $6,000 payments. Schema checks pass and nothing crashes.

Shockwave closes that gap:

1. **Discover** the provider's API contract (Stripe's real published OpenAPI spec: 644 operations).
2. **Scan** the repositories a consumer chooses to share, and find where their code depends on the API.
3. **Assess** a proposed change, including changes in meaning that a schema diff can't see.
4. **Prove** each break by running the consumer's own code against the current and proposed responses.
5. **Fix** it with a migration patch that is re-tested before it is offered.

The provider only ever sees counts (predicted, verified, validated). Consumer code, file names,
logs and patches stay private to the consumer.

## Quick start (the website)

Prerequisite, once per clone:

```sh
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
```

Then start everything with one command:

```sh
./start-website.sh
```

- It starts the backend on port 8000 and the website on port 3000. `Ctrl+C` stops both.
- On the first run it installs the website's and the Stripe checkout's Node dependencies (this needs network).
- It uses the repository's own Node (`.venv/bin/node`), so the system Node doesn't matter.
- Stop any older backend on port 8000 first.

Open http://127.0.0.1:3000. Demo mode only answers requests from the same computer.

**Start clean before a demo**, or after pulling backend changes:

```sh
rm -rf app/shockwave.db app/.shockwave-integrated
```

## Website walkthrough (Stripe)

The sign-in page lists two demo workspaces. Click **Open** next to one to enter it; no password is needed.

| Workspace | Plays | Email |
|---|---|---|
| Provider | Stripe's API team ("Demo API team") | `provider@example.test` |
| Consumer | Acme, a store built on Stripe ("Acme storefront") | `consumer@example.test` |

Use a normal window for the provider and a private window for the consumer. A browser keeps one
login per site, so two tabs in the same window can't be signed in as different people.

**Provider**
1. **Overview** → **Create Stripe API project**.
2. **Endpoints** → **Load contract**, then **Refresh**. This loads 644 operations from Stripe's pinned spec.
3. **Consumers** → enter `consumer@example.test` → **Invite consumer**.

**Consumer**

4. **Invitations** → **Review invitation** → tick consent → **Accept invitation**.
5. **Repositories** → **Connect repositories**:
   - Tick `acme/stripe-checkout`, `acme/fraud-review` and "Allow central processing…".
   - Click **Select 2 repositories**.
   - Click **Scan / retry** on each row.

**Provider**

6. **Changes** → **Assess change**. The schema stays `integer → integer`. The declared change is
   `units_changed`. Shared impact shows 2 predicted.

**Consumer**

7. **Findings** → **Inspect** `src/orderSummary.ts` → **Run verification (current vs proposed)**.
   The current contract shows $10.00; the proposed contract shows $0.10, "Regression proven".
8. **Generate migration patch** → **Review patch diff**. The fix removes the `/ 100` and is validated.
9. Repeat for `fraud_service/review.py`. A $6,000 payment is flagged today and not flagged after the
   change; the patch changes `500000` to `5000`.

**Provider**

10. **Overview** or **Changes**, then **Refresh**: 2 predicted, 2 verified, 2 validated. Counts only.

If a result doesn't appear within a few seconds, click **Refresh** in the top bar.

### What is real and what is a stand-in

| Real | Stand-in |
|---|---|
| Stripe's published OpenAPI spec, pinned (`2026-09-30`) | The change itself: Stripe has not announced it |
| Official Stripe SDKs in the consumer apps | The proposed responses are prepared fixtures served locally; nothing calls Stripe |
| The scanner reading the code and finding both use sites | Acme is fictional; its repositories are local copies (`demo/templates`), not GitHub |
| Running the consumer's own code and tests ($0.10 is real output) | Migration patches are prepared, then genuinely re-tested |
| Email/password accounts (scrypt), SMTP when configured | Without SMTP, invitation emails go to a development inbox |
| | Pull-request publishing is turned off |

## Accounts and email

Besides the demo workspaces, anyone can create a provider or consumer account under **Create account**.
Invitations are notifications only. The invited person signs in with that email and accepts under
**Invitations**. SMTP settings are in `app/README.md` and `app/.env.example`.

## Command-line demo

The same Stripe story without the website (about 20 seconds, offline after `prepare`):

```sh
.venv/bin/python demo/scripts/demo.py prepare   # once, needs network for npm
.venv/bin/python demo/scripts/demo.py all
```

Details: `demo/README.md`. Talk track: `demo/DEMO_SCRIPT.md`.

## Repository layout

| Owner | Area |
|---|---|
| Ishan | `app/`: platform backend, job queue, snapshot lifecycle, shared contracts (`app/shared/schemas.py`) |
| Noirit | `scanner/`, `examples/consumers/`, email/password accounts and invitation email |
| Kanishk | `discovery/`, `analyzer/`, `verification/scenarios/`, `demo/`, integrated mode (`app/backend/integrated.py`), website design |
| Sai | `integrations/` (GitHub), `verification/runner/`, `migration/` |

## Tests

```sh
PATH=.venv/bin:$PATH .venv/bin/python -m pytest -q -p no:cacheprovider                  # repository root
cd app && PATH=../.venv/bin:$PATH ../.venv/bin/python -m pytest -q -p no:cacheprovider   # platform
cd app/frontend && PATH=../../.venv/bin:$PATH npm run typecheck                          # website
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| A scan fails with "already recorded differently" (`RESULT_ID_CONFLICT`) | The database is from an older version. Stop, run `rm -rf app/shockwave.db app/.shockwave-integrated`, start again. |
| "Authorized organization membership required" | Another tab signed in as someone else. Use one private window per person. |
| Repositories list `acme/storefront` instead of the Stripe apps | The backend isn't in integrated mode. Start it with `./start-website.sh`. |
| `env: node: Bad CPU type` | Use `./start-website.sh`; it puts `.venv/bin` (the repository's Node) first on `PATH`. |
| Port 8000 already in use | Stop the old backend first; it may lack current routes. |

Merge history and open items: `INTEGRATION.md`. Platform internals: `app/README.md`.
