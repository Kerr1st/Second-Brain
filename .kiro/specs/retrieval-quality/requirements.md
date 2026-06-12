# Requirements Document: Retrieval Quality

## Introduction

Retrieval Quality is a set of Layer 1 (passive, at ingest/retrieval time) improvements to the Second Brain's search and ranking pipeline. These are complementary to the dream cycle pipeline (Layer 2 — active synthesis), which produces new semantic memories. Retrieval Quality improvements make search better across all 74K+ existing memories regardless of how they were created.

Layer 1 (this spec) covers: spaced retrieval, memory classification, depth scoring, project scoping, automatic relationship discovery, and temporal contiguity in search results. Layer 2 (dream cycle, separate spec) covers: exploration, candidate generation, consensus evaluation, and feedback injection.

The existing test suite (174 tests across 8 files) covers the dream cycle pipeline and the data-layer-decomposition property tests. This spec's test infrastructure extends coverage to the core `db.py`, `search.py`, and `mcp_server.py` modules. The consolidation pipeline (original V2 Task 8) has been superseded by the dream cycle pipeline. Documentation sync (original V2 Task 9) was completed in a prior cleanup commit.

## Glossary

- **Reranker**: The `rerank()` function in `src/search.py` that applies utility-based scoring (recency, type boost, token overlap, retrieval reinforcement) on top of RRF-fused hybrid search results.
- **Hybrid_Search**: The `hybrid_search()` function in `src/search.py` that combines pgvector cosine similarity search with PostgreSQL full-text search via Reciprocal Rank Fusion (RRF).
- **Memory**: A row in the `memories` PostgreSQL table representing a single unit of knowledge (idea, synthesis, source, insight, decision, etc.).
- **Spacing_Bonus**: A multiplier (0.0–1.0) applied to retrieval reinforcement, based on elapsed time since last access, to prevent popularity bias from repeated same-session retrievals.
- **Memory_Classification**: A categorical label (`semantic`, `episodic`, or `procedural`) assigned to each Memory based on its type, source_type, and content signals.
- **Classifier**: The `classify_memory()` function in `src/classify.py` that deterministically assigns a Memory_Classification to a Memory.
- **Depth_Score**: A numeric value (0.0–1.0) measuring the explanatory depth of a Memory's content, based on causal connectors, concrete examples, and structural signals.
- **Depth_Scorer**: The `compute_depth_score()` function in `src/depth.py` that computes the Depth_Score for a given content string.
- **Project_Tag**: An optional text label on a Memory identifying which project the Memory belongs to, used for encoding-specificity scoping.
- **Cross_Project_Penalty**: A fixed score reduction (-0.15) applied by the Reranker when a Memory's Project_Tag differs from the query's project context.
- **Temporal_Neighbor**: A Memory created within ±24 hours of another Memory, used for temporal contiguity in relationship discovery and search enrichment.
- **Semantic_Neighbor**: A Memory with embedding cosine similarity above a threshold (0.75) to another Memory, discovered at ingest time.
- **Relationship_Discovery**: The automatic process of creating `related_to` relationships between a newly ingested Memory and its Semantic_Neighbors and Temporal_Neighbors.
- **Ingest_Pipeline**: The `ingest_content()` function in `src/ingest.py` that parses, chunks, embeds, and stores content as memories.
- **MCP_Server**: The `src/mcp_server.py` module exposing Second Brain tools (memory_create, memory_search, etc.) to AI agents via the MCP protocol.
- **Test_DB**: An isolated `memory_bank_test` PostgreSQL database used for automated testing, sharing the same local PostgreSQL instance as the production database.
- **Embedding_Mock**: A deterministic fake embedding generator used in tests to avoid Bedrock API calls.

## Requirements

### Requirement 1: Test Infrastructure for Core Modules

**User Story:** As a developer, I want shared test fixtures and baseline regression tests for `db.py`, `search.py`, and `mcp_server.py` so that all subsequent retrieval quality changes can be verified without degrading existing behavior or calling external services.

Note: pytest, hypothesis, and 174 tests (157 dream cycle + 17 data-layer-decomposition property tests) already exist. This requirement covers the gap: `conftest.py` with shared fixtures, `pytest.ini`/`pyproject.toml` configuration, and baseline tests for the core modules that this spec modifies.

#### Acceptance Criteria

