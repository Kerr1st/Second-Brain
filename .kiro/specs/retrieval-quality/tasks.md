# Implementation Plan: Retrieval Quality

## Overview

Layer 1 retrieval improvements for the Second Brain MCP server. Adds test infrastructure, schema migration, spaced retrieval, memory classification, depth scoring, project scoping, relationship discovery, and temporal contiguity. All reranking changes target `src/search.py` (extracted from `db.py` in the data-layer-decomposition spec). The existing 174 tests (157 original + 17 property tests) must continue to pass after each step.

## Tasks

- [x] 1. Test infrastructure — shared fixtures and baseline regression tests
  - [x] 1.1 Create `pytest.ini` with test discovery config and `tests/conftest.py` with shared fixtures
    - `pytest.ini`: testpaths, python_files, python_functions, addopts for verbose output
    - `tests/conftest.py`: Test_DB fixture (session-scoped, creates `memory_bank_test` DB, applies all migrations from `migrations/`, overrides `db.DB_CONFIG`), `clean_tables` fixture (function-scoped, truncates `memories` and `memory_relationships`), `mock_embedding` fixture (deterministic 1024-dim vector from input hash, no Bedrock calls), `sample_memory_factory` fixture (creates memories with known content via `create_memory`)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.7, 1.8_

  - [x] 1.2 Create `tests/test_db.py` with baseline CRUD regression tests and `tests/test_search.py` with baseline search regression tests
    - `tests/test_db.py` (data-access layer only):
      - `test_create_and_get_memory` — create_memory returns valid UUID, get_memory retrieves it
      - `test_search_similar_returns_results` — insert memory with embedding, search_similar finds it
      - `test_create_and_get_relationship` — create_relationship persists, get_relationships retrieves
    - `tests/test_search.py` (search/ranking layer — complements existing `test_search_properties.py` structural tests with behavioral baselines):
      - `test_hybrid_search_returns_results` — insert memory with embedding, hybrid_search finds it
      - `test_rerank_preserves_order_for_exact_match` — exact title match ranks first
      - `test_increment_access_count` — bumps access_count by 1
    - _Requirements: 1.5_

  - [x] 1.3 Create `tests/test_mcp_server.py` with MCP smoke tests
    - `test_memory_create_returns_id` — memory_create returns a string containing a UUID
    - `test_memory_search_returns_list` — memory_search returns a list
    - `test_memory_create_depth_warning` — shallow content triggers depth warning
    - Mock `generate_embedding` to use deterministic fake embeddings
    - _Requirements: 1.6_

  - [x] 1.4 Write property test for Embedding Mock determinism
    - **Property 1: Embedding Mock Determinism**
    - For any input string, mock returns 1024-dim list of floats; same input → same output
    - **Validates: Requirements 1.3**

- [x] 2. Checkpoint — verify all tests pass
  - Ensure all tests pass (174 existing + new baseline tests), ask the user if questions arise.

- [x] 3. Schema migration — add `mem_class`, `project`, `last_accessed_at` columns
  - [x] 3.1 Create `migrations/002_v2_columns.sql` with idempotent schema additions
    - `ALTER TABLE memories ADD COLUMN IF NOT EXISTS mem_class TEXT;`
    - `ALTER TABLE memories ADD COLUMN IF NOT EXISTS project TEXT;`
    - `ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;`
    - Create indexes: `idx_memories_mem_class`, `idx_memories_project`, `idx_memories_last_accessed_at`
    - Add column comments documenting the research basis
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.2 Update `src/db.py` — extend `create_memory()` and `ALLOWED_UPDATE_FIELDS`
    - Add `mem_class=None` and `project=None` parameters to `create_memory()` signature
    - Update INSERT statement to include `mem_class` and `project` columns
    - Add `"mem_class"`, `"project"`, `"last_accessed_at"` to `ALLOWED_UPDATE_FIELDS`
    - _Requirements: 2.5, 2.6_

  - [x] 3.3 Write property test for create_memory V2 fields round trip
    - **Property 2: create_memory V2 Fields Round Trip**
    - For any valid mem_class in {semantic, episodic, procedural} and any non-empty project string, create_memory + get_memory round-trips the values
    - **Validates: Requirements 2.5**

- [x] 4. Spaced retrieval — modulate reinforcement by spacing bonus
  - [x] 4.1 Modify `src/search.py` `increment_access_count()` to also set `last_accessed_at = now()`
    - Update SQL: `SET access_count = coalesce(access_count, 0) + 1, last_accessed_at = now()`
    - _Requirements: 3.1_

  - [x] 4.2 Modify `src/search.py` `rerank()` to apply spacing bonus to reinforcement
    - Read `last_accessed_at` from each result
    - Compute `spacing_bonus = min(1.0, days_since_last_access / 7.0)`, default 1.0 if NULL
    - Change reinforcement from `0.03 * log1p(access_count)` to `0.03 * log1p(access_count) * spacing_bonus`
    - Do NOT change base weights yet — that happens in task 9
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 4.3 Write property test for spacing bonus formula
    - **Property 3: Spacing Bonus Formula**
    - For any non-negative days_since_last_access: bonus = min(1.0, days/7.0); NULL → 1.0; 0 days → 0.0; 7+ days → 1.0
    - **Validates: Requirements 3.2, 3.3, 3.4, 3.5**

  - [x] 4.4 Write property test for spacing bonus ordering
    - **Property 9: Spacing Bonus Ordering**
    - Two memories identical except last_accessed_at — older access gets higher rerank score
    - **Validates: Requirements 3.7**

  - [x] 4.5 Write property test for increment_access_count updates last_accessed_at
    - **Property 16: increment_access_count Updates last_accessed_at**
    - After calling increment_access_count, each memory's last_accessed_at is non-NULL and recent
    - **Validates: Requirements 3.1**

