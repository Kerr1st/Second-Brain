-- Migration 013: task-ready context exposure and outcome receipts.

CREATE TABLE context_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    objective TEXT NOT NULL,
    semantic_project TEXT,
    source_system TEXT NOT NULL,
    repository TEXT,
    returned_memory_ids UUID[] NOT NULL DEFAULT '{}',
    used_memory_ids UUID[] NOT NULL DEFAULT '{}',
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending', 'followed', 'corrected', 'not_used', 'unknown')),
    outcome_note TEXT,
    correction_episode_id UUID REFERENCES memories(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    evaluated_at TIMESTAMPTZ
);

CREATE INDEX idx_context_receipts_created_at
    ON context_receipts (created_at DESC);

CREATE INDEX idx_context_receipts_project
    ON context_receipts (semantic_project, created_at DESC);
