#!/bin/bash
# Weekly backup verification — decrypts and validates the Google Drive backup.
# (S3 de-scoped 2026-06-01; durable copies = local + GDrive + git.)
# See docs/DISASTER-RECOVERY.md for recovery procedures.
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."

KEY_FILE="$HOME/second-brain/.backup-key"
VERIFY_DIR="/tmp/second-brain-verify"

mkdir -p "$VERIFY_DIR" logs

log() { echo "[$(date '+%H:%M:%S')] $1"; }

MISSING=""
command -v gpg &>/dev/null || MISSING="$MISSING gpg"
command -v rclone &>/dev/null || MISSING="$MISSING rclone"
[ -f "$KEY_FILE" ] || MISSING="$MISSING encryption-key"

if [ -n "$MISSING" ]; then
  log "ABORT: missing prerequisites:$MISSING"
  exit 1
fi

ERRORS=0

log "=== Backup Verification: $(date) ==="

verify_cloud() {
  local name="$1" remote="$2"

  log "--- $name ---"
  LATEST=$(rclone ls "$remote" --include "*.dump.gpg" 2>/dev/null | sort -k2 | tail -1 | awk '{print $2}')

  if [ -z "$LATEST" ]; then
    log "❌ NO BACKUPS FOUND on $name"
    ERRORS=$((ERRORS + 1))
    return
  fi

  log "Latest: $LATEST"
  rclone copy "$remote/$LATEST" "$VERIFY_DIR/" 2>/dev/null

  if gpg --decrypt --batch --passphrase-file "$KEY_FILE" \
    "$VERIFY_DIR/$(basename "$LATEST")" > "$VERIFY_DIR/test.dump" 2>/dev/null; then

    # Validate dump structure via docker
    if pg_restore -l < "$VERIFY_DIR/test.dump" > /dev/null 2>&1; then
      TABLES=$(pg_restore -l < "$VERIFY_DIR/test.dump" 2>/dev/null | grep "TABLE DATA" | wc -l | tr -d ' ')
      log "✅ $name backup valid ($TABLES tables)"
    else
      log "❌ $name BACKUP CORRUPT — pg_restore cannot read it"
      ERRORS=$((ERRORS + 1))
    fi
  else
    log "❌ $name DECRYPTION FAILED — key mismatch or corrupt file"
    ERRORS=$((ERRORS + 1))
  fi

  KB_COUNT=$(rclone ls "$remote/kb-sources/" 2>/dev/null | wc -l | tr -d ' ')
  CFG_COUNT=$(rclone ls "$remote/config/" 2>/dev/null | wc -l | tr -d ' ')
  log "   KB sources: $KB_COUNT files, Config: $CFG_COUNT files"

  rm -f "$VERIFY_DIR"/*
}

verify_cloud "Google Drive" "gdrive:memory-bank-backups"

rm -rf "$VERIFY_DIR"

if [ $ERRORS -gt 0 ]; then
  log "=== VERIFICATION FAILED: $ERRORS errors ==="
  exit 1
else
  log "=== Verification passed ==="
fi
