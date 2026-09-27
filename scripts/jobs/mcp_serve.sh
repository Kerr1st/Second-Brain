#!/bin/bash
# Trusted interactive MCP launcher; discovery uses the separate read-only registry.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 1
export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"
export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"
exec "$ROOT/.venv/bin/python" -m src.mcp_server