- [x] 5. Memory classification — classify memories as semantic/episodic/procedural
  - [x] 5.1 Create `src/classify.py` with `classify_memory()` function
    - `classify_memory(type: str, source_type: str | None, content: str) -> str`
    - Priority rules: (1) procedural markers in content → "procedural", (2) type in SEMANTIC_TYPES → "semantic", (3) type == "source" → "episodic", (4) default → "episodic"
    - `SEMANTIC_TYPES = {"idea", "synthesis", "insight", "decision", "connection", "priority", "project", "question"}`
    - `PROCEDURAL_MARKERS` regex: step-by-step, "how to", numbered instruction lists
    - Pure function, no DB/network dependencies
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 5.2 Integrate classifier into `src/ingest.py` `ingest_content()` and `src/mcp_server.py` `memory_create()`
    - `ingest_content()`: call `classify_memory(type, source_type, content)`, pass `mem_class` to `create_memory()`
    - `memory_create()`: call `classify_memory(type, source_type, content)`, pass `mem_class` to `create_memory()`
    - _Requirements: 4.5, 4.6_

  - [x] 5.3 Add `mem_class_boost` to `src/search.py` `rerank()`
    - Read `mem_class` from each result
    - Add boost: semantic +0.04, procedural +0.02, episodic/NULL +0.00
    - Stacks with existing `type_boost`
    - _Requirements: 4.7, 4.8, 4.9_

  - [x] 5.4 Write property test for classifier correctness
    - **Property 4: Classifier Correctness**
    - For any (type, source_type, content), classify_memory returns correct class per priority rules
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4**

  - [x] 5.5 Write property test for classification ordering
    - **Property 10: Classification Ordering**
    - Two memories identical except mem_class — semantic ranks above episodic
    - **Validates: Requirements 4.10**

- [x] 6. Checkpoint — verify all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Depth scoring — numeric depth score replacing binary regex check
  - [x] 7.1 Create `src/depth.py` with `compute_depth_score()` function
    - `compute_depth_score(content: str) -> float` returning 0.0–1.0
    - Signals: causal connectors, code blocks, specific numbers, named tools, "Questions this answers:", content length, connection phrases
    - Move `_DEPTH_RE` pattern from `mcp_server.py` to `depth.py` (or build enhanced version)
    - Truncate analysis to first 10,000 characters for long content
    - Pure function, no DB/network dependencies
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 7.2 Integrate depth scorer into `src/ingest.py` and `src/mcp_server.py`
    - `ingest_content()`: compute depth_score, store in `metadata["depth_score"]`
    - `memory_create()`: compute depth_score, store in metadata, use numeric score for depth warnings instead of binary `_DEPTH_RE` check
    - Remove or replace `_DEPTH_RE` usage in `mcp_server.py`
    - _Requirements: 5.5, 5.6_

  - [x] 7.3 Add depth factor to `src/search.py` `rerank()`
    - Read `depth_score` from `metadata` JSONB (default 0.0 if missing)
    - Add `0.05 * depth_score` to rerank formula
    - _Requirements: 5.7, 5.8_

  - [x] 7.4 Write property test for depth score range invariant
    - **Property 5: Depth Score Range Invariant**
    - For any string input (empty, whitespace, unicode, long), compute_depth_score returns float in [0.0, 1.0]
    - **Validates: Requirements 5.1**

  - [x] 7.5 Write property test for rich content high depth score
    - **Property 6: Rich Content Produces High Depth Score**
    - Content with 2+ causal connectors, 1+ code block, "Questions this answers:" → score > 0.7
    - **Validates: Requirements 5.3**

  - [x] 7.6 Write property test for shallow content low depth score
    - **Property 7: Shallow Content Produces Low Depth Score**
    - Single short sentence (<50 chars), no signals → score < 0.3
    - **Validates: Requirements 5.4**

