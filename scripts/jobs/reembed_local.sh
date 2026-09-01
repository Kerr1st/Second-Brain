#!/bin/bash
# Run one bounded, resumable local embedding batch.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LIMIT="${SECOND_BRAIN_REEMBED_LIMIT:-5000}"
BATCH_SIZE="${SECOND_BRAIN_REEMBED_BATCH_SIZE:-32}"
LOCK="${SECOND_BRAIN_REEMBED_LOCK:-/tmp/com.second-brain.reembed-local.lock}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Second Brain Python is not executable: $PYTHON" >&2
  exit 2
fi
if [[ ! "$LIMIT" =~ ^[0-9]+$ ]] || [[ ! "$BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "re-embedding limit and batch size must be non-negative integers" >&2
  exit 2
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  printf '{"status":"already_running"}\n'
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

export EMBEDDING_PROVIDER="ollama"
export OLLAMA_EMBEDDING_MODEL="${OLLAMA_EMBEDDING_MODEL:-bge-m3}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434}"

cd "$REPO_ROOT"
"$PYTHON" "$REPO_ROOT/scripts/reembed_memories.py" \
  --limit "$LIMIT" \
  --batch-size "$BATCH_SIZE"
