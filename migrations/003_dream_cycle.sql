-- Migration 003: Dream Cycle tables
-- Adds dream_cycle_runs and dream_cycle_candidates tables for the
-- four-agent autonomous learning pipeline, plus temporal awareness
-- on memory_relationships.

-- ============================================================
-- Table: dream_cycle_runs
-- Tracks each dream cycle execution with run metadata and stats.
-- ============================================================
CREATE TABLE dream_cycle_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type TEXT NOT NULL,                    -- scheduled, post_learn, session_start, user_triggered
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    explorer_output JSONB,                     -- memory slices assembled by Explorer
    explorer_feedback_injected TEXT,            -- "Lessons from recent cycles" text block
    candidates_generated INTEGER,
    candidates_accepted INTEGER,
    candidates_deferred INTEGER,
    candidates_rejected INTEGER,
    digest TEXT                                 -- human-readable summary markdown
);

-- ============================================================
-- Table: dream_cycle_candidates
-- Stores every candidate insight with individual evaluator
-- verdicts and final consensus result.
-- ============================================================
CREATE TABLE dream_cycle_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES dream_cycle_runs(id),
    candidate_json JSONB,                      -- Thinker's full output for this candidate
    operation TEXT,                             -- CREATE, UPDATE, SUPERSEDE
    target_memory_id UUID,                     -- for UPDATE/SUPERSEDE operations
    schema_operation TEXT,                      -- assimilation, accommodation
    evaluator_a_verdict TEXT,
    evaluator_a_reasoning TEXT,
    evaluator_b_verdict TEXT,
    evaluator_b_reasoning TEXT,
    evaluator_c_verdict TEXT,
    evaluator_c_reasoning TEXT,
    final_verdict TEXT,                         -- ACCEPTED, DEFERRED, REJECTED
    created_memory_id UUID REFERENCES memories(id),
    user_rejected_at TIMESTAMPTZ,
    user_rejection_reason TEXT,
    deferred_twice_rejected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Indexes for query performance
-- ============================================================
CREATE INDEX idx_dream_cycle_runs_run_type ON dream_cycle_runs (run_type);
CREATE INDEX idx_dream_cycle_candidates_final_verdict ON dream_cycle_candidates (final_verdict);
CREATE INDEX idx_dream_cycle_candidates_created_at ON dream_cycle_candidates (created_at);
CREATE INDEX idx_dream_cycle_candidates_run_id ON dream_cycle_candidates (run_id);

-- ============================================================
-- Temporal awareness on memory_relationships (Zep bi-temporal model)
-- ============================================================
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;
