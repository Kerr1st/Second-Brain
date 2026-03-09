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

# Pre-flight: check PostgreSQL is reachable
if ! docker exec second-brain-db pg_isready -U memory_bank > /dev/null 2>&1; then
  log "ABORT: PostgreSQL is not reachable. Is Docker running?"
  exit 1
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

# Write status file for monitoring
cat > logs/last_ingest_status.json << EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "processed": $PROCESSED,
  "failed": $FAILED,
  "pending": $(ls staging/chats/*.md 2>/dev/null | wc -l | tr -d ' '),
  "failed_files": $(ls staging/failed/*.md 2>/dev/null | wc -l | tr -d ' ')
}
EOF
