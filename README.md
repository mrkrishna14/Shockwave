# Shockwave: know what breaks before you ship

Shockwave discovers a provider's API contract, scans authorized consumer repositories, evaluates a
proposed API change, reproduces the downstream regressions it causes, and prepares tested
migrations. This repository merges the four owners' modules:

| Owner | Area |
|---|---|
| Ishan | `app/`: shared contracts (`app/shared/schemas.py`), backend, frontend |
| Noirit | `scanner/`, `examples/consumers/` |
| Kanishk | `discovery/`, `analyzer/`, `verification/scenarios/`, `demo/` |
| Sai | `integrations/`, `verification/runner/`, `migration/` |

## Setup

```sh
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
```

Node 24 is `.venv/bin/node` (from `nodejs-wheel`). Put `.venv/bin` first on `PATH` when a step
needs Node.

## Demo 1: Stripe (the main demo)

Stripe's real, pinned public contract and official SDKs, three team-built consumer apps, and a
**simulated** change (USD `amount` from cents to dollars; not announced by Stripe). Everything runs
offline.

```sh
.venv/bin/python demo/scripts/demo.py prepare   # once, needs network for npm
.venv/bin/python demo/scripts/demo.py all       # about 20 s, offline
```

Details are in `demo/README.md`. The talk track is in `demo/DEMO_SCRIPT.md` (three minutes).

## Demo 2: the platform UI on the real modules (Stripe or fictional Demo Payments provider)

One command, from any folder:

```sh
./start-website.sh
```

It starts the backend (port 8000, integrated mode: the owners' real modules) and the website
(port 3000) with the repository's own Node. `Ctrl+C` stops both. Stop any backend already using
port 8000 before running the script; a process started before a pull may lack current dashboard routes.
The launcher checks the Stripe checkout's Node dependencies and prints the exact install command if
they are missing. Integrated scans accept only a 40-character lowercase hexadecimal fixture revision;
each scan keeps its own immutable source copy.

Open http://127.0.0.1:3000 and follow the walkthrough in `app/README.md`. To run the same flow
headless:

```sh
cd app && PATH=../.venv/bin:$PATH PYTHONPATH=.:.. ../.venv/bin/python scripts/integrated_demo.py
```

## Tests

```sh
PATH=.venv/bin:$PATH .venv/bin/python -m pytest -q -p no:cacheprovider                  # repository root
cd app && PATH=../.venv/bin:$PATH ../.venv/bin/python -m pytest -q -p no:cacheprovider   # platform
```

Merge history, fixes, results and open items are in `INTEGRATION.md`.
