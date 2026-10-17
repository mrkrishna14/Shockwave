#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export SHOCKWAVE_DEMO=1
exec .venv/bin/python scripts/worker.py
