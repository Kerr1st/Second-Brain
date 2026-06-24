-- 009_hnsw_recall.sql
-- Strengthen HNSW recall. Diagnosed 2026-06-04: mean recall@10 was 0.65 (and 0.0 for
-- conceptual/synthesis queries) because (a) the embedding index used default params
-- (m=16, ef_construction=64), and (b) a ~39% bulk delete degraded the HNSW graph
-- ("unreachable points"). VACUUM does NOT rebuild HNSW graph connectivity — only a
-- rebuild/REINDEX does. Rebuilding with m=32 / ef_construction=200 and ef_search=200
-- restored mean recall@10 to 0.96 and conceptual-query recall to ~1.0.
--
-- MAINTENANCE: after any bulk delete of >~10% of rows, rebuild this index
-- (see scripts/jobs/reindex_embedding.sh) — a VACUUM alone is not sufficient.
--
-- Idempotent: only (re)builds when the index is missing or still on weak params, so it
-- is a no-op on a database that already has the strengthened index.
DO $$
DECLARE opts text[];
BEGIN
  SELECT reloptions INTO opts FROM pg_class WHERE relname = 'idx_memories_embedding';
  IF opts IS NULL OR NOT ('ef_construction=200' = ANY(opts)) THEN
    DROP INDEX IF EXISTS memories_embedding_idx;   -- default auto-name from 001
    DROP INDEX IF EXISTS idx_memories_embedding;
    EXECUTE 'CREATE INDEX idx_memories_embedding ON memories '
            'USING hnsw (embedding vector_cosine_ops) WITH (m = 32, ef_construction = 200)';
  END IF;
END $$;

-- Higher ef_search trades a little latency for materially better recall (sub-ms at our scale).
ALTER DATABASE memory_bank SET hnsw.ef_search = 200;