- [x] 8. Project scoping — project tags with cross-project penalty
  - [x] 8.1 Add `project` parameter to `src/ingest.py` `ingest_content()` and `src/mcp_server.py` `memory_create()` / `memory_search()`
    - `ingest_content()`: accept optional `project` param, pass to `create_memory()`
    - `memory_create()`: accept optional `project` param, pass to `create_memory()`
    - `memory_search()`: accept optional `project` param
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 8.2 Add `project` filter to `src/search.py` `hybrid_search()`
    - Accept optional `project` parameter
    - When set: add `AND (project = %s OR project IS NULL)` to WHERE clause
    - _Requirements: 6.4_

  - [x] 8.3 Add cross-project penalty to `src/search.py` `rerank()`
    - Accept optional `query_project` parameter
    - If `query_project` set and memory's `project` differs (non-NULL): apply -0.15 penalty
    - NULL project → no penalty (universal knowledge)
    - Matching project → no penalty
    - _Requirements: 6.5, 6.6, 6.7_

  - [x] 8.4 Wire project through `memory_search` in `src/mcp_server.py`
    - Pass `project` to `hybrid_search()` and `query_project` to `rerank()`
    - _Requirements: 6.3_

  - [x] 8.5 Write property test for hybrid_search project filtering
    - **Property 11: hybrid_search Project Filtering**
    - All results have matching project or NULL project; no mismatched non-NULL projects
    - **Validates: Requirements 6.4**

- [x] 9. Reranking formula update — revise base weights for V2
  - [x] 9.1 Update base weights in `src/search.py` `rerank()` to V2 formula
    - Change weights: rrf 0.35→0.30, token_overlap 0.20→0.18, title_overlap 0.20→0.18, recency 0.15→0.12, length_score 0.10→0.08
    - Verify all additive factors are present: depth_score (×0.05), type_boost, mem_class_boost, reinforcement (with spacing), project_penalty
    - Complete formula: `0.30*rrf + 0.18*overlap + 0.18*title + 0.12*recency + 0.08*length + 0.05*depth + type_boost + mem_class_boost + reinforcement + project_penalty`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [x] 9.2 Write property test for complete rerank formula
    - **Property 8: Complete Rerank Formula**
    - For any memory with known component values, rerank_score equals the V2 formula within float tolerance
    - **Validates: Requirements 7.1–7.7, 3.6, 4.7–4.9, 5.7, 6.5–6.7**

- [x] 10. Checkpoint — verify all tests pass after reranking changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Relationship discovery — automatic semantic and temporal neighbor linking at ingest
  - [x] 11.1 Add `find_temporal_neighbors()` to `src/db.py`
    - `find_temporal_neighbors(memory_id: str, created_at, limit: int = 3) -> list[dict]`
    - Query memories with `created_at` within ±24 hours, excluding the given memory_id
    - Return list of dicts with id, title, type, created_at
    - _Requirements: 8.7_

  - [x] 11.2 Add relationship discovery to `src/ingest.py` `ingest_content()`
    - After storing parent memory (not chunks): discover relationships
    - Semantic neighbors: embed parent content, `search_similar()` top-3 with similarity > 0.75, excluding self and own chunks, create `related_to` relationships
    - Temporal neighbors: `find_temporal_neighbors()` top-3 within ±24h, create `related_to` relationships
    - Guard: skip for chunk records (parent_id is not None)
    - Cap: max 3 semantic + 3 temporal relationships per memory
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x] 11.3 Write property test for relationship discovery caps
    - **Property 12: Relationship Discovery Caps**
    - For any newly ingested parent memory, at most 3 semantic + 3 temporal relationships created
    - **Validates: Requirements 8.5**

  - [x] 11.4 Write property test for chunks skip relationship discovery
    - **Property 13: Chunks Skip Relationship Discovery**
    - Memories with non-NULL parent_id do not trigger relationship discovery
    - **Validates: Requirements 8.6**

  - [x] 11.5 Write property test for find_temporal_neighbors correctness
    - **Property 14: find_temporal_neighbors Correctness**
    - All returned memories have created_at within ±24h; specified memory_id never in results
    - **Validates: Requirements 8.7**

- [x] 12. Temporal contiguity — enrich search results with temporal context
  - [x] 12.1 Modify `src/mcp_server.py` `memory_search()` to include temporal context
    - After ranked results, query `find_temporal_neighbors()` for the top result
    - Append `temporal_context` list to response: each entry has `id`, `title`, `type`, `created_at`, `relation: "temporal_neighbor"`
    - Deduplicate: exclude entries already in main results
    - Limit to 3 temporal neighbors
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x] 12.2 Write property test for temporal context invariants
    - **Property 15: Temporal Context Invariants**
    - Each entry has required fields, max 3 entries, no duplicates with main results
    - **Validates: Requirements 9.2, 9.3, 9.4**

- [x] 13. Final checkpoint — verify all tests pass
  - Ensure all tests pass (174 existing + all new tests), ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- All reranking modifications target `src/search.py` (not `src/db.py`) — the data-layer-decomposition moved `rerank()`, `hybrid_search()`, and `increment_access_count()` there
- `src/db.py` changes are limited to: `create_memory()` signature, `ALLOWED_UPDATE_FIELDS`, and new `find_temporal_neighbors()`
- Property tests use Hypothesis (already in requirements.txt)
- Checkpoints ensure incremental validation after each logical group
- The consolidation pipeline (V2-TASKS Task 8) is superseded by the dream cycle pipeline and excluded from this spec
- Documentation sync (V2-TASKS Task 9) was completed in a prior commit and excluded from this spec
