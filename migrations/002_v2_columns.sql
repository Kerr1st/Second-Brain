-- Migration 002: Retrieval Quality v2 columns
-- Adds mem_class, project, and last_accessed_at columns to the memories
-- table for memory classification, project scoping, and spaced retrieval.
-- All statements are idempotent (IF NOT EXISTS guards).

-- ============================================================
-- Column: mem_class
-- Categorical label: semantic | episodic | procedural
-- Research basis: Tulving's memory taxonomy — semantic memories
-- (facts, principles, decisions) are boosted over episodic (raw
-- logs, sources) in reranking to surface abstracted knowledge.
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS mem_class TEXT;

-- ============================================================
-- Column: project
-- Optional project tag for encoding-specificity scoping.
-- Memories tagged with a project are penalised when retrieved
-- from a different project context, preventing convention
-- pollution across codebases.
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS project TEXT;

-- ============================================================
-- Column: last_accessed_at
-- Timestamp of the most recent retrieval, used to compute the
-- spacing bonus (min(1, days_since/7)). Based on spaced
-- retrieval research — memories accessed at healthy intervals
-- receive full reinforcement, while same-session re-retrievals
-- are dampened to prevent popularity bias.
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;

-- ============================================================
-- Indexes for query performance
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_mem_class ON memories (mem_class);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories (project);
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed_at ON memories (last_accessed_at DESC);

-- ============================================================
-- Column comments documenting research basis
-- ============================================================
COMMENT ON COLUMN memories.mem_class IS
    'Memory classification (semantic/episodic/procedural) per Tulving taxonomy. '
    'Semantic memories receive a +0.04 rerank boost; procedural +0.02; episodic +0.00.';

COMMENT ON COLUMN memories.project IS
    'Project tag for encoding-specificity scoping. Cross-project results receive '
    'a -0.15 rerank penalty. NULL means universal knowledge (no penalty).';

COMMENT ON COLUMN memories.last_accessed_at IS
    'Timestamp of last retrieval, used for spaced retrieval bonus. '
    'spacing_bonus = min(1.0, days_since_last_access / 7.0). '
    'NULL defaults to bonus of 1.0 (full reinforcement).';
