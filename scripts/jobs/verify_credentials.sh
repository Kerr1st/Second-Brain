#!/bin/bash
# Morning credential probe. Every collector depends on one of four credential sets;
# when one lapses the pipeline goes quiet for days before liveness notices. This
# runs at 07:00 and raises a macOS notification the same morning.
#
#   second-brain-jobs  IAM user (Bedrock InvokeModel only) — embeddings + Astra   REQUIRED
#   grasp-mcp          Microsoft Graph token — Outlook mail/calendar              REQUIRED
#   slack-mcp          Midway session — Slack                                     REQUIRED
#   default            interactive SSO (AdministratorAccess), budgets — you, not jobs   ADVISORY
#
# 2026-09-23 — WHY THIS SCRIPT WAS REWRITTEN (alert fatigue was the actual outage).
# This job exited 1 every single night for weeks, and *because* it always failed
# nobody looked at it — so when Slack (Midway) and Outlook (grasp transport) really
# did break, they stayed broken for days. Two root causes, both fixed here:
#
#  1. PATH, not credentials. launchd gives a job PATH=/usr/bin:/bin:/usr/sbin:/sbin.
#     The AWS CLI on this machine is /usr/local/bin/aws (there is no aws in
#     /opt/homebrew/bin), and neither this script's old PATH line nor job_wrapper.sh
#     included /usr/local/bin. So `aws` exited 127 (command not found) and the script
#     reported "EXPIRED/BROKEN" — a missing tool was indistinguishable from a lapsed
#     credential. Verified: under `env -i HOME=$HOME PATH=/usr/bin:/bin`, `aws sts
#     get-caller-identity --profile second-brain-jobs` exits 127; with /usr/local/bin
#     on PATH the same call exits 0. Hence: /usr/local/bin is on the PATH below, and
#     exit 127 / an unresolvable binary is now its own third outcome (TOOL-MISSING).
#  2. Severity. `aws:sso-default` is an *interactive* AdministratorAccess SSO session
#     that lapses by design between logins (8 h) and is used for budget queries by a
#     human — never by the unattended pipeline. Failing the nightly job on it
#     guarantees a permanently red monitor. It is ADVISORY now: reported, never fatal.
#
# TOOL-MISSING is distinct from an expired credential. A missing REQUIRED check
# returns 2 (unknown health); a performed required check that fails returns 1.
# Interactive SSO remains advisory. Required health is never reported as valid
# when its probe could not run.
#
# Debugging aid born from cause (1): `verify_credentials.sh --print-tools` prints the
# effective PATH and where each binary resolves. Run it the way launchd does
#   env -i HOME=$HOME PATH=/usr/bin:/bin /bin/bash scripts/jobs/verify_credentials.sh --print-tools
# to see what the job can actually find, rather than what your shell can find.
set -uo pipefail
# /usr/local/bin is mandatory: that is where the AWS CLI lives (see note 1 above).
export PATH="$HOME/.toolbox/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")/../.."

AWS_BIN="${AWS_BIN:-aws}"
PYTHON_BIN="${SECOND_BRAIN_PYTHON:-.venv/bin/python}"

log() { echo "[$(date '+%H:%M:%S')] $1"; }
notify() { osascript -e "display notification \"$1\" with title \"Second Brain — credentials\" sound name \"Basso\"" 2>/dev/null || true; }
resolve() { command -v "$1" 2>/dev/null || echo MISSING; }

if [ "${1:-}" = "--print-tools" ]; then
  echo "PATH=$PATH"
  echo "aws=$(resolve "$AWS_BIN")"
  echo "python=$(resolve "$PYTHON_BIN")"
  echo "timeout=$(resolve timeout)"
  exit 0
fi

# `timeout` is Homebrew coreutils, not a macOS builtin. If it is absent every check
# would exit 127 and be misread as broken, so degrade to running unbounded instead.
if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 90"; else
  TIMEOUT=""
  log "⚠ timeout not found on PATH — running checks unbounded"
fi

REQUIRED_FAILED=""
REQUIRED_UNKNOWN=""
ADVISORY=""

