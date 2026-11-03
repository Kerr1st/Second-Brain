#!/bin/bash
# Command adapter; launchd invokes this through job_wrapper.sh.
# Stages (src.capture.qd): facts, tags, feed, chats. (The Slack knowledge-graph
# writer that used to run between feed and chats was retired in R7, migration 018.)
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2
exec "${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}" -m src.capture.qd "$@"
