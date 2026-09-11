#!/usr/bin/env bash
set -euo pipefail
PORT="${HELPDESK_PORT:-8787}"
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti:"$PORT" || true)
  [[ -z "$PIDS" ]] || kill $PIDS
fi
echo "Helpdesk service on port $PORT stopped."