# check <required|advisory> <label> <command...>
# Three outcomes: OK / EXPIRED-or-BROKEN / TOOL-MISSING. Unavailable required
# probes fail distinctly rather than inventing a valid or expired credential.
check() {
  local sev="$1" label="$2"; shift 2
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    log "⚠ $label tool not found on PATH ($bin) — check NOT RUN (not an expired credential)"
    if [ "$sev" = required ]; then REQUIRED_UNKNOWN="$REQUIRED_UNKNOWN ${label}[tool-missing]";
    else ADVISORY="$ADVISORY ${label}[tool-missing]"; fi
    return 0
  fi
  local rc=0
  $TIMEOUT "$@" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    log "✅ $label"
  elif [ "$rc" -eq 127 ]; then
    log "⚠ $label tool not found (exit 127) — check NOT RUN (not an expired credential)"
    if [ "$sev" = required ]; then REQUIRED_UNKNOWN="$REQUIRED_UNKNOWN ${label}[tool-missing]";
    else ADVISORY="$ADVISORY ${label}[tool-missing]"; fi
  elif [ "$sev" = required ]; then
    log "❌ $label EXPIRED/BROKEN (exit $rc)"
    REQUIRED_FAILED="$REQUIRED_FAILED $label"
  else
    log "⚠ $label expired (exit $rc) — advisory: interactive session, no job uses it"
    ADVISORY="$ADVISORY ${label}[expired]"
  fi
}

hints() {  # label list
  case "$1" in *sso-default*) log "   fix: aws sso login --sso-session kiro-admin-session";; esac
  case "$1" in *second-brain-jobs*) log "   fix: check the second-brain-jobs profile in ~/.aws/credentials (IAM user, no session to refresh)";; esac
  case "$1" in *grasp*|*slack*) log "   fix: open Kiro and use the Outlook/Slack tools once to re-authenticate";; esac
  case "$1" in *tool-missing*) log "   fix: launchd runs this job with PATH=/usr/bin:/bin:/usr/sbin:/sbin — add the tool's directory to the PATH line at the top of this script";; esac
}

check required "aws:second-brain-jobs" "$AWS_BIN" sts get-caller-identity --profile second-brain-jobs
check advisory "aws:sso-default"       "$AWS_BIN" sts get-caller-identity --profile default
check required "graph:grasp-mcp" "$PYTHON_BIN" - <<'EOF'
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
check required "slack:slack-mcp" "$PYTHON_BIN" - <<'EOF'
import asyncio
from src.capture.mcp_client import McpClient
from src.capture.slack import SLACK_CMD
async def main():
    async with McpClient(SLACK_CMD, [], call_timeout=60) as c:
        d = await c.call_json("list_dms", {"limit": 1})
        assert isinstance(d, dict) and "dms" in d, d
asyncio.run(main())
EOF

# File mtime does not reveal an IAM key's creation or rotation date. This local
# metadata check reads only configuration presence; it never sends or prints keys.
if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  "$PYTHON_BIN" - <<'PYTHON_METADATA'
import configparser, os
from pathlib import Path
path = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", str(Path.home()/".aws/credentials")))
profile = "second-brain-jobs"
try:
    config = configparser.RawConfigParser()
    config.read(path)
    static = config.has_option(profile, "aws_access_key_id") and not config.has_option(profile, "aws_session_token")
    if static:
        print("WARNING aws:second-brain-jobs STATIC KEY AGE UNKNOWN: local file mtime does not establish IAM creation or rotation; inspect the identified key before retirement")
except (OSError, configparser.Error):
    print("WARNING credential metadata could not be inspected; IAM key age remains unknown")
PYTHON_METADATA
fi

if [ -n "$ADVISORY" ]; then
  log "=== advisory, NOT failing the job:$ADVISORY ==="
  hints "$ADVISORY"
fi

if [ -n "$REQUIRED_UNKNOWN" ]; then
  log "=== REQUIRED CHECK UNKNOWN:$REQUIRED_UNKNOWN ==="
  hints "$REQUIRED_UNKNOWN"
fi

if [ -n "$REQUIRED_FAILED" ]; then
  log "=== REQUIRED EXPIRED/BROKEN:$REQUIRED_FAILED ==="
  hints "$REQUIRED_FAILED"
  notify "Expired:$REQUIRED_FAILED — see logs/credentials-$(date +%Y%m%d).log"
  exit 1
fi
if [ -n "$REQUIRED_UNKNOWN" ]; then
  notify "Required credential check unavailable:$REQUIRED_UNKNOWN"
  exit 2
fi
log "=== all required credentials valid ==="
