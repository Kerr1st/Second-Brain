---
title: "Database Schema"
type: reference
---

# Database Schema

Authoritative schema for the Second Brain PostgreSQL database. The schema is defined by migrations `migrations/000_migrations_table.sql` through `migrations/012_agent_task_capture.sql` and applied by `migrations/migrate.sh`.

## `schema_migrations`

Version-tracking table. One row per applied migration.

| Column | Type | Notes |
|--------|------|-------|
| `version` | `TEXT` | **PK.** Migration identifier (e.g. `001`) |
| `applied_at` | `TIMESTAMPTZ` | Defaults to `now()` |

## `memories`

Primary knowledge store. Each row is one *memory* (content + embedding + metadata + relationships).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | **PK.** `gen_random_uuid()` |
| `type` | `TEXT NOT NULL` | Memory kind, including `research`, `synthesis`, `idea`, `connection`, `priority`, `question`, `insight`, `decision`, `correction_episode`, `project`, and `source` |
| `title` | `TEXT NOT NULL` | Short descriptor |
| `content` | `TEXT NOT NULL` | Full body text |
| `summary` | `TEXT` | Optional condensed version |
| `embedding` | `vector(1024)` | Amazon Bedrock Titan v2 embedding |
| `tags` | `TEXT[]` | Default `'{}'` |
| `source_url` | `TEXT` | Origin URL if applicable |
| `source_type` | `TEXT` | Capture channel (e.g. `youtube`, `kiro_cli_chat`, `quick_desktop_doc`) |
| `metadata` | `JSONB` | Default `'{}'`. Extensible key-value store |
| `status` | `TEXT` | Default `'active'`. Values: `active`, `explored`, `archived`, `superseded`, `user_rejected` |
| `confidence` | `FLOAT` | Default `1.0` |
| `parent_id` | `UUID` | FK → `memories(id)`. Optional hierarchy |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |
| `updated_at` | `TIMESTAMPTZ` | Default `now()` |
| `search_vector` | `TSVECTOR` | Auto-populated by trigger (title + questions = weight A; remaining content = weight B) |
| `access_count` | `INTEGER` | Default `0`. Incremented on retrieval |
| `mem_class` | `TEXT` | Memory class: `semantic`, `episodic`, or `procedural` (migration 002) |
| `project` | `TEXT` | Project scope tag (migration 002) |
| `last_accessed_at` | `TIMESTAMPTZ` | Last retrieval timestamp (migration 002) |
| `encoding_context` | `TEXT` | Cognitive context at creation time (migration 006) |

Codex capture keeps source timestamps, capture time, ordered Agent Turns, Attachment Descriptors,
workspace and Git provenance, and the Semantic Processing Cursor inside `metadata`. Migration 012
adds no capture revision, content hash, or processing telemetry columns. It adds only uniqueness
for a Captured Task's native `codex://<thread-id>` identity, uniqueness for Topic Segment order
within that task, and permanence for `derived_from` provenance edges.

Task Distillation stores decisions and insights with `mem_class='semantic'`. A
`correction_episode` is stored with `mem_class='episodic'`, `source_type='distilled_agent_task'`,
and `metadata.supporting_turn_ids`. Its permanent `derived_from` relationship targets the
containing Topic Segment. The type is represented in the existing unconstrained `type` column, so
Build 1 requires no additional schema migration.

### Trigger

`trg_memories_search_vector` (BEFORE INSERT OR UPDATE OF title, content) calls `memories_search_vector_update()` which builds a weighted tsvector — title and "Questions this answers:" lines at weight A, remaining content at weight B (migration 005).

## `memory_relationships`

Typed directed edges between memories.

| Column | Type | Notes |
|--------|------|-------|
| `source_id` | `UUID` | **PK (composite).** FK → `memories(id)` ON DELETE CASCADE |
| `target_id` | `UUID` | **PK (composite).** FK → `memories(id)` ON DELETE CASCADE |
| `relation_type` | `TEXT NOT NULL` | **PK (composite).** e.g. `supports`, `contradicts`, `derived_from` |
| `note` | `TEXT` | Optional annotation |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |
| `expired_at` | `TIMESTAMPTZ` | Temporal expiry (migration 003); must remain `NULL` for `derived_from` provenance (migration 012) |

## `dream_cycle_runs`

