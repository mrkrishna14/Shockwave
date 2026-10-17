#!/bin/sh
# Start the Shockwave platform website locally: backend (port 8000) + website (port 3000).
#
#   ~/Desktop/Shockwave-main/start-website.sh            # integrated mode: the owners' real modules
#   SHOCKWAVE_ADAPTERS=fixture ./start-website.sh        # Ishan's embedded fixture mode
#
# Then open http://127.0.0.1:3000. Press Ctrl+C to stop both servers.
# Local only: the demo persona switcher and fixture execution must never be exposed publicly.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv/bin"
if [ ! -x "$VENV/python" ] || [ ! -x "$VENV/node" ]; then
  echo "Missing $ROOT/.venv (Python + Node). Create it with:" >&2
  echo "  cd \"$ROOT\" && uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
  exit 1
fi
# Node from .venv first: the system Node may be too old or built for another CPU.
PATH="$VENV:$PATH"
export PATH

if [ ! -d "$ROOT/app/frontend/node_modules" ]; then
  echo "Installing website dependencies (one time; needs network)..."
  npm ci --prefix "$ROOT/app/frontend" --no-audit --no-fund
fi

export SHOCKWAVE_DEMO=1
export SHOCKWAVE_ADAPTERS="${SHOCKWAVE_ADAPTERS:-integrated}"
if [ "$SHOCKWAVE_ADAPTERS" = integrated ] && [ ! -f "$ROOT/demo/templates/checkout/node_modules/stripe/package.json" ]; then
  echo "Integrated Stripe checkout dependencies are missing. Install them with:" >&2
  echo "  cd \"$ROOT\" && PATH=\"$VENV:\$PATH\" npm ci --ignore-scripts --prefix demo/templates/checkout" >&2
  exit 1
fi
export SHOCKWAVE_INLINE_JOBS=1            # keep integrated state in the API process
export SHOCKWAVE_DB="${SHOCKWAVE_DB:-$ROOT/app/shockwave.db}"
export SHOCKWAVE_WORK_DIR="${SHOCKWAVE_WORK_DIR:-$ROOT/app/.shockwave-integrated}"

cd "$ROOT/app"
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8000 is already in use. Stop the existing backend before starting the website." >&2
  echo "An older backend may be missing routes required by this dashboard; it cannot be reused safely." >&2
  lsof -nP -iTCP:8000 -sTCP:LISTEN >&2 || true
  exit 1
fi
"$VENV/python" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
BACKEND=$!
trap 'kill $BACKEND 2>/dev/null || true' EXIT INT TERM
if lsof -nP -iTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 3000 is already in use: the website may already be running at http://127.0.0.1:3000" >&2
  exit 1
fi

echo
echo "Backend: http://127.0.0.1:8000 (mode: $SHOCKWAVE_ADAPTERS)"
echo "Website: http://127.0.0.1:3000  <- open this in your browser"
echo "Press Ctrl+C to stop."
echo
cd "$ROOT/app/frontend"
npm run dev
