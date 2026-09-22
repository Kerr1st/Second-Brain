#!/bin/bash
# Codex session capture (docs/PLAN-capture-repair.md).
#
# Captures the user's own Codex Tasks from ~/.codex into `memories` as
# source_type='codex_task' (verbatim task record + per-topic-segment derived
# memories). Delegated sub-agent executions are skipped by the source itself.
#
# Adapted 2026-09-22 when this code was ported from origin/main (whose history is
# unrelated to local main). Two defaults from the original had to change because
# they do not exist on this machine:
#
#   * Profile `codex_local` → `controlled`. `codex_local` is not in
#     config/backends.toml. Of the profiles that do exist, `laptop` and `hybrid`
#     resolve the Thinker to Kiro and `bedrock` resolves it to Fable 5.1 — both
#     forbidden for corpus content (docs/PLAN-panel-acceptance.md §3: the corpus
#     never runs on Kiro/Fable). `controlled` keeps the semantic pass on
#     Bedrock Opus 5 inside the user's own account.
#   * Embeddings: the original gated the whole run on a local Ollama/bge-m3
#     server and, when it was absent, printed {"status":"waiting_for_local_embedding"}
#     and exited 0 — a silent no-op that reports success. There is no Ollama here
#     and src/embeddings.py is Titan-only, so the gate is removed: embeddings come
#     from Amazon Titan v2 via Bedrock, the same vector space as the rest of the
#     corpus (mixing embedding models in one column would break vector search).
#
# Exits non-zero on failure so com.second-brain.liveness can see it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Second Brain Python is not executable: $PYTHON" >&2
  exit 2
fi

export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
# Least-privilege IAM user: Bedrock InvokeModel (Titan embeddings + the semantic pass).
export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"
export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

cd "$REPO_ROOT"

if ! pg_isready -h 127.0.0.1 -p 5432 -U memory_bank >/dev/null 2>&1; then
  echo "ABORT: PostgreSQL is not reachable" >&2
  exit 1
fi

if [[ ! -d "$CODEX_HOME" ]]; then
  echo "ABORT: CODEX_HOME does not exist: $CODEX_HOME" >&2
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] codex-capture starting (profile=$SECOND_BRAIN_PROFILE)"
exec "$PYTHON" "$REPO_ROOT/scripts/capture_codex.py" "$@"
