#!/bin/bash
# Shared job boundary for every launchd job: environment, cwd, venv, per-job flock,
# deadline (SIGTERM → grace → SIGKILL), caffeinate, per-day log and durable JSON
# status under logs/jobs/<job>.json.  Usage (from the plists):
#   job_wrapper.sh <script_path> <job_name> [args…]
# All policy lives in src/operations/jobs.py; this file only finds the interpreter.
# No set -e: failures must reach the runner's status/notification handling first.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$REPO_ROOT" || exit 2
PYTHON="${SECOND_BRAIN_JOB_PYTHON:-${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}}"
if [[ ! -x "$PYTHON" ]]; then
  echo "job_wrapper.sh: interpreter not executable: $PYTHON" >&2
  exit 78  # EX_CONFIG
fi
exec "$PYTHON" -m src.operations.jobs "$@"
