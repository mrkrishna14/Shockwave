# Shockwave platform preview

A runnable local implementation of Ishan's platform scope: canonical contracts, persisted authorization/onboarding, job orchestration, provider summaries, and private consumer dashboards. This is **not a completed live Shockwave product**. Teammate-owned discovery/scanner/GitHub/email/runner/migration modules were not supplied. Clearly labeled platform doubles exercise their boundaries, with real Python and JavaScript fixture assertions.

## Install

Requires Python 3.12+ and Node 20.9+. Tested with Python 3.13.5 and Node 20.17.0.

```sh
cd /Users/ishanmysore/Documents/Ishan/Berkeley/shockwave/app
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
```

Alternatively use `uv venv .venv` and `uv pip sync requirements.lock`. Dependency versions are pinned in the Python lock and frontend package lock.

## Start (two terminals)

From this `app/` directory:

```sh
./scripts/start-backend.sh
```

```sh
npm run dev --prefix frontend
```

Open http://127.0.0.1:3000. Backend documentation: http://127.0.0.1:8000/docs.
Keep this local demo on loopback. The persona switcher deliberately simulates identity and verified email. It grants no real GitHub access. With `SHOCKWAVE_DEMO` unset, demo sign-in is disabled; real authentication remains an integration prerequisite.

## Rehearse

1. Choose Provider workspace. Create Demo Payments; load fixture inventory.
2. Open Consumers; invite `consumer@example.test`. Delivery is development inbox, not email.
3. Switch to Consumer. Review invitation; accept the processing/sharing terms.
4. Connect the demo installation, explicitly consent to repository processing, and select the fixture repository. Scan.
5. Switch to Provider → Changes; assess the fictional cents-to-dollars change.
6. Switch to Consumer → Findings; inspect either language. Run verification: old pass / proposed fail.
7. Generate the fixture patch, review the diff and passing tests. Deployment remains unknown. Publication requests are blocked by the absent live adapter; they never fabricate a PR URL.

The isolated CLI demo does not modify the browser's database or contact external services:

```sh
PYTHONPATH=. .venv/bin/python scripts/demo.py
```

It writes `docs/sample-demo-output.json`. Both language fixtures should report baseline exit 0, proposed exit 1, validation exit 0, and 2 validation tests.

## Checks

```sh
.venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/python scripts/generate_contracts.py
npm run typecheck --prefix frontend
npm run build --prefix frontend
```

Production build uses Next.js's supported webpack option because Turbopack's compiler subprocess could not bind its local IPC port in the managed build environment. Dev uses Turbopack. No framework versions were downgraded.

## Integration

Read `docs/HANDOFF-platform.md` and `docs/INTEGRATION.md`. Canonical Python types are in `shared/schemas.py`; `frontend/contracts.ts` and JSON schemas are generated. Do not fork model definitions in teammate modules. External sending, PR creation, pushes, merges, and deployment were not performed.
