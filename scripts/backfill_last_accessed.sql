-- backfill_last_accessed.sql
--
-- Purpose: Populate the last_accessed_at column for all existing memories
-- that currently have a NULL value. Sets last_accessed_at = updated_at as a
-- reasonable default — the best available proxy for "last touched" on legacy rows.
--
-- This ensures the spacing bonus in the V2 reranking formula produces meaningful
-- values instead of defaulting to 1.0 for every legacy memory.
--
-- Usage:  psql $DATABASE_URL -f scripts/backfill_last_accessed.sql
--
-- Idempotent: The WHERE clause filters on last_accessed_at IS NULL, so re-running
-- this script after all rows have been backfilled will match zero rows and the
-- UPDATE will be a no-op.
--
-- This is a one-time data fix, not a schema migration, which is why it lives in
-- scripts/ rather than migrations/.

-- Step 1: Preview how many rows will be affected.
SELECT count(*) AS rows_to_update
FROM memories
WHERE last_accessed_at IS NULL;

-- Step 2: Backfill last_accessed_at from updated_at for all NULL rows.
UPDATE memories
SET last_accessed_at = updated_at
WHERE last_accessed_at IS NULL;
