#!/bin/bash
# Rebuild the HNSW embedding index. Run this after any bulk delete of >~10% of rows:
# a VACUUM does NOT repair HNSW graph connectivity ("unreachable points") — only a
# rebuild does, and skipping it silently tanks recall (we measured mean recall@10
# fall to 0.65, with 0.0 on conceptual queries, after a 39% delete).
#
# Builds CONCURRENTLY (non-blocking for reads/writes) with parallel workers OFF — the
# parallel build otherwise needs ~500MB in Docker's 64MB /dev/shm and fails. Then swaps
# the new index in and restores ef_search.
#
# Roles (R7, migration 017): every statement here is DDL on objects the OWNER role
# owns (CREATE/DROP/ALTER INDEX, ALTER DATABASE ... SET), so this script connects as
# DB_MIGRATE_USER (default memory_bank_owner), never as the application role, and
# never names the database literally: DB_NAME (default memory_bank) is the target,
# and ALTER DATABASE uses current_database(). Local pg_hba is trust; if a password
# is ever required, set DB_MIGRATE_PASSWORD (falls back to DB_PASSWORD).
#
# Usage: bash scripts/jobs/reindex_embedding.sh
set -euo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-memory_bank}"
DB_MIGRATE_USER="${DB_MIGRATE_USER:-memory_bank_owner}"
if [ -n "${DB_MIGRATE_PASSWORD:-${DB_PASSWORD:-}}" ]; then
  export PGPASSWORD="${DB_MIGRATE_PASSWORD:-$DB_PASSWORD}"
fi
PSQL=(psql -X -v ON_ERROR_STOP=1 -h "$DB_HOST" -p "$DB_PORT" -U "$DB_MIGRATE_USER" -d "$DB_NAME")

# Refuse early with a clear message instead of failing half-way through the swap.
OWNER=$("${PSQL[@]}" -Atc "SELECT datdba::regrole FROM pg_database WHERE datname = current_database()")
if [ "$OWNER" != "$DB_MIGRATE_USER" ]; then
  echo "refuse: connected as '$DB_MIGRATE_USER' but the database is owned by '$OWNER'; run as the owner role (DB_MIGRATE_USER)" >&2
  exit 2
fi

echo "$(date '+%H:%M:%S') building idx_memories_embedding_v2 (m=32, ef_construction=200) as $DB_MIGRATE_USER on $DB_NAME..."
# maintenance_work_mem must be large enough to hold the whole graph in RAM. If the
# build logs "hnsw graph no longer fits into maintenance_work_mem", the newest vectors
# are built in a degraded on-disk phase that measurably hurts recall — raise this value.
# 1GB fits ~120k vectors; bump it as the corpus grows.
PGOPTIONS="-c max_parallel_maintenance_workers=0 -c maintenance_work_mem=1GB" \
  "${PSQL[@]}" \
  -c "DROP INDEX IF EXISTS idx_memories_embedding_v2;" \
  -c "CREATE INDEX CONCURRENTLY idx_memories_embedding_v2 ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 32, ef_construction = 200);"

echo "$(date '+%H:%M:%S') swapping new index in..."
"${PSQL[@]}" -c "DROP INDEX CONCURRENTLY IF EXISTS idx_memories_embedding;"
"${PSQL[@]}" -c "ALTER INDEX idx_memories_embedding_v2 RENAME TO idx_memories_embedding;"
"${PSQL[@]}" -c "DO \$\$ BEGIN EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search = 200', current_database()); END \$\$;"
echo "$(date '+%H:%M:%S') done. Verify with: .venv/bin/python scripts/eval/recall_check.py --verbose"
