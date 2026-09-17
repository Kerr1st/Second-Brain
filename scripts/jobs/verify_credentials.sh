#!/bin/bash
# Morning credential probe. Every collector depends on one of four credential sets;
# when one lapses the pipeline goes quiet for days before liveness notices. This
# runs at 07:00 and raises a macOS notification the same morning.
#
#   second-brain-jobs  IAM user (Bedrock InvokeModel only) — embeddings + Astra
#   default            interactive SSO (AdministratorAccess) — only for you, not jobs
#   grasp-mcp          Microsoft Graph token — Outlook mail/calendar
#   slack-mcp          Midway session — Slack
set -uo pipefail
export PATH="$HOME/.toolbox/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."
log() { echo "[$(date '+%H:%M:%S')] $1"; }
notify() { osascript -e "display notification \"$1\" with title \"Second Brain — credentials\" sound name \"Basso\"" 2>/dev/null || true; }

FAILED=""
check() {  # label, command...
  local label="$1"; shift
  if timeout 90 "$@" >/dev/null 2>&1; then log "✅ $label"; else log "❌ $label"; FAILED="$FAILED $label"; fi
}

check "aws:second-brain-jobs" aws sts get-caller-identity --profile second-brain-jobs
check "aws:sso-default"       aws sts get-caller-identity --profile default
check "graph:grasp-mcp" .venv/bin/python - <<'EOF'
import asyncio
from src.capture.mcp_client import McpClient
from src.capture.outlook import GRASP_CMD, GRASP_ARGS
async def main():
    async with McpClient(GRASP_CMD, GRASP_ARGS, call_timeout=60) as c:
        # A successful mailbox listing proves the Graph token is valid (any format).
        d = await c.call_json("get_emails", {"folder": "inbox", "maxResults": 1})
        assert isinstance(d, dict) and "emails" in d, d
asyncio.run(main())
EOF
check "slack:slack-mcp" .venv/bin/python - <<'EOF'
import asyncio
from src.capture.mcp_client import McpClient
from src.capture.slack import SLACK_CMD
async def main():
    async with McpClient(SLACK_CMD, [], call_timeout=60) as c:
        d = await c.call_json("list_dms", {"limit": 1})
        assert isinstance(d, dict) and "dms" in d, d
asyncio.run(main())
EOF

if [ -n "$FAILED" ]; then
  log "=== EXPIRED/BROKEN:$FAILED ==="
  case "$FAILED" in
    *sso-default*) log "   fix: aws sso login --sso-session kiro-admin-session";;
  esac
  case "$FAILED" in *grasp*|*slack*) log "   fix: open Kiro and use the Outlook/Slack tools once to re-authenticate";; esac
  notify "Expired:$FAILED — see logs/credentials-$(date +%Y%m%d).log"
  exit 1
fi
log "=== all credentials valid ==="
