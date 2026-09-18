-- Migration 012: attention ledger — persist Explorer slices with their memory ids
-- Until now dream_cycle_runs.explorer_output kept only {name, strategy, memory_count}
-- per slice; the memory ids were discarded, so what the Explorer actually looked at
-- could not be audited after the fact (2026-09-17: three runs examined only the
-- Second Brain cluster while 1,000+ new memories from other sources went untouched,
-- and this had to be reconstructed from candidate citations).
--
-- memory_ids is TEXT[] not UUID[]: the Explorer is an LLM and occasionally emits
-- ids that do not exist; the ledger records exactly what was asked for and
-- resolves separately. metrics holds the per-slice attention metrics computed at
-- run time (see src/dream_cycle/ledger.py). origin distinguishes slices produced
-- by the Explorer from ones the orchestrator composed ('guaranteed_arrivals') and
-- from baseline rows backfilled out of candidate citations ('backfill').
CREATE TABLE IF NOT EXISTS dream_cycle_slices (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES dream_cycle_runs(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    strategy      TEXT NOT NULL DEFAULT '',
    hypothesis    TEXT NOT NULL DEFAULT '',
    memory_ids    TEXT[] NOT NULL DEFAULT '{}',
    memory_titles TEXT[] NOT NULL DEFAULT '{}',
    origin        TEXT NOT NULL DEFAULT 'explorer',
    metrics       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, position)
);

CREATE INDEX IF NOT EXISTS idx_dream_cycle_slices_run ON dream_cycle_slices(run_id);
