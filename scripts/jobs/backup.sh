#!/bin/bash
# Daily backup (launchd com.second-brain.backup, 02:00, via job_wrapper.sh).
#
# One encrypted bundle per run — pg_dump + repository trees + .env + knowledge
# sources → AES-256 GPG → backups/second-brain_<stamp>.tar.gpg → `rclone copyto`
# of THAT FILE ONLY to Google Drive, read back and compared, then retention
# (local 7 d / remote 30 d, *.gpg only; config/backup.toml). Only ciphertext ever
# leaves this machine: there is no rclone call in this file at all, and
# scripts/maintenance/backup_bundle.py hands rclone nothing but the .tar.gpg
# (tests/test_backup_bundle.py pins that). The historical plaintext trees on Drive
# (kb-sources/, config/, pre-2026-10-05) are NOT touched by this job — retiring
# them is the confirm-required manual step in docs/DISASTER-RECOVERY.md.
#
# The wrapper supplies PATH, AWS_PROFILE, cwd, lock, the deadline
# (SECOND_BRAIN_JOB_TIMEOUT_BACKUP ≥ 2 h) and the log. Exit codes: 0 ok;
# 1 bundle/upload/verification failed (JSON reason on stdout); 69 PostgreSQL down
# (not auto-started — see scripts/jobs/_preflight.sh); 78 config/tool missing.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="${SECOND_BRAIN_TOOL_PATH:+$SECOND_BRAIN_TOOL_PATH:}/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# shellcheck source=scripts/jobs/_preflight.sh
. "$ROOT/scripts/jobs/_preflight.sh"
cd "$ROOT" || exit 2

log() { echo "[$(date '+%H:%M:%S')] $1"; }

MISSING=""
for tool in gpg rclone pg_dump pg_restore; do command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"; done
if [ -n "$MISSING" ]; then
  log "ABORT (exit 78): missing tools:$MISSING — brew install gnupg rclone postgresql@17"
  exit 78
fi
require_postgres

log "=== Backup bundle: $(date '+%Y-%m-%d %H:%M:%S') ==="
exec "${SECOND_BRAIN_PYTHON:-$ROOT/.venv/bin/python}" "$ROOT/scripts/maintenance/backup_bundle.py" create --repo "$ROOT" "$@"
