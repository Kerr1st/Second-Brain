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
# Usage: bash scripts/jobs/reindex_embedding.sh
set -euo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
CON=second-brain-db
PSQL=(psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank)

echo "$(date '+%H:%M:%S') building idx_memories_embedding_v2 (m=32, ef_construction=200)..."
# maintenance_work_mem must be large enough to hold the whole graph in RAM. If the
# build logs "hnsw graph no longer fits into maintenance_work_mem", the newest vectors
# are built in a degraded on-disk phase that measurably hurts recall — raise this value.
# 1GB fits ~120k vectors; bump it as the corpus grows.
PGOPTIONS="-c max_parallel_maintenance_workers=0 -c maintenance_work_mem=1GB" \
  psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank \
  -c "DROP INDEX IF EXISTS idx_memories_embedding_v2;" \
  -c "CREATE INDEX CONCURRENTLY idx_memories_embedding_v2 ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 32, ef_construction = 200);"

echo "$(date '+%H:%M:%S') swapping new index in..."
"${PSQL[@]}" -c "DROP INDEX CONCURRENTLY IF EXISTS idx_memories_embedding;"
"${PSQL[@]}" -c "ALTER INDEX idx_memories_embedding_v2 RENAME TO idx_memories_embedding;"
"${PSQL[@]}" -c "ALTER DATABASE memory_bank SET hnsw.ef_search = 200;"
echo "$(date '+%H:%M:%S') done. Verify with: .venv/bin/python scripts/eval/recall_check.py --verbose"
