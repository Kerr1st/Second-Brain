-- Migration 011: backend provenance on dream cycle runs
-- Records which (backend, model, effort) produced each run, per role, so a
-- now-backend-dependent decision stays auditable across a backend swap or the
-- Mac Mini cutover (Kiro-Opus era vs Claude-Code-on-Bedrock era).
-- Shape: {role: {backend, model, effort}}. NULL for runs predating this column.
-- See docs/MODEL-BACKENDS.md.
ALTER TABLE dream_cycle_runs ADD COLUMN IF NOT EXISTS backend_provenance JSONB;
