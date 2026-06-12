#!/bin/bash
# Phase 2: Staged chat ingestion + Crawlee web/YouTube ingestion.
# Ingests staged chats directly via batch_ingest_staged.py (idempotent, no
# external CLI dependency), then pulls new Crawlee knowledge-store content.
# Runs via launchd at 3:00 AM daily.
#
# See docs/HYBRID-CHAT-EXTRACTION.md for full spec.

set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."

LOGFILE="logs/ingest-$(date +%Y%m%d).log"
FAILED_DIR="staging/failed"
PROCESSED=0
FAILED=0

mkdir -p "$FAILED_DIR" logs

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOGFILE"; }

# Pre-flight: ensure PostgreSQL is reachable, starting Docker container if needed
if ! pg_isready -h 127.0.0.1 -p 5432 -U memory_bank > /dev/null 2>&1; then
  log "PostgreSQL not reachable — starting brew service..."
  brew services start postgresql@17 > /dev/null 2>&1
  sleep 5
  if ! pg_isready -h 127.0.0.1 -p 5432 -U memory_bank > /dev/null 2>&1; then
    log "ABORT: PostgreSQL still not reachable after starting brew service."
    exit 1
  fi
  log "Brew service started successfully."
fi

log "=== Phase 2: Staged chat ingestion ==="
CHAT_OUT=$(.venv/bin/python scripts/batch_ingest_staged.py 2>&1)
echo "$CHAT_OUT" >> "$LOGFILE"
PROCESSED=$(echo "$CHAT_OUT" | grep -oE "processed=[0-9]+" | tail -1 | cut -d= -f2); PROCESSED=${PROCESSED:-0}
FAILED=$(echo "$CHAT_OUT" | grep -oE "failed=[0-9]+" | tail -1 | cut -d= -f2); FAILED=${FAILED:-0}
log "=== Chat ingestion complete: processed=$PROCESSED failed=$FAILED ==="

# Ingest scraped web/YouTube sources from the Crawlee knowledge store
log "=== Crawlee source ingestion ==="
web_count() { psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank -tAc \
  "SELECT COUNT(*) FROM memories WHERE source_type IN ('article','youtube');" 2>/dev/null || echo 0; }
CRAWLEE_BEFORE=$(web_count)
if .venv/bin/python scripts/crawlee_ingest.py >> "$LOGFILE" 2>&1; then
  log "Crawlee ingest OK"
else
  log "Crawlee ingest FAILED (exit $?)"
fi
CRAWLEE_AFTER=$(web_count)
CRAWLEE_CAPTURES=$(( ${CRAWLEE_AFTER:-0} - ${CRAWLEE_BEFORE:-0} ))
log "captures_this_cycle: chats=$PROCESSED web/youtube=$CRAWLEE_CAPTURES"

# Write status file for monitoring
cat > logs/last_ingest_status.json << EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "processed": $PROCESSED,
  "failed": $FAILED,
  "crawlee_captures": ${CRAWLEE_CAPTURES:-0},
  "pending": $(ls staging/chats/*.md 2>/dev/null | wc -l | tr -d ' '),
  "failed_files": $(ls staging/failed/*.md 2>/dev/null | wc -l | tr -d ' ')
}
EOF

# Purge ingested files older than 7 days
find staging/ingested/ -type f -mtime +7 -delete 2>/dev/null

exit 0
