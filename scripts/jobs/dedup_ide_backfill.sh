#!/bin/bash
# One-time P2b cleanup: batch-delete the legacy ide_*.md duplicate IDE-chat rows
# (every one has a surviving chat:// twin — verified). Children (parent_id NOT
# NULL) are deleted before parents to respect the parent_id self-FK (NO ACTION).
# Separate transaction per batch so locks release between batches and search
# stays available. A single bulk delete is pathologically slow due to HNSW
# vector-index maintenance, hence batching.
#
# Roles (R7, migration 017): the deletes are DML and run as the application role
# (DB_USER); the closing VACUUM needs table ownership and runs as the owner role
# (DB_MIGRATE_USER). Neither the database nor a role is named literally.
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
DB_HOST="${DB_HOST:-127.0.0.1}"; DB_PORT="${DB_PORT:-5432}"; DB_NAME="${DB_NAME:-memory_bank}"
DB_USER="${DB_USER:-memory_bank}"; DB_MIGRATE_USER="${DB_MIGRATE_USER:-memory_bank_owner}"
[ -n "${DB_PASSWORD:-}" ] && export PGPASSWORD="$DB_PASSWORD"
APP=(psql -X -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME")
DB() { "${APP[@]}" -tAc "$1"; }
LEGACY() { DB "SELECT COUNT(*) FROM memories WHERE source_type='kiro_ide_chat' AND source_url LIKE 'ide_%'${1:-}"; }

echo "$(date '+%H:%M:%S') START legacy=$(LEGACY)"
for cond in "AND parent_id IS NOT NULL" "AND parent_id IS NULL"; do
  prev=-1
  while :; do
    rem=$(LEGACY " $cond")
    [ "${rem:-0}" -eq 0 ] && break
    if [ "$rem" -eq "$prev" ]; then echo "$(date '+%H:%M:%S') NO PROGRESS at $rem — aborting"; exit 1; fi
    prev=$rem
    "${APP[@]}" -c \
      "SET statement_timeout='150s'; WITH d AS (SELECT id FROM memories WHERE source_type='kiro_ide_chat' AND source_url LIKE 'ide_%' $cond LIMIT 2000) DELETE FROM memories WHERE id IN (SELECT id FROM d);" >/dev/null 2>&1 || true
    echo "$(date '+%H:%M:%S') [$cond] legacy_remaining=$(LEGACY)"
  done
done
echo "$(date '+%H:%M:%S') DONE legacy=$(LEGACY) ide_total=$(DB "SELECT COUNT(*) FROM memories WHERE source_type='kiro_ide_chat'") total=$(DB 'SELECT COUNT(*) FROM memories')"
# Required after a bulk delete: clear HNSW index tombstones + refresh planner
# stats, else vector recall degrades. Verified: skipping this dropped
# distilled-in-top5 from 7/12 to 5/12; VACUUM restored it to 8/12.
echo "$(date '+%H:%M:%S') VACUUM ANALYZE memories ..."
PGPASSWORD="${DB_MIGRATE_PASSWORD:-${DB_PASSWORD:-}}" psql -X -h "$DB_HOST" -p "$DB_PORT" -U "$DB_MIGRATE_USER" -d "$DB_NAME" -c "VACUUM (ANALYZE) memories;" >/dev/null 2>&1
echo "$(date '+%H:%M:%S') COMPLETE"
