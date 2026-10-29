#!/bin/bash
# Nightly Outlook capture (mail + calendar) via grasp-mcp. Rolling 2-day window;
# ingest dedups by source_url so re-runs are idempotent.
#
# Exit-code taxonomy (see src/capture/mcp_client.py) — the last line of this log
# is what a human greps, so it names the class of failure and the fix:
#   0  OK
#   3  auth      grasp-mcp login lapsed — run `mwinit`, then `grasp-mcp login`, re-run
#   4  transport grasp-mcp died mid-session and did not come back after one
#                reconnect (the 2026-09-20/21 "Connection closed" failure)
#   1  anything else (bug, DB, bad data)
set -uo pipefail
# grasp-mcp lives in the toolbox; launchd's PATH lacks it.
export PATH="$HOME/.toolbox/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."          # repo root, so `-m src.capture.outlook` resolves

.venv/bin/python -m src.capture.outlook "$@"
RC=$?

ts() { date '+%H:%M:%S'; }
case $RC in
  0) echo "[$(ts)] outlook: OK" ;;
  3) echo "[$(ts)] outlook: FAILED (auth — run mwinit, then grasp-mcp login, then re-run)" ;;
  4) echo "[$(ts)] outlook: FAILED (transport — grasp-mcp not answering)" ;;
  *) echo "[$(ts)] outlook: FAILED (error — exit $RC)" ;;
esac
exit $RC
