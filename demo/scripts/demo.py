"""Shockwave Stripe demo environment: one entry point for every documented step.

    .venv/bin/python demo/scripts/demo.py <command> [options]

Commands: fetch-sources, prepare, inventory, fixture-server, scan, submit-change, execute,
validate-migrations, reset, all, live-check. Run ``demo.py --help`` or ``demo.py <command> --help``.
Everything is offline except ``fetch-sources --network`` (read-only public GitHub fetch of the
pinned commits) and ``live-check`` (Stripe TEST mode; needs ``--authorize-live-test-mode`` and an
``sk_test_`` key in STRIPE_SECRET_KEY). The orchestration lives in ``demo/scripts/shockwave_demo``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS.parents[1]
# app/ holds Ishan's canonical contracts (shared.schemas); with it on the path every module uses them.
for _entry in (str(_REPO_ROOT / "app"), str(_REPO_ROOT), str(_SCRIPTS)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from shockwave_demo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
