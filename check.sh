#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./venv/bin/python -m compileall -q app.py discord_ingest.py submission_store.py danser_renderer.py
exec ./venv/bin/python app.py --check
