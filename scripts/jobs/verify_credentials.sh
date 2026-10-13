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
# A TOOL-MISSING result is also non-fatal: it means the *check* could not run, which
# is a monitoring defect rather than evidence of lost capture, and verify_liveness.sh
# (which measures the database, not the tooling) is the backstop for actual data loss.
# Silence from a monitor is the thing we are trying to avoid, so it is printed loudly
# as ⚠ with a fix hint instead of being folded into the failure list.
#
# 2026-10-13 — WHY A FAILED CHECK NOW LOGS A REASON. Probes used to run as
# `>/dev/null 2>&1`, so the 10-11/10-12 Graph + Slack lapses logged only "(exit 1)":
# an expired token, a hung server and a bug looked identical. Each probe's combined
# output now goes to a private temp file (mode 600, removed on exit) and a failure
# adds ONE line under the ✅/❌/⚠ line (whose format is unchanged):
#     reason: <auth|timeout|transport|tool-missing|other>: <last meaningful line>
# The class comes from the exit code first (timeout's 124/137; 3/4 from the MCP
# probes below, which mirror src.capture.mcp_client.classify_error / KIND_EXIT),
# then from keywords. The message is redacted (Bearer/Basic, key=value secrets,
# cookies, AWS key ids, 21+ char base64/hex runs) and capped at 160 characters;
# full probe output is never printed.
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
ADVISORY=""

# One private scratch file for probe output, reused (truncated) by every check.
# The TERM/INT/HUP traps turn a signal into an exit so the EXIT trap still cleans up.
PROBE_OUT="$(umask 077 && mktemp "${TMPDIR:-/tmp}/sb-credentials.XXXXXX" 2>/dev/null)" || PROBE_OUT=""
if [ -n "$PROBE_OUT" ]; then
  chmod 600 "$PROBE_OUT"
  trap 'rm -f "$PROBE_OUT"' EXIT
  trap 'exit 143' TERM; trap 'exit 130' INT; trap 'exit 129' HUP
else
  log "⚠ mktemp failed — probe output will not be captured (reasons unavailable)"
fi

# classify <rc> <output-file> → auth | timeout | transport | tool-missing | other
# Exit code first; keywords only for a generic failure. Auth wins over transport,
# as in src.capture.mcp_client.classify_error (a stale token often *looks* hung).
classify() {
  case "$1" in
    124|137) echo timeout; return ;;   # coreutils timeout: TERM sent / KILL sent
    127) echo tool-missing; return ;;
    3) echo auth; return ;;            # EXIT_AUTH from the MCP probes
    4) echo transport; return ;;       # EXIT_TRANSPORT from the MCP probes
  esac
  local f="$2"
  [ -n "$f" ] && [ -s "$f" ] || { echo other; return; }
  if grep -qiE 'expired|will expire|authenticat|unauthori[sz]ed|auth_error|invalid_grant|mwinit|re-?auth|not authenticated|(invalid|expired|missing|refresh)[^[:alnum:]]{0,3}[a-z_ ]{0,15}token|token[_ -]?(invalid|expired|refresh)|unable to locate credentials|InvalidClientTokenId|SignatureDoesNotMatch|AccessDenied|credential_process' "$f"; then
    echo auth
  elif grep -qiE 'FileNotFoundError|No such file or directory|command not found' "$f"; then
    echo tool-missing   # e.g. the MCP server binary itself is gone; still ❌, the capture needs it
  elif grep -qiE 'timed out|timeout' "$f"; then
    echo timeout
  elif grep -qiE 'connection (closed|reset|refused)|broken pipe|could not connect|endpoint url|name resolution|nodename nor servname|network is unreachable|EndOfStream|ClosedResource|BrokenResource|process exited' "$f"; then
    echo transport
  else
    echo other
  fi
}

