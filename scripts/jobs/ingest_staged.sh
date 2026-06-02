#!/bin/bash
# Phase 2: Intelligent processing via Kiro headless.
# Processes staged chats one per session through Kiro CLI with Opus 4.6-1m.
# Runs via launchd at 3:00 AM daily.
#
# See docs/HYBRID-CHAT-EXTRACTION.md for full spec.

set -uo pipefail
cd "$(dirname "$0")/.."

LOGFILE="logs/ingest-$(date +%Y%m%d).log"
FAILED_DIR="staging/failed"
PROCESSED=0
FAILED=0

mkdir -p "$FAILED_DIR" logs

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOGFILE"; }

# Pre-flight: ensure PostgreSQL is reachable, starting Docker container if needed
if ! docker exec second-brain-db pg_isready -U memory_bank > /dev/null 2>&1; then
  log "PostgreSQL not reachable — starting container..."
  docker start second-brain-db > /dev/null 2>&1
  sleep 5
  if ! docker exec second-brain-db pg_isready -U memory_bank > /dev/null 2>&1; then
    log "ABORT: PostgreSQL still not reachable after starting container."
    exit 1
  fi
  log "Container started successfully."
fi

log "=== Phase 2: Staged Chat Ingestion ==="

for chat_file in staging/chats/*.md; do
  [ -f "$chat_file" ] || continue
  FILENAME=$(basename "$chat_file")
  log "Processing: $FILENAME"

  kiro-cli chat --no-interactive --trust-all-tools --model claude-opus-4.6-1m \
    "Process the staged chat at ~/second-brain/$chat_file. Read the file, chunk it by topic shifts, extract metadata (topics, decisions, action items, project, files discussed), generate embeddings via Bedrock, and store in the Second Brain PostgreSQL database. After successful storage, delete the staging file." \
    >> "$LOGFILE" 2>&1

  EXIT_CODE=$?
  if [ $EXIT_CODE -eq 0 ] && [ ! -f "$chat_file" ]; then
    log "OK: $FILENAME ingested and removed from staging"
    PROCESSED=$((PROCESSED + 1))
  else
    log "FAIL: $FILENAME (exit code $EXIT_CODE) — moved to $FAILED_DIR/"
    mv "$chat_file" "$FAILED_DIR/"
    FAILED=$((FAILED + 1))
  fi

  sleep 5
done

log "=== Run complete: processed=$PROCESSED failed=$FAILED ==="

# Ingest scraped web/YouTube sources from the Crawlee knowledge store
log "=== Crawlee source ingestion ==="
web_count() { docker exec second-brain-db psql -U memory_bank -d memory_bank -tAc \
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
