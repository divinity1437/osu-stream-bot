#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# If RUN_MODE=render-worker, launch render_worker.py, otherwise launch main app.py
RUN_MODE_VAL="${RUN_MODE:-}"
if [[ -z "$RUN_MODE_VAL" && -f .env ]]; then
  RUN_MODE_VAL="$(grep -E '^RUN_MODE=' .env | cut -d= -f2- | tr -d ' "\r' || true)"
fi

if [[ "${RUN_MODE_VAL,,}" == "render-worker" ]]; then
  exec ./venv/bin/python render_worker.py "$@"
else
  exec ./venv/bin/python app.py "$@"
fi
