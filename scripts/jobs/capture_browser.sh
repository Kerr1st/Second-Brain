#!/bin/bash
# Command adapter; launchd invokes this through job_wrapper.sh.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2
exec "${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}" -m src.capture.browser "$@"
