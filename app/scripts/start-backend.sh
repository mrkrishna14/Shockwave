#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export SHOCKWAVE_DEMO=1
export SHOCKWAVE_INLINE_JOBS=${SHOCKWAVE_INLINE_JOBS:-0}
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
