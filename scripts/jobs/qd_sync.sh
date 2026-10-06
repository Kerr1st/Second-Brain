#!/bin/bash
# Command adapter; launchd invokes this through job_wrapper.sh.
# Stages (src.capture.qd): facts, tags, feed, graph, chats — "graph" is the Slack
# knowledge-graph writer, kept until R7 (user decision 2026-10-06).
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2
exec "${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}" -m src.capture.qd "$@"
