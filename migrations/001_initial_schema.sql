CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    embedding vector(1024),
    tags TEXT[] DEFAULT '{}',
    source_url TEXT,
    source_type TEXT,
    metadata JSONB DEFAULT '{}',
    status TEXT DEFAULT 'active',
    confidence FLOAT DEFAULT 1.0,
    parent_id UUID REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    search_vector TSVECTOR,
    access_count INTEGER DEFAULT 0
);

CREATE TABLE memory_relationships (
    source_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    target_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (source_id, target_id, relation_type)
);

-- Vector similarity search
CREATE INDEX ON memories USING hnsw (embedding vector_cosine_ops);

-- Full-text search (BM25)
CREATE INDEX ON memories USING gin (search_vector);

-- Common query patterns
CREATE INDEX ON memories (type, status);
CREATE INDEX ON memories USING gin (tags);
CREATE INDEX ON memories USING gin (metadata);
CREATE INDEX ON memories (created_at DESC);
CREATE INDEX ON memories (source_type);

-- Auto-populate search_vector on insert/update
CREATE OR REPLACE FUNCTION memories_search_vector_update() RETURNS trigger AS $$
BEGIN
  NEW.search_vector := to_tsvector('english', coalesce(NEW.title,'') || ' ' || coalesce(NEW.content,''));
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_memories_search_vector
  BEFORE INSERT OR UPDATE OF title, content ON memories
  FOR EACH ROW EXECUTE FUNCTION memories_search_vector_update();
