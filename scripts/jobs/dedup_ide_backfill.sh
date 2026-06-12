#!/bin/bash
# One-time P2b cleanup: batch-delete the legacy ide_*.md duplicate IDE-chat rows
# (every one has a surviving chat:// twin — verified). Children (parent_id NOT
# NULL) are deleted before parents to respect the parent_id self-FK (NO ACTION).
# Separate transaction per batch so locks release between batches and search
# stays available. A single bulk delete is pathologically slow due to HNSW
# vector-index maintenance, hence batching.
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
DB() { psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank -tAc "$1"; }
LEGACY() { DB "SELECT COUNT(*) FROM memories WHERE source_type='kiro_ide_chat' AND source_url LIKE 'ide_%'${1:-}"; }

echo "$(date '+%H:%M:%S') START legacy=$(LEGACY)"
for cond in "AND parent_id IS NOT NULL" "AND parent_id IS NULL"; do
  prev=-1
  while :; do
    rem=$(LEGACY " $cond")
    [ "${rem:-0}" -eq 0 ] && break
    if [ "$rem" -eq "$prev" ]; then echo "$(date '+%H:%M:%S') NO PROGRESS at $rem — aborting"; exit 1; fi
    prev=$rem
    psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank -c \
      "SET statement_timeout='150s'; WITH d AS (SELECT id FROM memories WHERE source_type='kiro_ide_chat' AND source_url LIKE 'ide_%' $cond LIMIT 2000) DELETE FROM memories WHERE id IN (SELECT id FROM d);" >/dev/null 2>&1 || true
    echo "$(date '+%H:%M:%S') [$cond] legacy_remaining=$(LEGACY)"
  done
done
echo "$(date '+%H:%M:%S') DONE legacy=$(LEGACY) ide_total=$(DB "SELECT COUNT(*) FROM memories WHERE source_type='kiro_ide_chat'") total=$(DB 'SELECT COUNT(*) FROM memories')"
# Required after a bulk delete: clear HNSW index tombstones + refresh planner
# stats, else vector recall degrades. Verified: skipping this dropped
# distilled-in-top5 from 7/12 to 5/12; VACUUM restored it to 8/12.
echo "$(date '+%H:%M:%S') VACUUM ANALYZE memories ..."
psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank -c "VACUUM (ANALYZE) memories;" >/dev/null 2>&1
echo "$(date '+%H:%M:%S') COMPLETE"
