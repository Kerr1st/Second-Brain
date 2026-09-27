#!/bin/bash
# Shared job boundary: environment, cwd, venv, lock, timeout, logs and JSON status.
# No set -e: failures must reach the runner's status/notification handling first.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$REPO_ROOT" || exit 2
PYTHON="${SECOND_BRAIN_JOB_PYTHON:-${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}}"
exec "$PYTHON" -m src.operations.jobs "$@"
