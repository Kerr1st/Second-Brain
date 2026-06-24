-- Migration 006: Encoding context for contextual reinstatement
-- Adds encoding_context column to memories table.
--
-- Research basis: Godden & Baddeley (1975) — memory retrieval is
-- dramatically better when the retrieval context matches the encoding
-- context. This column captures the cognitive context at creation time:
-- what problem was being solved, what project was active, what the
-- user was thinking about. Used as a reranking signal at search time.

-- ============================================================
-- Column: encoding_context
-- Free-text description of the cognitive context at encoding time.
-- Examples: "debugging auth flow", "reading about CLS theory",
-- "planning dream cycle architecture", "reviewing PR feedback"
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS encoding_context TEXT;

-- ============================================================
-- Index for full-text search on encoding_context
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_encoding_context
    ON memories USING gin (to_tsvector('english', coalesce(encoding_context, '')));

-- ============================================================
-- Column comment documenting research basis
-- ============================================================
COMMENT ON COLUMN memories.encoding_context IS
    'Cognitive context at encoding time (Godden & Baddeley 1975). '
    'Captures what the user was working on when the memory was created. '
    'Used as a reranking signal via token overlap with query context.';