Tracks each dream cycle execution.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | **PK.** `gen_random_uuid()` |
| `run_type` | `TEXT NOT NULL` | `scheduled`, `post_learn`, `session_start`, `user_triggered` |
| `started_at` | `TIMESTAMPTZ` | Default `now()` |
| `completed_at` | `TIMESTAMPTZ` | Set when run finishes |
| `explorer_output` | `JSONB` | Memory slices assembled by Explorer |
| `explorer_feedback_injected` | `TEXT` | "Lessons from recent cycles" text block |
| `candidates_generated` | `INTEGER` | |
| `candidates_accepted` | `INTEGER` | |
| `candidates_deferred` | `INTEGER` | |
| `candidates_rejected` | `INTEGER` | |
| `digest` | `TEXT` | Human-readable summary (Markdown) |

### Indexes

| Index | Method | Column(s) |
|-------|--------|-----------|
| `idx_dream_cycle_runs_run_type` | btree | `run_type` |

## `dream_cycle_candidates`

Every candidate insight with individual evaluator verdicts and final consensus.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | **PK.** `gen_random_uuid()` |
| `run_id` | `UUID` | FK → `dream_cycle_runs(id)` |
| `candidate_json` | `JSONB` | Thinker's full output |
| `operation` | `TEXT` | `CREATE`, `UPDATE`, `SUPERSEDE` |
| `target_memory_id` | `UUID` | For UPDATE/SUPERSEDE operations |
| `schema_operation` | `TEXT` | `assimilation` or `accommodation` |
| `evaluator_a_verdict` | `TEXT` | Skeptic: ACCEPT/REJECT |
| `evaluator_a_reasoning` | `TEXT` | |
| `evaluator_b_verdict` | `TEXT` | User Advocate: ACCEPT/REJECT |
| `evaluator_b_reasoning` | `TEXT` | |
| `evaluator_c_verdict` | `TEXT` | Epistemologist: ACCEPT/REJECT |
| `evaluator_c_reasoning` | `TEXT` | |
| `evaluator_d_verdict` | `TEXT` | Methodologist: ACCEPT/REJECT (migration 004) |
| `evaluator_d_reasoning` | `TEXT` | (migration 004) |
| `final_verdict` | `TEXT` | `ACCEPTED`, `DEFERRED`, `REJECTED` |
| `created_memory_id` | `UUID` | FK → `memories(id)`. Set on acceptance |
| `user_rejected_at` | `TIMESTAMPTZ` | |
| `user_rejection_reason` | `TEXT` | |
| `deferred_twice_rejected` | `BOOLEAN` | Default `FALSE` |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |

> [!NOTE]
> **Legacy / vestigial columns.** The original dream cycle used 3 evaluators with a three-state outcome (ACCEPT / DEFERRED / REJECT). Since migration 004 added a fourth evaluator, the panel is **binary**: `final_verdict` is written only as `ACCEPTED` or `REJECTED`. The `DEFERRED` value, `dream_cycle_runs.candidates_deferred` (stays `0`), and `deferred_twice_rejected` (stays `FALSE`) remain in the schema but are no longer produced. See [DESIGN-DECISIONS.md](../DESIGN-DECISIONS.md).

### Indexes

| Index | Method | Column(s) |
|-------|--------|-----------|
| `idx_dream_cycle_candidates_run_id` | btree | `run_id` |
| `idx_dream_cycle_candidates_final_verdict` | btree | `final_verdict` |
| `idx_dream_cycle_candidates_created_at` | btree | `created_at` |

## `express_feedback`

Delivery preferences for Express briefings (migration 010). Latest signal per target wins (upsert on unique key).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | **PK.** `gen_random_uuid()` |
| `target_type` | `TEXT NOT NULL` | `'item'`, `'kind'`, or `'topic'` |
| `target_key` | `TEXT NOT NULL` | Item id, kind name, or topic/project |
| `signal` | `TEXT NOT NULL` | `'useful'`, `'less'`, or `'mute'` |
| `weight` | `DOUBLE PRECISION` | Default `0`. Soft ranking effect (mute is a hard filter) |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |
| `updated_at` | `TIMESTAMPTZ` | Default `now()` |

**Unique constraint:** `(target_type, target_key)`

### Indexes

