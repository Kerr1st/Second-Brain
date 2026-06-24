-- Migration 007: Schema memory type support
-- Adds infrastructure for schema-level retrieval (Bartlett 1932; Piaget).
--
-- Schemas are abstract structures that group related principles, decisions,
-- and insights. They represent named patterns discovered by the dream cycle
-- when 3+ memories share an unnamed principle.
--
-- No new tables needed — schemas are memories with type='schema' and
-- relationships of type 'derived_from' to their constituent memories.
-- This migration adds an index to support efficient schema lookups.

-- ============================================================
-- Index: schema type lookup
-- Supports fast retrieval of all schema memories and their
-- constituent relationships.
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_type_schema
    ON memories (type) WHERE type = 'schema';

-- ============================================================
-- Index: derived_from relationships for schema constituents
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_relationships_derived_from
    ON memory_relationships (relation_type, source_id)
    WHERE relation_type = 'derived_from';

-- ============================================================
-- Column comment
-- ============================================================
COMMENT ON INDEX idx_memories_type_schema IS
    'Partial index for schema-type memories (Bartlett 1932). '
    'Schemas group related principles/insights into abstract structures. '
    'Used for two-level retrieval: schema first, then constituents.';
