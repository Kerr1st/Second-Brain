#!/bin/bash
# Capture API launcher (launchd: com.second-brain.capture-api).
#
# Secrets are NOT in the plist (it is tracked in git). They live in the
# gitignored, 0600 file `.env` at the repo root and are exported here. The
# server itself refuses to start without CAPTURE_API_TOKEN, so a missing or
# incomplete .env fails loudly in ~/Library/Logs/second-brain/ instead of
# silently serving an unauthenticated endpoint.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f .env ]; then
  echo "capture_api.sh: .env not found in $(pwd) — create it with CAPTURE_API_TOKEN=<64 hex> (chmod 600)" >&2
  exit 78  # EX_CONFIG
fi
set -a; . ./.env; set +a
if [ -z "${CAPTURE_API_TOKEN:-}" ]; then
  echo "capture_api.sh: CAPTURE_API_TOKEN is not set in .env" >&2
  exit 78
fi

exec "${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}" -m src.capture_api
