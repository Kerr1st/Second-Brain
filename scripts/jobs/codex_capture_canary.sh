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
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-ollama}"
export OLLAMA_EMBEDDING_MODEL="${OLLAMA_EMBEDDING_MODEL:-bge-m3}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434}"

if ! "$PYTHON" -c \
  'import os, requests, sys
base = os.environ["OLLAMA_BASE_URL"].rstrip("/")
model = os.environ["OLLAMA_EMBEDDING_MODEL"]
try:
    response = requests.get(f"{base}/api/tags", timeout=5)
    response.raise_for_status()
    names = [item.get("name", "") for item in response.json().get("models", [])]
    ready = any(name == model or name.startswith(f"{model}:") for name in names)
except requests.RequestException:
    ready = False
sys.exit(0 if ready else 75)'
then
  printf '{"status":"waiting_for_local_embedding","task_id":"%s"}\n' "$TASK_ID"
  exit 0
fi

cd "$REPO_ROOT"
exec "$PYTHON" "$REPO_ROOT/scripts/capture_codex.py" --task-id "$TASK_ID"