# sanitize <output-file> → the last meaningful line, redacted, ≤160 characters.
# Needs perl (stock on macOS). Without it nothing is printed rather than risk a secret.
sanitize() {
  local f="$1"
  [ -n "$f" ] && [ -s "$f" ] || { echo "(no output)"; return; }
  command -v perl >/dev/null 2>&1 || { echo "(message withheld: perl not found to redact it)"; return; }
  perl -CSD -e '
    my $last = "";
    while (my $l = <STDIN>) {
      $l =~ s/\e\[[0-9;?]*[A-Za-z]//g;           # ANSI colour codes
      $l =~ s/[\x00-\x1f\x7f]+/ /g;               # control chars, incl. CR/LF
      $l =~ s/^\s+|\s+$//g;
      next if $l eq "" or $l =~ /^(Traceback \(most recent call last\)|File "|During handling of|The above exception)/;
      next if $l =~ /^[\^~\s]+$/;                 # traceback caret markers
      $last = $l;
    }
    $last = "(no output)" if $last eq "";
    my $run = q{A-Za-z0-9+/_=%.!*-};              # incl. URL-encoding and token punctuation
    $last =~ s/\b(bearer|basic)\s+\S+/$1 [REDACTED]/gi;
    $last =~ s/\b((?:set-)?cookie\s*[:=]\s*).*/$1\[REDACTED]/gi;
    $last =~ s/\bxox[a-z]-\S+/[REDACTED]/gi;       # Slack tokens/cookies (xoxb/xoxp/xoxc/xoxd, URL-encoded too)
    $last =~ s/eyJ[\w-]+\.[\w-]+\.[\w-]*/[REDACTED]/g;   # JWTs, whatever their segment lengths
    # key=value, key: "value" and Python-repr key: 'value' (\x27 is a single quote:
    # this program sits inside a single-quoted shell string).
    $last =~ s/\b((?:[a-z0-9]+[_-])*(?:token|secret|password|passwd|pwd|apikey|api[_-]key|signature|sig|session|cookie|credentials?)s?["\x27]?\s*[:=]\s*["\x27]?)[^\s"&;,]+/$1\[REDACTED]/gi;
    $last =~ s/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/[REDACTED]/g;
    $last =~ s{(?<![$run])(?=[$run]*[0-9])[$run]{21,}}{[REDACTED]}g;   # base64/hex runs
    $last = substr($last, 0, 160);
    print "$last\n";
  ' <"$f" 2>/dev/null || echo "(message unavailable)"
}

reason() {  # rc
  log "   reason: $(classify "$1" "$PROBE_OUT"): $(sanitize "$PROBE_OUT")"
}

# check <required|advisory> <label> <command...>
# Three outcomes: OK / EXPIRED-or-BROKEN / TOOL-MISSING. Only a REQUIRED credential
# that answered and answered badly is a failure.
check() {
  local sev="$1" label="$2"; shift 2
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    log "⚠ $label tool not found on PATH ($bin) — check NOT RUN (not an expired credential)"
    ADVISORY="$ADVISORY ${label}[tool-missing]"
    return 0
  fi
  local rc=0
  if [ -n "$PROBE_OUT" ]; then
    : >"$PROBE_OUT"
    $TIMEOUT "$@" >"$PROBE_OUT" 2>&1 || rc=$?
  else
    $TIMEOUT "$@" >/dev/null 2>&1 || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    log "✅ $label"
  elif [ "$rc" -eq 127 ]; then
    log "⚠ $label tool not found (exit 127) — check NOT RUN (not an expired credential)"
    reason "$rc"
    ADVISORY="$ADVISORY ${label}[tool-missing]"
  elif [ "$sev" = required ]; then
    log "❌ $label EXPIRED/BROKEN (exit $rc)"
    reason "$rc"
    REQUIRED_FAILED="$REQUIRED_FAILED $label"
  else
    log "⚠ $label expired (exit $rc) — advisory: interactive session, no job uses it"
    reason "$rc"
    ADVISORY="$ADVISORY ${label}[expired]"
  fi
  [ -n "$PROBE_OUT" ] && : >"$PROBE_OUT"   # do not keep probe output around between checks
  return 0
}

hints() {  # label list
  case "$1" in *sso-default*) log "   fix: aws sso login --sso-session kiro-admin-session";; esac
  case "$1" in *second-brain-jobs*) log "   fix: IAM Roles Anywhere profile — run the credential_process line from ~/.aws/config by hand to see the error (cert expired? see docs/OPERATIONS.md 'Credentials')";; esac
  case "$1" in *roles-anywhere*) log "   fix: re-issue ~/.aws/rolesanywhere/jobs.pem from the CA before it expires (docs/OPERATIONS.md 'Credentials')";; esac
  case "$1" in *grasp*|*slack*) log "   fix: open Kiro and use the Outlook/Slack tools once to re-authenticate";; esac
  case "$1" in *tool-missing*) log "   fix: launchd runs this job with PATH=/usr/bin:/bin:/usr/sbin:/sbin — add the tool's directory to the PATH line at the top of this script";; esac
}

