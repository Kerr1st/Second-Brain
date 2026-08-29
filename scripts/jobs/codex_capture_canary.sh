#!/bin/bash
# Run the one-task Codex operational canary.

set -euo pipefail

TASK_ID="${CODEX_CAPTURE_CANARY_TASK_ID:-}"
if [[ -z "$TASK_ID" ]]; then
  echo "CODEX_CAPTURE_CANARY_TASK_ID is required" >&2
  exit 2
fi
if [[ ! "$TASK_ID" =~ ^[0-9a-fA-F-]{36}$ ]]; then
  echo "CODEX_CAPTURE_CANARY_TASK_ID must be a UUID" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Second Brain Python is not executable: $PYTHON" >&2
  exit 2
fi

export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-codex_local}"
export CODEX_CLI="${CODEX_CLI:-/Applications/ChatGPT.app/Contents/Resources/codex}"

if ! "$PYTHON" -c \
  'import boto3, sys; sys.exit(0 if boto3.Session().get_credentials() else 75)'
then
  printf '{"status":"waiting_for_embedding_credentials","task_id":"%s"}\n' "$TASK_ID"
  exit 0
fi

cd "$REPO_ROOT"
exec "$PYTHON" "$REPO_ROOT/scripts/capture_codex.py" --task-id "$TASK_ID"
