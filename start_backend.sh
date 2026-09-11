#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python -c "import langgraph" >/dev/null 2>&1 || .venv/bin/python -m pip install "langgraph>=0.2,<2"
exec .venv/bin/python scripts/start_backend.py "$@"