check required "aws:second-brain-jobs" "$AWS_BIN" sts get-caller-identity --profile second-brain-jobs
# second-brain-jobs authenticates with IAM Roles Anywhere (2026-10-10): a client
# certificate signed by a local CA. Warn 30 days before the certificate expires;
# once expired the sts check above fails too.
RA_CERT="${SECOND_BRAIN_RA_CERT:-$HOME/.aws/rolesanywhere/jobs.pem}"
if [ -f "$RA_CERT" ]; then
  check advisory "cert:roles-anywhere(30d)" /usr/bin/openssl x509 -checkend 2592000 -noout -in "$RA_CERT"
fi
check advisory "aws:sso-default"       "$AWS_BIN" sts get-caller-identity --profile default
check required "graph:grasp-mcp" "$PYTHON_BIN" - <<'EOF'
import asyncio, sys
def _leaf(e):
    # anyio wraps transport failures in ExceptionGroups; report the first real one.
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return e
try:
    from src.capture.mcp_client import McpClient, McpAuthError, auth_error_in_payload, classify_error, KIND_EXIT
    from src.capture.outlook import GRASP_CMD, GRASP_ARGS
    async def main():
        async with McpClient(GRASP_CMD, GRASP_ARGS, call_timeout=60) as c:
            # A successful mailbox listing proves the Graph token is valid (any format).
            d = await c.call_json("get_emails", {"folder": "inbox", "maxResults": 1})
            auth = auth_error_in_payload(d)
            if auth:
                raise McpAuthError(f"get_emails: {auth}")
            # Name the keys, never the payload: it is someone's mailbox.
            assert isinstance(d, dict) and "emails" in d, \
                f"get_emails: unexpected reply (keys: {sorted(d)[:5] if isinstance(d, dict) else type(d).__name__})"
    asyncio.run(main())
except Exception as e:  # one classified line on stderr + the capture jobs' exit code
    leaf = _leaf(e)
    kind = classify_error(e) if "classify_error" in globals() else "error"
    print(f"{type(leaf).__name__}: {' '.join(str(leaf).split())}", file=sys.stderr)  # sanitize redacts, then caps
    sys.exit(KIND_EXIT.get(kind, 1) if "KIND_EXIT" in globals() else 1)
EOF
check required "slack:slack-mcp" "$PYTHON_BIN" - <<'EOF'
import asyncio, sys
def _leaf(e):
    # anyio wraps transport failures in ExceptionGroups; report the first real one.
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return e
try:
    from src.capture.mcp_client import McpClient, McpAuthError, auth_error_in_payload, classify_error, KIND_EXIT
    from src.capture.slack import SLACK_CMD
    async def main():
        async with McpClient(SLACK_CMD, [], call_timeout=60) as c:
            d = await c.call_json("list_dms", {"limit": 1})
            auth = auth_error_in_payload(d)
            if auth:
                raise McpAuthError(f"list_dms: {auth}")
            assert isinstance(d, dict) and "dms" in d, \
                f"list_dms: unexpected reply (keys: {sorted(d)[:5] if isinstance(d, dict) else type(d).__name__})"
    asyncio.run(main())
except Exception as e:  # one classified line on stderr + the capture jobs' exit code
    leaf = _leaf(e)
    kind = classify_error(e) if "classify_error" in globals() else "error"
    print(f"{type(leaf).__name__}: {' '.join(str(leaf).split())}", file=sys.stderr)  # sanitize redacts, then caps
    sys.exit(KIND_EXIT.get(kind, 1) if "KIND_EXIT" in globals() else 1)
EOF

if [ -n "$ADVISORY" ]; then
  log "=== advisory, NOT failing the job:$ADVISORY ==="
  hints "$ADVISORY"
fi

if [ -n "$REQUIRED_FAILED" ]; then
  log "=== REQUIRED EXPIRED/BROKEN:$REQUIRED_FAILED ==="
  hints "$REQUIRED_FAILED"
  notify "Expired:$REQUIRED_FAILED — see logs/credentials-$(date +%Y%m%d).log"
  exit 1
fi
log "=== all required credentials valid ==="
