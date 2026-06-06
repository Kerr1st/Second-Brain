-- Migration 010: Express feedback (delivery preferences)
-- Lets the user shape WHAT the Express briefing surfaces (not what the dream cycle
-- synthesizes) via the `brief` command's --useful / --less / --mute flags, targeting
-- a specific item, a kind (insight|contradiction|resurface|digest|question), or a
-- topic/project. Gradient: useful (boost) → less (soft down-weight) → mute (hard hide).
-- Latest signal per target wins (upsert on the unique key).

CREATE TABLE IF NOT EXISTS express_feedback (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type TEXT NOT NULL,                     -- 'item' | 'kind' | 'topic'
    target_key  TEXT NOT NULL,                     -- item id (or 'src:tgt'), kind name, or topic/project
    signal      TEXT NOT NULL,                     -- 'useful' | 'less' | 'mute'
    weight      DOUBLE PRECISION NOT NULL DEFAULT 0, -- soft ranking effect (mute is handled as a hard filter)
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (target_type, target_key)
);

CREATE INDEX IF NOT EXISTS idx_express_feedback_type ON express_feedback (target_type);
