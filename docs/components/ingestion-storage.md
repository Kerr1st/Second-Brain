# Ingestion & Storage Component

> **Status: canonical component contract.** Last reviewed: 2026-07-23.

Ingestion & Storage turns normalized evidence into durable memories,
relationships, embeddings, and transactional processing state in PostgreSQL.

## Boundary

This component owns:

- metadata parsing and content chunking;
- memory classification, depth scoring, and project normalization;
- embedding generation;
- creation and update of memories and relationships;
- parent-child and `derived_from` provenance relationships;
- database migrations and persisted invariants; and
- atomic writes for multi-record semantic results.

It does not own source access, search ranking, Dream Cycle judgment, user
delivery, or model-provider selection.

## Contract

The generic ingestion contract is:

```python
ingest_content(
    content,
    source_type,
    source_url=None,
    project=None,
)
```

The input is normalized content plus source metadata. The output is durable
PostgreSQL state:

- an unembedded source parent when the content is chunked;
- embedded searchable child memories;
- classification and depth metadata;
- source and semantic project metadata kept distinct; and
- typed relationships to parents, provenance, and discovered neighbors.

Agent Task capture has stronger invariants. A Captured Task stores ordered
Agent Turns, Topic Segments store ordered supporting turns, and derived
decisions, insights, or Correction Episodes retain durable `derived_from`
edges.

## Generic ingestion flow

```text
normalized content
  → parse metadata
  → split into coherent chunks
  → classify and score depth
  → normalize semantic project
  → generate embeddings
  → write parent and children
  → discover relationships
```

## Transaction and retry behavior

Database writes use explicit transactions. The Codex Task Semantic Pass stores
all Topic Segments, derived memories, relationships, and the updated Semantic
Processing Cursor atomically. A failure retains the earlier source capture but
keeps no partial semantic result.

The migration runner records applied versions and is safe to rerun. Tests must
use the isolated test database configured by `TEST_DB_NAME`.

## Current physical seams

The logical component boundary is clearer than the current code boundary:

- `src/ingest.py` owns the generic document ingestion pipeline.
- `src/capture/codex.py` directly performs the specialized Captured Task and
  Task Semantic Pass writes.
- `src/mcp_server.py::memory_create` composes another interactive write path.

The componentization roadmap tracks convergence on one shared storage
primitive. These current seams must remain visible until that refactor is
implemented and proven.

## Entry points and data

| Purpose | Entry point |
|---|---|
| Generic ingestion | `src/ingest.py::ingest_content` |
| Database CRUD | `src/db.py` |
| Embeddings | `src/embeddings.py::generate_embedding` |
| Classification | `src/classify.py::classify_memory` |
| Depth scoring | `src/depth.py::compute_depth_score` |
| Project normalization | `src/project.py::normalize_project_tag` |
| Schema changes | `migrations/*.sql` and `migrations/migrate.sh` |

Primary persisted structures are `memories`, `memory_relationships`,
`dream_cycle_runs`, `dream_cycle_candidates`, and `express_feedback`. The
entity knowledge-graph tables are retained but dormant.

## Tests

- `tests/test_ingest_v2.py`
- `tests/test_ingest_doc_chunks.py`
- `tests/test_ingest_eventlog.py`
- `tests/test_db.py`
- `tests/test_migration.py`
- `tests/test_schema_migration_runner.py`
- `tests/test_agent_task_schema.py`
- `tests/test_test_database_safety.py`

## Related

- [Architecture Component Index](index.md)
- [Database schema](../user-guide/database-schema.md)
- [System architecture](../ARCHITECTURE.md#database-schema)
- [Componentization roadmap](../COMPONENTIZATION-PLAN.md)
- [ADR 0001: Store captured tasks in memories](../adr/0001-store-captured-tasks-in-memories.md)
- [ADR 0005: Use real Agent Task data in tests](../adr/0005-use-real-agent-task-data-throughout-testing.md)