| Index | Method | Column(s) |
|-------|--------|-----------|
| `idx_express_feedback_type` | btree | `target_type` |

## `entities`, `entity_edges`, `memory_entities` — DORMANT

> [!WARNING]
> These tables were imported from Quick Desktop and are **not read by retrieval, synthesis, or Express**. They are retained for possible future use. The entity knowledge graph is ~99.5% disconnected from the memory store.

### `entities`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | **PK.** `gen_random_uuid()` |
| `category` | `TEXT NOT NULL` | Person, Organization, Project, Product, DefinedTerm, etc. |
| `name` | `TEXT NOT NULL` | |
| `summary` | `TEXT` | |
| `properties` | `JSONB` | Default `'{}'` |
| `source_type` | `TEXT` | |
| `embedding` | `vector(1536)` | **Note:** 1536 dimensions (vs `memories.embedding` at 1024) |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |
| `updated_at` | `TIMESTAMPTZ` | Default `now()` |

**Unique constraint:** `(category, name)`

### `entity_edges`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | **PK.** `gen_random_uuid()` |
| `from_entity` | `UUID NOT NULL` | FK → `entities(id)` ON DELETE CASCADE |
| `to_entity` | `UUID NOT NULL` | FK → `entities(id)` ON DELETE CASCADE |
| `relation` | `TEXT NOT NULL` | e.g. `worksFor`, `dependsOn`, `isPartOf` |
| `weight` | `REAL` | Default `1.0` |
| `properties` | `JSONB` | Default `'{}'` |
| `source_type` | `TEXT` | |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |

**Unique constraint:** `(from_entity, to_entity, relation)`

### `memory_entities`

Bridge table linking memories to entities they mention.

| Column | Type | Notes |
|--------|------|-------|
| `memory_id` | `UUID NOT NULL` | **PK (composite).** FK → `memories(id)` ON DELETE CASCADE |
| `entity_id` | `UUID NOT NULL` | **PK (composite).** FK → `entities(id)` ON DELETE CASCADE |
| `relation` | `TEXT` | **PK (composite).** Default `'mentions'` |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |

### Knowledge-graph indexes

| Index | Method | Column(s) |
|-------|--------|-----------|
| `idx_entities_category` | btree | `category` |
| `idx_entities_name` | GIN | `to_tsvector('english', name)` |
| `idx_entities_embedding` | ivfflat | `embedding vector_cosine_ops` (lists=50) |
| `idx_entity_edges_from` | btree | `from_entity` |
| `idx_entity_edges_to` | btree | `to_entity` |
| `idx_entity_edges_relation` | btree | `relation` |
| `idx_memory_entities_memory` | btree | `memory_id` |
| `idx_memory_entities_entity` | btree | `entity_id` |

## Indexes on `memories`

| Index | Method | Column(s) / Options | Notes |
|-------|--------|---------------------|-------|
| `idx_memories_embedding` | HNSW | `embedding vector_cosine_ops` (m=32, ef_construction=200) | Rebuilt in migration 009 for recall; database-level `hnsw.ef_search = 200` |
| *(unnamed)* | GIN | `search_vector` | Full-text search |
| *(unnamed)* | btree | `(type, status)` | |
| *(unnamed)* | GIN | `tags` | |
| *(unnamed)* | GIN | `metadata` | |
| *(unnamed)* | btree | `created_at DESC` | |
| *(unnamed)* | btree | `source_type` | |
| `idx_memories_mem_class` | btree | `mem_class` | Migration 002 |
| `idx_memories_project` | btree | `project` | Migration 002 |
| `idx_memories_last_accessed_at` | btree | `last_accessed_at DESC` | Migration 002 |
| `idx_memories_encoding_context` | GIN | `to_tsvector('english', coalesce(encoding_context, ''))` | Migration 006 |
| `idx_memories_type_schema` | btree | `type` WHERE `type = 'schema'` | **INERT** — partial index for a schema-type feature with 0 rows currently matching. Migration 007 |
| `idx_relationships_derived_from` | btree | `(relation_type, source_id)` WHERE `relation_type = 'derived_from'` | **INERT** — supports schema feature; no schema rows exist. Migration 007 |

## Related

- [Reference](reference.md) — MCP tools, CLI commands, memory and relationship types
- [Index](index.md) — user guide navigation
