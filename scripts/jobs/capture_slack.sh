#!/bin/bash
# Nightly Slack capture (channels + DMs, one document per conversation-day) via
# slack-mcp. Captures the two most recent *completed* local days; dedup makes
# re-runs idempotent.
set -uo pipefail
# slack-mcp is a wrapper that execs `aim` from the toolbox; launchd's PATH lacks it.
export PATH="$HOME/.toolbox/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."          # repo root, so `-m src.capture.slack` resolves
exec .venv/bin/python -m src.capture.slack "$@"
