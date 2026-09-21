-- Migration 014: skim stage — one structured record per arriving document
-- (docs/PLAN-skim-stage.md §2 "Storage"). A cheap, faithful model (Amazon Nova 2
-- Lite on Bedrock) reads every parent-level arrival in a run's window and emits a
-- skim record (summary, people, entities, topics, decisions / open questions with
-- verbatim quotes, novelty, salience, contradiction_hint, doc_kind) so the Explorer
-- can see every arrival instead of a 4-sample manifest.
--
-- One row per (memory_id, model, prompt_version): the same document is never
-- skimmed twice with the same model and prompt (idempotent across runs); a new
-- prompt version or model gets its own rows and the old ones stay for comparison.
-- run_id is the dream-cycle run that paid for the row (NULL for probes and
-- backfills; no FK so the table can outlive a run row). skim is the validated
-- record in the shape of src/skim/schema.py SKIM_SCHEMA. cluster_id is reserved
-- for Phase 3 (greedy cosine clustering of the window). truncated marks a
-- document that was cut to the runner's max_doc_chars before the model saw it.
-- Deleting a memory deletes its skims.
CREATE TABLE IF NOT EXISTS memory_skim (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    run_id         UUID,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    skim           JSONB NOT NULL,
    cluster_id     TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       NUMERIC(10, 6),
    truncated      BOOLEAN DEFAULT false,
    created_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (memory_id, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_memory_skim_run ON memory_skim(run_id);
CREATE INDEX IF NOT EXISTS idx_memory_skim_created ON memory_skim(created_at);

-- Run-level stats beyond the three candidate counters that have their own
-- columns. complete_run() writes every stats key without a column here — first
-- the skim counters (skim_docs, skim_skimmed, skim_reused, skim_failed,
-- skim_cost_usd) — so a run's coverage and cost stay auditable without a new
-- column per counter. NULL for runs before this migration and for the main
-- tree's panel-acceptance runs, which do not write it (additive; safe).
ALTER TABLE dream_cycle_runs ADD COLUMN IF NOT EXISTS stats JSONB;
