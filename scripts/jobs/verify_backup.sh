#!/bin/bash
# Weekly backup verification (launchd com.second-brain.verify, Sun 03:00, via
# job_wrapper.sh; deadline SECOND_BRAIN_JOB_TIMEOUT_VERIFY ≥ 2 h).
#
# 1. List the encrypted files at the root of the remote — BOTH naming schemes,
#    because during the R5 transition the remote holds pre-R5
#    memory_bank_<ts>.dump.gpg files next to new second-brain_<ts>.tar.gpg bundles.
# 2. Pick the newest by the timestamp in its name and FAIL if it is older than
#    config/backup.toml [verify].max_age_hours (36 h): a Drive copy that stopped
#    updating must not pass just because it decrypts (E-N9 / F-137d).
# 3. Download it and verify:
#      bundle       → backup_bundle.py verify --restore : manifest checksums +
#                     modes, then a FULL pg_restore into a fresh socket-only
#                     cluster, reporting per-table row counts;
#      legacy dump  → backup_bundle.py verify-legacy    : decrypt + pg_restore --list
#                     (what the pre-R5 verify did), so the old format stays
#                     verifiable until the last .dump.gpg ages out of retention.
# Time budget: ~1.1 GB download (Drive-bound, minutes) + decrypt ~10 s + restore
# ~4 min (219 s measured on the R3 clone); per-command cap command_timeout_s=3600,
# job deadline 2 h.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="${SECOND_BRAIN_TOOL_PATH:+$SECOND_BRAIN_TOOL_PATH:}/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$ROOT" || exit 2
PYTHON="${SECOND_BRAIN_PYTHON:-$ROOT/.venv/bin/python}"
BUNDLE="$ROOT/scripts/maintenance/backup_bundle.py"
REMOTE="${SECOND_BRAIN_BACKUP_REMOTE:-$("$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from scripts.maintenance.backup_bundle import load_config; print(load_config()["remote"] or "")' "$ROOT" 2>/dev/null)}"
REMOTE="${REMOTE:-gdrive:memory-bank-backups}"

log() { echo "[$(date '+%H:%M:%S')] $1"; }

MISSING=""
for tool in gpg rclone pg_restore initdb pg_ctl; do command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"; done
if [ -n "$MISSING" ]; then
  log "ABORT (exit 78): missing tools:$MISSING"
  exit 78
fi

AREA="$(mktemp -d "${TMPDIR:-/tmp}/sb-cloud-verify.XXXXXX")" || exit 1
chmod 700 "$AREA"
trap 'rm -rf "$AREA"' EXIT

log "=== Backup verification: $(date '+%Y-%m-%d %H:%M:%S') — remote $REMOTE ==="
if ! rclone lsf "$REMOTE" --files-only --max-depth 1 --include '*.gpg' \
     --contimeout 20s --timeout 90s --retries 3 --low-level-retries 10 > "$AREA/list" 2> "$AREA/lsf.err"; then
  log "❌ cannot list $REMOTE: $(tail -1 "$AREA/lsf.err" 2>/dev/null)"
  exit 1
fi

if ! SELECTION="$("$PYTHON" "$BUNDLE" select-latest "$AREA/list" "$@")"; then
  log "❌ freshness check failed: $SELECTION"
  exit 1
fi
LATEST="$(printf '%s' "$SELECTION" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
KIND="$(printf '%s' "$SELECTION" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["kind"])')"
log "newest: $LATEST ($KIND) — $SELECTION"
case "$LATEST" in */*|"") log "❌ refusing unexpected remote name"; exit 1;; esac

if ! rclone copyto "$REMOTE/$LATEST" "$AREA/$LATEST"; then
  log "❌ download failed: $LATEST"
  exit 1
fi

if [ "$KIND" = bundle ]; then
  RESULT="$("$PYTHON" "$BUNDLE" verify "$AREA/$LATEST" --restore)"; RC=$?
else
  RESULT="$("$PYTHON" "$BUNDLE" verify-legacy "$AREA/$LATEST")"; RC=$?
fi
log "$RESULT"
if [ "$RC" -ne 0 ]; then
  log "=== VERIFICATION FAILED ($KIND) ==="
  exit 1
fi
log "=== Verification passed ($KIND, fresh) ==="
