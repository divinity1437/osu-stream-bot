#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./venv/bin/python -m compileall -q app.py config.py queue_store.py replay_source.py ordr_client.py discord_ingest.py submission_store.py danser_renderer.py
exec ./venv/bin/python app.py --check