1. WHEN the test suite is executed, THE `tests/conftest.py` SHALL provide a Test_DB fixture that creates an isolated `memory_bank_test` database with the same schema as production, including all migrations.
2. WHEN a test function requests a database connection, THE Test_DB fixture SHALL override `db.DB_CONFIG` so that all database operations target the Test_DB instead of the production database.
3. WHEN a test function requires an embedding, THE `tests/conftest.py` SHALL provide an Embedding_Mock fixture that returns a deterministic 1024-dimensional vector without calling the Bedrock API.
4. WHEN the test suite is executed, THE `tests/conftest.py` SHALL provide a sample memory factory fixture that creates memories with known content for use in db.py and mcp_server.py tests.
5. WHEN the test suite is executed, THE `tests/test_db.py` SHALL include baseline tests that verify: `create_memory` returns a valid UUID, `search_similar` returns results for a known embedding, and `create_relationship` persists a retrievable relationship. Search and ranking tests (`hybrid_search`, `rerank`, `increment_access_count`) SHALL be in `tests/test_search.py` or the existing `tests/test_search_properties.py` (which already contains 17 property tests for these functions from the data-layer-decomposition spec).
6. WHEN the test suite is executed, THE `tests/test_mcp_server.py` SHALL include smoke tests that verify: `memory_create` returns an ID string, `memory_search` returns a list, and `memory_create` emits a depth warning for shallow content.
7. THE test suite SHALL pass with zero calls to the Bedrock embedding API.
8. THE project SHALL have a `pytest.ini` or `pyproject.toml` `[tool.pytest]` section configuring test discovery and options.

### Requirement 2: Schema Migration

**User Story:** As a developer, I want the `memories` table extended with `mem_class`, `project`, and `last_accessed_at` columns in a single migration so that all retrieval quality features have the schema they need without multiple ALTER TABLE passes on the 74K-row table.

#### Acceptance Criteria

1. WHEN the migration is applied, THE `memories` table SHALL contain a `mem_class` TEXT column with an index on `mem_class`.
2. WHEN the migration is applied, THE `memories` table SHALL contain a `project` TEXT column with an index on `project`.
3. WHEN the migration is applied, THE `memories` table SHALL contain a `last_accessed_at` TIMESTAMPTZ column with a descending index on `last_accessed_at`.
4. WHEN the migration is applied, THE migration SHALL be idempotent (using `IF NOT EXISTS` guards) so that re-running the migration produces no errors.
5. WHEN the migration is applied, THE `create_memory()` function in `db.py` SHALL accept optional `mem_class` and `project` parameters and persist them to the corresponding columns.
6. WHEN the migration is applied, THE `ALLOWED_UPDATE_FIELDS` set in `db.py` SHALL include `mem_class`, `project`, and `last_accessed_at`.

### Requirement 3: Spaced Retrieval

**User Story:** As a developer, I want retrieval reinforcement modulated by a spacing bonus so that memories accessed repeatedly in a single session do not permanently outrank memories accessed at healthy intervals over time.

#### Acceptance Criteria

1. WHEN `increment_access_count()` is called for a set of Memory IDs, THE function SHALL also set `last_accessed_at` to the current UTC timestamp for each Memory.
2. WHEN the Reranker computes the reinforcement component, THE Reranker SHALL compute Spacing_Bonus as `min(1.0, days_since_last_access / 7.0)` where `days_since_last_access` is the number of days between the current time and the Memory's `last_accessed_at`.
3. WHEN a Memory has a `last_accessed_at` value of today, THE Spacing_Bonus SHALL be 0.0.
4. WHEN a Memory has a `last_accessed_at` value 7 or more days ago, THE Spacing_Bonus SHALL be 1.0.
5. WHEN a Memory has never been accessed (`last_accessed_at` is NULL), THE Spacing_Bonus SHALL default to 1.0.
6. WHEN the Reranker computes the reinforcement component, THE Reranker SHALL use the formula `reinforcement = 0.03 * log1p(access_count) * spacing_bonus` instead of the current `reinforcement = 0.03 * log1p(access_count)`.
7. WHEN two memories have identical RRF scores, token overlap, title overlap, recency, length scores, type boosts, and access counts, THE Memory with a higher Spacing_Bonus SHALL receive a higher rerank score.

### Requirement 4: Memory Classification

**User Story:** As a developer, I want each memory automatically classified as semantic, episodic, or procedural so that the Reranker can boost abstracted knowledge (principles, decisions) over raw logs in search results.

#### Acceptance Criteria

