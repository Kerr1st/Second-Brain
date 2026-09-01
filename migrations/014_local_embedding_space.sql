-- Preserve the former Titan vector space and create a clean local BGE-M3 space.
-- Incompatible embedding spaces must never be compared in the same pgvector column.

DROP INDEX IF EXISTS memories_embedding_idx;
DROP INDEX IF EXISTS idx_memories_embedding;

ALTER TABLE memories RENAME COLUMN embedding TO legacy_embedding;

ALTER TABLE memories
    ADD COLUMN embedding vector(1024),
    ADD COLUMN embedding_space text;

COMMENT ON COLUMN memories.legacy_embedding IS
    'Immutable legacy Amazon Titan 1024-dimension vector retained for rollback evidence';
COMMENT ON COLUMN memories.embedding IS
    'Active local embedding vector; compare only with the space named by embedding_space';
COMMENT ON COLUMN memories.embedding_space IS
    'Provider, model, and dimension identity for the active embedding vector';

CREATE OR REPLACE FUNCTION set_active_embedding_space()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.embedding IS NULL THEN
        NEW.embedding_space := NULL;
    ELSIF NEW.embedding_space IS NULL THEN
        NEW.embedding_space := 'ollama:bge-m3:1024';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memories_active_embedding_space
BEFORE INSERT OR UPDATE OF embedding, embedding_space ON memories
FOR EACH ROW EXECUTE FUNCTION set_active_embedding_space();

ALTER TABLE memories ADD CONSTRAINT memories_embedding_space_pair
CHECK (
    (embedding IS NULL AND embedding_space IS NULL)
    OR (embedding IS NOT NULL AND embedding_space IS NOT NULL)
);

CREATE INDEX idx_memories_embedding ON memories
USING hnsw (embedding vector_cosine_ops)
WITH (m = 32, ef_construction = 200);
