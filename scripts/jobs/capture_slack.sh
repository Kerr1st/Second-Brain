#!/bin/bash
# Nightly Slack capture (channels + DMs, one document per conversation-day) via
# slack-mcp. Captures the two most recent *completed* local days; dedup makes
# re-runs idempotent.
#
# Exit-code taxonomy (see src/capture/mcp_client.py) — the last line of this log
# is what a human greps, so it names the class of failure and the fix:
#   0  OK
#   3  auth      stale Midway cookie — run `mwinit` (pre-flight probe caught it,
#                no capture was attempted, so nothing is half-written)
#   4  transport slack-mcp died or stopped answering — retry next run
#   1  anything else (bug, DB, bad data)
set -uo pipefail
# slack-mcp is a wrapper that execs `aim` from the toolbox; launchd's PATH lacks it.
export PATH="$HOME/.toolbox/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."          # repo root, so `-m src.capture.slack` resolves

.venv/bin/python -m src.capture.slack "$@"
RC=$?

ts() { date '+%H:%M:%S'; }
case $RC in
  0) echo "[$(ts)] slack: OK" ;;
  3) echo "[$(ts)] slack: FAILED (auth — run mwinit)" ;;
  4) echo "[$(ts)] slack: FAILED (transport — slack-mcp not answering)" ;;
  *) echo "[$(ts)] slack: FAILED (error — exit $RC)" ;;
esac
exit $RC
