#!/bin/bash
# Weekly digest job (called by job_wrapper.sh from com.second-brain.weekly-digest).
#
# Added 2026-09-23 when the digest moved off Kiro. The plist used to point
# job_wrapper.sh straight at scripts/weekly_digest.py, which built a KiroInvoker
# itself; the job now resolves its model through the backend profile, so it needs
# the same environment the other metered jobs set (compare
# scripts/jobs/dream_cycle_scheduled.sh and scripts/jobs/codex_capture.sh).
#
#   SECOND_BRAIN_PROFILE=controlled   the only profile whose every seat is
#     Bedrock-in-account. `laptop`/`hybrid` route to Kiro and `bedrock` routes the
#     Thinker to Fable 5.1 ("NOT FOR CUSTOMER DATA, ITAR OR PII"); this job
#     summarises the user's own corpus, so neither is allowed
#     (docs/PLAN-panel-acceptance.md §3). The digest borrows the `skim` role,
#     which the resolver already pins to direct Bedrock in every profile, and
#     scripts/weekly_digest.py refuses an MCP-capable backend outright.
#   SECOND_BRAIN_DIGEST_CAP_USD       the digest's own spend cap (default $0.25 in
#     the script). One Nova 2 Lite call is ~$0.002, so this only ever trips on a
#     runaway or a DIGEST_MODEL override onto an unpriced model.
#   AWS_PROFILE=second-brain-jobs     least-privilege IAM user (Bedrock
#     InvokeModel: the digest call + Titan embeddings), not the 8h SSO session.
#
# Exits non-zero on failure (backend refusal 2, spend cap 3, timeout 4) so
# job_wrapper.sh notifies and com.second-brain.liveness can see it.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Second Brain Python is not executable: $PYTHON" >&2
  exit 2
fi

export PATH="$HOME/.toolbox/bin:/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"
export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"
export SECOND_BRAIN_DIGEST_CAP_USD="${SECOND_BRAIN_DIGEST_CAP_USD:-0.25}"
export BEDROCK_REGION="${BEDROCK_REGION:-us-east-1}"

cd "$REPO_ROOT"

# PostgreSQL runs natively (Homebrew postgresql@17). Not auto-started here any more
# (R5): see scripts/jobs/_preflight.sh for why; exit 69 classifies "database down".
# shellcheck source=scripts/jobs/_preflight.sh
. "$REPO_ROOT/scripts/jobs/_preflight.sh"
require_postgres

echo "[$(date '+%Y-%m-%d %H:%M:%S')] weekly-digest starting (profile=$SECOND_BRAIN_PROFILE, cap=\$$SECOND_BRAIN_DIGEST_CAP_USD)"
exec "$PYTHON" "$REPO_ROOT/scripts/weekly_digest.py" "$@"
