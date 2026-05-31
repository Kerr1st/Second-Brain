#!/bin/bash
# Daily backup — pg_dump + GPG encrypt + rclone to Google Drive + S3.
# See docs/DISASTER-RECOVERY.md for full recovery procedures.
set -uo pipefail
cd "$(dirname "$0")/.."

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
if ! docker exec "$CONTAINER" pg_isready -U "$DB_USER" &>/dev/null; then
  log "PostgreSQL not reachable — starting container..."
  docker start "$CONTAINER" &>/dev/null
  sleep 5
fi
docker exec "$CONTAINER" pg_isready -U "$DB_USER" &>/dev/null || MISSING="$MISSING docker/postgresql"
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
docker exec "$CONTAINER" pg_dump -Fc -U "$DB_USER" "$DB_NAME" > "$BACKUP_DIR/memory_bank_${TIMESTAMP}.dump"

log "Exporting JSON fallback..."
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(m) FROM memories m" \
  > "$BACKUP_DIR/memories_${TIMESTAMP}.json"
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(e) FROM entities e" \
  > "$BACKUP_DIR/entities_${TIMESTAMP}.json"
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c \
  "SELECT row_to_json(e) FROM entity_edges e" \
  > "$BACKUP_DIR/edges_${TIMESTAMP}.json"
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c \
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

# --- Upload to both clouds ---
log "Uploading to Google Drive..."
rclone copy "$BACKUP_DIR/" gdrive:memory-bank-backups/ --include "*.gpg" 2>&1 || log "WARN: Google Drive upload failed"
rclone sync "$BACKUP_DIR/kb-sources/" gdrive:memory-bank-backups/kb-sources/ 2>&1 || true
rclone sync "$BACKUP_DIR/config/" gdrive:memory-bank-backups/config/ 2>&1 || true

log "Checking AWS SSO token..."
S3_OK=true
if ! aws sts get-caller-identity &>/dev/null; then
  log "SSO token expired — attempting refresh..."
  if aws sso login --profile default &>/dev/null; then
    log "SSO token refreshed"
  else
    log "WARN: SSO refresh failed — S3 upload will be skipped"
    log "WARN: Run 'aws sso login --profile default' manually to restore S3 backups"
    S3_OK=false
  fi
fi

if [ "$S3_OK" = true ]; then
  log "Uploading to AWS S3..."
  rclone copy "$BACKUP_DIR/" s3:memory-bank-backups/ --include "*.gpg" 2>&1 || log "WARN: S3 upload failed"
  rclone sync "$BACKUP_DIR/kb-sources/" s3:memory-bank-backups/kb-sources/ 2>&1 || true
  rclone sync "$BACKUP_DIR/config/" s3:memory-bank-backups/config/ 2>&1 || true
else
  log "WARN: S3 upload SKIPPED — no valid AWS credentials"
fi

# --- Retention: 30 days cloud, 7 days local ---
log "Cleaning old backups..."
rclone delete gdrive:memory-bank-backups/ --min-age 30d --include "*.gpg" 2>/dev/null || true
rclone delete s3:memory-bank-backups/ --min-age 30d --include "*.gpg" 2>/dev/null || true
find "$BACKUP_DIR" -name "*.gpg" -mtime +7 -delete 2>/dev/null || true

log "=== Backup complete: $TIMESTAMP ==="
