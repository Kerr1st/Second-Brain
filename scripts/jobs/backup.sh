#!/bin/bash
# Daily backup — pg_dump + GPG encrypt + rclone to Google Drive.
# S3 de-scoped 2026-06-01 (brittle overnight SSO); durable copies = local + GDrive + git.
# See docs/DISASTER-RECOVERY.md for full recovery procedures.
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."

BACKUP_DIR="backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
KEY_FILE="$HOME/second-brain/.backup-key"
CONTAINER="${DB_CONTAINER:-second-brain-db}"
DB_USER="${DB_USER:-memory_bank}"
DB_NAME="${DB_NAME:-memory_bank}"

mkdir -p "$BACKUP_DIR" logs

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# Pre-flight checks
MISSING=""
command -v gpg &>/dev/null || MISSING="$MISSING gpg"
command -v rclone &>/dev/null || MISSING="$MISSING rclone"
# Start container if not running
if ! pg_isready -h 127.0.0.1 -p 5432 -U "$DB_USER" &>/dev/null; then
  log "PostgreSQL not reachable — starting brew service..."
  brew services start postgresql@17 &>/dev/null
  sleep 5
fi
pg_isready -h 127.0.0.1 -p 5432 -U "$DB_USER" &>/dev/null || MISSING="$MISSING postgresql"
[ -f "$KEY_FILE" ] || MISSING="$MISSING encryption-key"

if [ -n "$MISSING" ]; then
  log "ABORT: missing prerequisites:$MISSING"
  log "Install: brew install gnupg rclone"
  log "Key: openssl rand -base64 32 > $KEY_FILE && chmod 600 $KEY_FILE"
  exit 1
fi

log "=== Backup: $TIMESTAMP ==="

# --- Database dump ---
log "Dumping PostgreSQL..."
pg_dump -h 127.0.0.1 -p 5432 -Fc -U "$DB_USER" "$DB_NAME" > "$BACKUP_DIR/memory_bank_${TIMESTAMP}.dump"
ROWS=$(psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT COUNT(*) FROM memories;" 2>/dev/null || echo "?")
log "Backed up $ROWS memories"

log "Exporting JSON fallback..."
psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(m) FROM memories m" \
  > "$BACKUP_DIR/memories_${TIMESTAMP}.json"
psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(e) FROM entities e" \
  > "$BACKUP_DIR/entities_${TIMESTAMP}.json"
psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(e) FROM entity_edges e" \
  > "$BACKUP_DIR/edges_${TIMESTAMP}.json"
psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(me) FROM memory_entities me" \
  > "$BACKUP_DIR/memory_entities_${TIMESTAMP}.json"

log "Encrypting..."
for f in "$BACKUP_DIR/memory_bank_${TIMESTAMP}.dump" \
         "$BACKUP_DIR/memories_${TIMESTAMP}.json" \
         "$BACKUP_DIR/entities_${TIMESTAMP}.json" \
         "$BACKUP_DIR/edges_${TIMESTAMP}.json" \
         "$BACKUP_DIR/memory_entities_${TIMESTAMP}.json"; do
  gpg --symmetric --cipher-algo AES256 --batch --yes --passphrase-file "$KEY_FILE" "$f"
  rm "$f"
done

# --- Knowledge base sources ---
log "Copying KB sources..."
mkdir -p "$BACKUP_DIR/kb-sources"
rsync -a --delete ~/Work/Tools/Crawlee/knowledge/sources/ "$BACKUP_DIR/kb-sources/" 2>/dev/null || true

# --- Config files ---
log "Copying config..."
mkdir -p "$BACKUP_DIR/config"
cp docker-compose.yml "$BACKUP_DIR/config/" 2>/dev/null || true
cp requirements.txt "$BACKUP_DIR/config/" 2>/dev/null || true
cp migrations/*.sql "$BACKUP_DIR/config/" 2>/dev/null || true
cp docs/*.md "$BACKUP_DIR/config/" 2>/dev/null || true
cp README.md "$BACKUP_DIR/config/" 2>/dev/null || true
cp scheduling/README.md "$BACKUP_DIR/config/scheduling-README.md" 2>/dev/null || true
cp -r ~/.kiro/skills/ "$BACKUP_DIR/config/kiro-skills/" 2>/dev/null || true

# --- Upload to Google Drive (primary cloud copy) ---
log "Uploading to Google Drive..."
GDRIVE_OK=true
rclone copy "$BACKUP_DIR/" gdrive:memory-bank-backups/ --include "*.gpg" 2>&1 || { log "WARN: Google Drive upload failed"; GDRIVE_OK=false; }
rclone sync "$BACKUP_DIR/kb-sources/" gdrive:memory-bank-backups/kb-sources/ 2>&1 || true
rclone sync "$BACKUP_DIR/config/" gdrive:memory-bank-backups/config/ 2>&1 || true

# rclone copy (above) verifies each transfer's size/hash and exits non-zero if the
# upload didn't land — so GDRIVE_OK from the copy is the source of truth for upload
# success. As a best-effort confirmation, list the SPECIFIC file (--files-only so
# the config/ and kb-sources/ subdirs don't count as a match). A transient Drive
# list error (rate-limit / "couldn't find root directory ID") must NOT flip a
# successful upload to "failed", so a list miss only warns — it never sets
# GDRIVE_OK=false. (Root cause of the prior nightly false-failures: the Drive API
# rate-limit on the verify's listing, masked by 2>/dev/null.)
if [ "$GDRIVE_OK" = true ]; then
  REMOTE_FILE="memory_bank_${TIMESTAMP}.dump.gpg"
  if rclone lsf gdrive:memory-bank-backups/ --files-only --include "$REMOTE_FILE" \
       --contimeout 20s --timeout 90s --retries 3 --low-level-retries 10 2>/tmp/sb_verify_err \
       | grep -Fxq "$REMOTE_FILE"; then
    log "✅ Google Drive backup verified ($ROWS memories; $REMOTE_FILE present on remote)"
  else
    log "⚠️  Upload reported success (rclone copy ok) but listing didn't confirm $REMOTE_FILE — likely a transient Drive rate-limit: $(tail -1 /tmp/sb_verify_err 2>/dev/null)"
  fi
  rm -f /tmp/sb_verify_err
fi

# --- Retention: 30 days cloud, 7 days local ---
log "Cleaning old backups..."
rclone delete gdrive:memory-bank-backups/ --min-age 30d --include "*.gpg" 2>/dev/null || true
find "$BACKUP_DIR" -name "*.gpg" -mtime +7 -delete 2>/dev/null || true

if [ "$GDRIVE_OK" = true ]; then
  log "=== Backup complete: $TIMESTAMP ==="
else
  log "=== Backup FAILED to verify on Google Drive: $TIMESTAMP ==="
  exit 1
fi
