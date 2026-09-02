-- The active vector column is one vector space. Provider changes require a new migration;
-- arbitrary identity strings are rejected so incompatible vectors cannot be mixed silently.

ALTER TABLE memories ADD CONSTRAINT memories_active_embedding_space
CHECK (
    embedding_space IS NULL
    OR embedding_space = 'ollama:bge-m3:1024'
);
