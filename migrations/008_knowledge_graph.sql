-- Migration 008: Knowledge Graph
-- Adds entity/edge tables for structured knowledge alongside the existing memory store.
-- Design informed by Quick Desktop's dual-store architecture: memories for behavioral
-- knowledge (facts, preferences, patterns), KG for structural knowledge (who works where,
-- what depends on what, who attended what).

-- ============================================================
-- Entities: typed nodes in the knowledge graph
-- ============================================================
CREATE TABLE IF NOT EXISTS entities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category    TEXT NOT NULL,          -- Person, Organization, Project, Product, Decision, DefinedTerm, etc.
    name        TEXT NOT NULL,
    summary     TEXT,
    properties  JSONB DEFAULT '{}',     -- flexible metadata (aliases, source_ids, etc.)
    source_type TEXT,                   -- where this entity was extracted from
    embedding   vector(1536),           -- for semantic entity search
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(category, name)              -- no duplicate entities within a category
);

CREATE INDEX IF NOT EXISTS idx_entities_category ON entities(category);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities USING gin (to_tsvector('english', name));
CREATE INDEX IF NOT EXISTS idx_entities_embedding ON entities USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- ============================================================
-- Entity edges: typed relationships between entities
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_edges (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_entity   UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,           -- worksFor, memberOf, dependsOn, isPartOf, attended, etc.
    weight      REAL DEFAULT 1.0,
    properties  JSONB DEFAULT '{}',     -- reason, source context, timestamps
    source_type TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(from_entity, to_entity, relation)
);

CREATE INDEX IF NOT EXISTS idx_entity_edges_from ON entity_edges(from_entity);
CREATE INDEX IF NOT EXISTS idx_entity_edges_to ON entity_edges(to_entity);
CREATE INDEX IF NOT EXISTS idx_entity_edges_relation ON entity_edges(relation);

-- ============================================================
-- Memory-entity links: connects memories to the entities they mention
-- This is the bridge between the two stores
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id   UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id   UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation    TEXT DEFAULT 'mentions', -- mentions, about, authored_by, etc.
    created_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (memory_id, entity_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_memory ON memory_entities(memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities(entity_id);

-- ============================================================
-- Comments
-- ============================================================
COMMENT ON TABLE entities IS 'Knowledge graph entities — structured nodes representing people, projects, organizations, etc.';
COMMENT ON TABLE entity_edges IS 'Typed directed edges between entities — worksFor, dependsOn, isPartOf, etc.';
COMMENT ON TABLE memory_entities IS 'Bridge table linking memories to the entities they reference. Enables: given an entity, find all memories about it; given a memory, find all entities it mentions.';
