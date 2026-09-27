#!/bin/bash
# Wrapper supplies environment, lock, deadline, status and logs. Only ciphertext leaves the host.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
ARGS=(create --repo "$ROOT" --remote "${SECOND_BRAIN_BACKUP_REMOTE:-gdrive:memory-bank-backups}")
if [ -n "${SECOND_BRAIN_KB_SOURCES:-}" ]; then ARGS+=(--kb-sources "$SECOND_BRAIN_KB_SOURCES"); fi
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/maintenance/backup_bundle.py" "${ARGS[@]}" "$@"
