#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export SHOCKWAVE_DEMO=1
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
