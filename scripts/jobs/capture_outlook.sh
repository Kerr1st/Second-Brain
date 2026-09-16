#!/bin/bash
# Nightly Outlook capture (mail + calendar) via grasp-mcp. Rolling 2-day window;
# ingest dedups by source_url so re-runs are idempotent.
set -uo pipefail
cd "$(dirname "$0")/../.."          # repo root, so `-m src.capture.outlook` resolves
exec .venv/bin/python -m src.capture.outlook "$@"