1. WHEN the Classifier receives a Memory with type in (`idea`, `synthesis`, `insight`, `decision`, `connection`, `priority`, `project`, `question`), THE Classifier SHALL return `semantic`.
2. WHEN the Classifier receives a Memory with type `source`, THE Classifier SHALL return `episodic`.
3. WHEN the Classifier receives a Memory whose content contains procedural markers (step-by-step instructions, "how to" phrases, numbered instruction lists), THE Classifier SHALL return `procedural` regardless of the type-based classification.
4. WHEN the Classifier receives a Memory that matches none of the above rules, THE Classifier SHALL return `episodic` as the default.
5. WHEN the Ingest_Pipeline stores a new Memory, THE Ingest_Pipeline SHALL call the Classifier and pass the resulting Memory_Classification as the `mem_class` parameter to `create_memory()`.
6. WHEN the MCP_Server `memory_create` tool stores a new Memory, THE MCP_Server SHALL call the Classifier and pass the resulting Memory_Classification as the `mem_class` parameter to `create_memory()`.
7. WHEN the Reranker scores a Memory with `mem_class` equal to `semantic`, THE Reranker SHALL add a boost of 0.04 to the rerank score.
8. WHEN the Reranker scores a Memory with `mem_class` equal to `procedural`, THE Reranker SHALL add a boost of 0.02 to the rerank score.
9. WHEN the Reranker scores a Memory with `mem_class` equal to `episodic` or NULL, THE Reranker SHALL add a boost of 0.00 to the rerank score.
10. WHEN two memories have identical scores in all other reranking components, THE Memory classified as `semantic` SHALL rank above the Memory classified as `episodic`.

### Requirement 5: Depth Scoring

**User Story:** As a developer, I want a numeric depth score computed for each memory so that deep explanations of WHY rank above shallow bullet lists in search results, replacing the current binary depth check.

#### Acceptance Criteria

1. THE Depth_Scorer SHALL return a float value in the range 0.0 to 1.0 inclusive for all inputs.
2. WHEN the Depth_Scorer analyzes content, THE Depth_Scorer SHALL detect causal connectors ("because", "when...then", "which causes", "which leads", "which means", "so that", "the fix was", "this means"), concrete examples (code blocks, specific numbers, named tools/libraries), the presence of a "Questions this answers:" section, content length, and connection phrases ("extends", "contradicts", "relates to").
3. WHEN content contains multiple causal connectors, concrete examples, and a "Questions this answers:" section, THE Depth_Scorer SHALL return a score above 0.7.
4. WHEN content is a short sentence with no causal connectors or examples, THE Depth_Scorer SHALL return a score below 0.3.
5. WHEN the Ingest_Pipeline stores a new Memory, THE Ingest_Pipeline SHALL compute the Depth_Score and store it in `metadata.depth_score`.
6. WHEN the MCP_Server `memory_create` tool stores a new Memory, THE MCP_Server SHALL compute the Depth_Score, store it in metadata, and use the numeric score for depth warnings instead of the current binary regex check.
7. WHEN the Reranker scores a Memory, THE Reranker SHALL read `depth_score` from the Memory's metadata JSONB and add `0.05 * depth_score` to the rerank score.
8. IF a Memory's metadata does not contain a `depth_score` key, THEN THE Reranker SHALL treat the Depth_Score as 0.0.

### Requirement 6: Project Scoping

**User Story:** As a developer, I want memories tagged with a project identifier so that search results from the current project rank higher than cross-project results, preventing convention pollution across projects.

#### Acceptance Criteria

1. WHEN the MCP_Server `memory_create` tool receives an optional `project` parameter, THE MCP_Server SHALL pass the Project_Tag to `create_memory()`.
2. WHEN the Ingest_Pipeline receives an optional `project` parameter, THE Ingest_Pipeline SHALL pass the Project_Tag to `create_memory()`.
3. WHEN the MCP_Server `memory_search` tool receives an optional `project` parameter, THE MCP_Server SHALL pass the Project_Tag to `hybrid_search()` and to the Reranker as `query_project`.
4. WHEN `hybrid_search()` receives a `project` parameter, THE Hybrid_Search SHALL add a WHERE clause filtering results to memories matching that project or memories with a NULL project.
5. WHEN the Reranker receives a `query_project` parameter and a Memory has a different non-NULL Project_Tag, THE Reranker SHALL apply a Cross_Project_Penalty of -0.15 to the rerank score.
6. WHEN a Memory has a NULL Project_Tag, THE Reranker SHALL apply no Cross_Project_Penalty regardless of the `query_project` value (the Memory is treated as universal knowledge).
7. WHEN a Memory's Project_Tag matches the `query_project`, THE Reranker SHALL apply no Cross_Project_Penalty.

