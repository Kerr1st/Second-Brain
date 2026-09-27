-- Local lineage reserves 015 for the divergent upstream fork. No corpus dedup,
-- destructive chunk rewrite, provider calls or role changes occur here.
ALTER TABLE dream_cycle_runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION;
ALTER TABLE dream_cycle_runs ADD CONSTRAINT dream_cycle_runs_cost_nonnegative
    CHECK (cost_usd IS NULL OR (cost_usd >= 0 AND cost_usd < 'Infinity'::float8)) NOT VALID;

CREATE INDEX IF NOT EXISTS idx_memories_parent_id ON memories(parent_id);
CREATE INDEX IF NOT EXISTS idx_memories_source_created ON memories(source_type, created_at DESC)
    WHERE parent_id IS NULL;
DO $$ BEGIN
    IF to_regclass('idx_memories_search_vector') IS NULL AND to_regclass('memories_search_vector_idx') IS NOT NULL THEN
        ALTER INDEX memories_search_vector_idx RENAME TO idx_memories_search_vector;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_memories_search_vector ON memories USING gin(search_vector);
DROP INDEX IF EXISTS idx_entities_name;
DROP INDEX IF EXISTS idx_entities_embedding;
DROP INDEX IF EXISTS idx_memories_encoding_context;

-- Correct historical premature expiry before readers filter expired links.
UPDATE memory_relationships SET expired_at = NULL
WHERE expired_at IS NOT NULL AND (
    relation_type IN ('derived_from', 'superseded_by')
    OR expired_at - created_at < interval '90 days');
ALTER TABLE memory_relationships ADD CONSTRAINT relationship_provenance_permanent
    CHECK (relation_type NOT IN ('derived_from', 'superseded_by') OR expired_at IS NULL);

-- Existing legacy values remain inspectable until a separately qualified backfill.
-- NOT VALID enforces new writes without pretending existing rows were validated.
ALTER TABLE memories ADD CONSTRAINT memories_type_vocabulary CHECK (type IN
    ('idea','synthesis','research','insight','question','decision','priority','project',
     'connection','source','schema','correction_episode')) NOT VALID;
ALTER TABLE memories ADD CONSTRAINT memories_status_vocabulary
    CHECK (status IN ('active','superseded','archived')) NOT VALID;
ALTER TABLE memories ADD CONSTRAINT memories_class_vocabulary
    CHECK (mem_class IN ('semantic','episodic','procedural','source')) NOT VALID;
ALTER TABLE memory_relationships ADD CONSTRAINT relationships_type_vocabulary
    CHECK (relation_type IN ('supports','contradicts','extends','inspires','derived_from','related_to','superseded_by')) NOT VALID;

-- Maintenance resumability is committed with each rewrite, independently of
-- a local manifest/checkpoint file. No payload text is kept in the ledger.
CREATE TABLE corpus_maintenance (
    manifest_sha256 TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    details JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY(manifest_sha256, operation_key)
);

-- Retire unreachable deferred state without erasing historical audit values.
UPDATE dream_cycle_runs SET stats=COALESCE(stats,'{}'::jsonb) ||
    jsonb_build_object('legacy_candidates_deferred',candidates_deferred)
    WHERE candidates_deferred IS NOT NULL AND candidates_deferred <> 0;
UPDATE dream_cycle_candidates SET candidate_json=COALESCE(candidate_json,'{}'::jsonb) ||
    jsonb_build_object('legacy_deferred_twice_rejected',true)
    WHERE deferred_twice_rejected IS TRUE;
ALTER TABLE dream_cycle_runs DROP COLUMN candidates_deferred;
ALTER TABLE dream_cycle_candidates DROP COLUMN deferred_twice_rejected;

-- Historical channel labels verified read-only on September 26; manual/API
-- provenance labels are explicit. Existing legacy rows are not auto-rewritten.
ALTER TABLE memories ADD CONSTRAINT memories_source_vocabulary
    CHECK (source_type IN ('api_browser','api_email','api_slack','article','browser_history','codex_task','distilled_agent_task','distilled_chat','dream_cycle','file','kiro_cli_chat','kiro_ide_chat','manual','outlook_calendar','outlook_email','quick_desktop','quick_desktop_anti_pattern','quick_desktop_chat','quick_desktop_decision','quick_desktop_doc','quick_desktop_feed','quick_desktop_people','quick_desktop_preference','quick_desktop_procedure','quick_desktop_profile','quick_desktop_session_event','quick_desktop_source','quick_desktop_terminology','quick_desktop_tool_strategy','slack','weekly_digest','youtube')) NOT VALID;
