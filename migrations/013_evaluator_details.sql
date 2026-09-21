-- Migration 013: graded evaluator detail per candidate
-- The panel's verdict schema now carries a 0-100 score and the defects each seat
-- found, split into blocking (fabrication, contradiction, misattribution, uncited)
-- and advisory (overreach, overlap, timing, depth). Stored per seat so the digest
-- can show why a candidate passed or failed and so the panel's calibration can be
-- measured without re-running it. Shape: {role: {score, blocking_issues,
-- advisory_issues}}. NULL for candidates judged before this column existed.
-- The pre-panel grounding audit's findings live in candidate_json.revision.
-- See docs/PLAN-panel-acceptance.md.
ALTER TABLE dream_cycle_candidates ADD COLUMN IF NOT EXISTS evaluator_details JSONB;