### Requirement 7: Reranking Formula Update

**User Story:** As a developer, I want the reranking formula updated with revised base weights to accommodate the new scoring factors (depth, classification, spacing, project penalty) while keeping the total score in a comparable range.

#### Acceptance Criteria

1. WHEN the Reranker computes the rerank score, THE Reranker SHALL use the following base weights: 0.30 for `rrf_score`, 0.18 for `token_overlap`, 0.18 for `title_overlap`, 0.12 for `recency`, and 0.08 for `length_score`.
2. WHEN the Reranker computes the rerank score, THE Reranker SHALL add `0.05 * depth_score` as an additive factor.
3. WHEN the Reranker computes the rerank score, THE Reranker SHALL add `type_boost` (0.06 for idea/synthesis/insight/decision, 0.00 otherwise) as an additive factor.
4. WHEN the Reranker computes the rerank score, THE Reranker SHALL add `mem_class_boost` (0.04 for semantic, 0.02 for procedural, 0.00 for episodic/NULL) as an additive factor.
5. WHEN the Reranker computes the rerank score, THE Reranker SHALL add `reinforcement` computed as `0.03 * log1p(access_count) * spacing_bonus` as an additive factor.
6. WHEN the Reranker computes the rerank score and a Cross_Project_Penalty applies, THE Reranker SHALL subtract 0.15 from the rerank score.
7. THE complete V2 rerank formula SHALL be: `rerank_score = 0.30 * rrf_score + 0.18 * token_overlap + 0.18 * title_overlap + 0.12 * recency + 0.08 * length_score + 0.05 * depth_score + type_boost + mem_class_boost + reinforcement + project_penalty`.

### Requirement 8: Automatic Relationship Discovery

**User Story:** As a developer, I want new memories to automatically discover and link to semantically similar and temporally adjacent memories at ingest time so that the relationship graph populates without manual effort.

#### Acceptance Criteria

1. WHEN the Ingest_Pipeline stores a parent Memory (not a chunk), THE Ingest_Pipeline SHALL search for the top 3 Semantic_Neighbors with cosine similarity above 0.75, excluding the Memory itself and its own chunks.
2. WHEN a Semantic_Neighbor is found above the similarity threshold, THE Ingest_Pipeline SHALL create a `related_to` relationship between the new Memory and the Semantic_Neighbor.
3. WHEN the Ingest_Pipeline stores a parent Memory, THE Ingest_Pipeline SHALL search for the top 3 Temporal_Neighbors created within ±24 hours of the new Memory's creation time.
4. WHEN a Temporal_Neighbor is found, THE Ingest_Pipeline SHALL create a `related_to` relationship between the new Memory and the Temporal_Neighbor.
5. THE Relationship_Discovery process SHALL create a maximum of 3 semantic relationships and 3 temporal relationships per Memory to prevent graph explosion.
6. WHEN a chunk Memory is stored (a Memory with a non-NULL `parent_id`), THE Ingest_Pipeline SHALL skip Relationship_Discovery for that chunk.
7. THE `db.py` module SHALL provide a `find_temporal_neighbors(memory_id, created_at, limit)` function that returns memories created within ±24 hours of the given timestamp, excluding the specified memory_id.

### Requirement 9: Temporal Contiguity in Search Results

**User Story:** As a developer, I want search results enriched with temporal context so that retrieving a memory also surfaces what else was being worked on at the same time.

#### Acceptance Criteria

1. WHEN the MCP_Server `memory_search` tool returns results, THE MCP_Server SHALL query for Temporal_Neighbors of the top-ranked result that are not already present in the search results.
2. WHEN Temporal_Neighbors are found, THE MCP_Server SHALL append them as a `temporal_context` list in the response, with each entry containing `id`, `title`, `type`, `created_at`, and `relation` set to `temporal_neighbor`.
3. THE `temporal_context` list SHALL contain a maximum of 3 Temporal_Neighbors.
4. WHEN a Temporal_Neighbor is already present in the main search results, THE MCP_Server SHALL exclude that Temporal_Neighbor from the `temporal_context` list to avoid duplicates.
