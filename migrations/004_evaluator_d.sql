-- Migration 004: Add fourth evaluator (Methodologist) columns
-- Supports Byzantine fault tolerance with 4-evaluator binary consensus panel.
-- Lamport 3f+1 bound: 4 evaluators tolerate 1 faulty (hallucinating) evaluator.

ALTER TABLE dream_cycle_candidates
    ADD COLUMN IF NOT EXISTS evaluator_d_verdict TEXT,
    ADD COLUMN IF NOT EXISTS evaluator_d_reasoning TEXT;

COMMENT ON COLUMN dream_cycle_candidates.evaluator_d_verdict IS
    'Methodologist evaluator verdict (ACCEPT/REJECT). Fourth evaluator for BFT.';
COMMENT ON COLUMN dream_cycle_candidates.evaluator_d_reasoning IS
    'Methodologist evaluator reasoning text.';
