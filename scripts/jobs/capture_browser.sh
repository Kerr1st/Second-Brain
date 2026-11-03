#!/bin/bash
# Nightly browser history + reading capture. Daily browsing-log documents from
# Chrome's History DB; pages actually read are sent to the Crawlee batch scraper
# (needs node on PATH), and the 03:00 ingest job lands their full text.
set -uo pipefail
export PATH="/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."          # repo root, so `-m src.capture.browser` resolves
exec .venv/bin/python -m src.capture.browser "$@"
