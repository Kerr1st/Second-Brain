-- Migration 012: minimal Codex Task identity and durable provenance
--
-- Capture timestamps, Agent Turns, the semantic cursor, workspace, Codex
-- Project, Git context, and Attachment Descriptors live in memory metadata.
-- No capture revisions, hashes, stage state, or telemetry columns are needed.

CREATE UNIQUE INDEX uq_memories_codex_captured_task
    ON memories (source_url)
    WHERE source_type = 'codex_task'
      AND parent_id IS NULL
      AND metadata @> '{"record_kind": "captured_task"}'::jsonb;

CREATE UNIQUE INDEX uq_memories_codex_topic_segment_index
    ON memories (parent_id, (metadata->>'segment_index'))
    WHERE source_type = 'codex_task'
      AND parent_id IS NOT NULL
      AND metadata @> '{"record_kind": "topic_segment"}'::jsonb;

-- Exact Provenance is evidence, not a temporal association. Existing
-- derived_from edges are repaired once and future expiry is prohibited.
UPDATE memory_relationships
SET expired_at = NULL
WHERE relation_type = 'derived_from' AND expired_at IS NOT NULL;

ALTER TABLE memory_relationships
    ADD CONSTRAINT chk_relationships_derived_from_not_expired
        CHECK (relation_type <> 'derived_from' OR expired_at IS NULL);

CREATE INDEX idx_relationships_derived_from_target
    ON memory_relationships (target_id, source_id)
    WHERE relation_type = 'derived_from';
