# Requirements Document

## Introduction

This document specifies the requirements for decomposing `src/db.py` by extracting search and ranking logic into a new `src/search.py` module. The refactor is purely structural — no behavioral changes. Three functions (`hybrid_search`, `rerank`, `increment_access_count`) move from `src/db.py` to `src/search.py`. The data-access primitive `search_similar` stays in `src/db.py`. Only two consumer files update imports (`src/mcp_server.py` and `scripts/golden_queries.py`). All 157 existing tests must pass after each step.

## Glossary

- **Data_Access_Layer**: `src/db.py` — connection management, memory CRUD, vector similarity search, relationships, deduplication helpers
- **Service_Layer**: `src/search.py` — hybrid retrieval algorithms, utility reranking, retrieval reinforcement
- **Presentation_Layer**: `src/mcp_server.py` — MCP tool definitions, input validation, output formatting
- **Hybrid_Search**: Multi-step retrieval combining pgvector cosine search, PostgreSQL BM25 full-text search, and Reciprocal Rank Fusion (RRF)
- **Rerank**: Utility reranking function computing a composite score from rrf_score, token overlap, recency, content length, type boost, and retrieval reinforcement
- **Increment_Access_Count**: Function that bumps `access_count` for retrieved memories to implement retrieval reinforcement
- **Search_Similar**: Single-SQL vector similarity query — a data-access primitive that stays in `src/db.py`
- **Extraction**: The atomic operation of creating `src/search.py` with the three functions and simultaneously removing them from `src/db.py`
- **Consumer_Module**: Any Python module that imports functions from `src/db.py` or `src/search.py`

## Requirements

### Requirement 1: Atomic Function Extraction

**User Story:** As a developer, I want the three search/ranking functions extracted from `db.py` into `search.py` in a single atomic step, so that there is never a state where functions are duplicated or missing.

#### Acceptance Criteria

1. WHEN the Extraction is performed, THE Service_Layer SHALL contain exactly three public functions: `hybrid_search`, `rerank`, and `increment_access_count`
2. WHEN the Extraction is performed, THE Data_Access_Layer SHALL no longer define `hybrid_search`, `rerank`, or `increment_access_count`
3. WHEN the Extraction is performed, THE Service_Layer SHALL import `get_connection` from the Data_Access_Layer for database access
4. WHEN the Extraction is performed, THE Data_Access_Layer SHALL continue to define and export `search_similar` as a data-access primitive

### Requirement 2: Behavioral Preservation of hybrid_search

**User Story:** As a developer, I want `hybrid_search` in `src/search.py` to behave identically to the original in `src/db.py`, so that retrieval results are unchanged after the refactor.

#### Acceptance Criteria

1. WHEN `hybrid_search` is called with any query text, query embedding, limit, type filter, and status filter, THE Service_Layer SHALL return the same records with the same `rrf_score` values and the same ordering as the original implementation in the Data_Access_Layer
2. WHEN `hybrid_search` receives an empty query text, THE Service_Layer SHALL skip full-text search and return vector-only results fused via RRF
3. WHEN `hybrid_search` is called, THE Service_Layer SHALL compute RRF scores using the formula `1/(k + vec_rank) + 1/(k + fts_rank)` with k=60

### Requirement 3: Behavioral Preservation of rerank

**User Story:** As a developer, I want `rerank` in `src/search.py` to compute identical scores to the original in `src/db.py`, so that ranking behavior is unchanged.

#### Acceptance Criteria

1. WHEN `rerank` is called with a list of result dicts and a query text, THE Service_Layer SHALL compute `rerank_score` as `0.35×rrf_score + 0.20×content_overlap + 0.20×title_overlap + 0.15×recency + 0.10×length_score + type_boost + reinforcement`
2. WHEN `rerank` is called, THE Service_Layer SHALL make zero database calls
3. WHEN `rerank` receives an empty results list, THE Service_Layer SHALL return the empty list immediately
4. WHEN a result dict has a NULL or missing `created_at`, THE Service_Layer SHALL use a default recency value of 0.35
5. WHEN a result dict has a type of `idea`, `synthesis`, `insight`, or `decision`, THE Service_Layer SHALL apply a type boost of 0.06

### Requirement 4: Behavioral Preservation of increment_access_count

**User Story:** As a developer, I want `increment_access_count` in `src/search.py` to behave identically to the original, so that retrieval reinforcement is unchanged.

#### Acceptance Criteria

1. WHEN `increment_access_count` is called with an empty list, THE Service_Layer SHALL return immediately without making any database connection
2. WHEN `increment_access_count` is called with a non-empty list of memory IDs, THE Service_Layer SHALL increment `access_count` by exactly 1 for each matching memory using `coalesce(access_count, 0) + 1`

### Requirement 5: Data Access Layer Retains Correct Public Interface

**User Story:** As a developer, I want `db.py` to retain only connection management and CRUD operations after extraction, so that the module has a single clear responsibility.

#### Acceptance Criteria

1. THE Data_Access_Layer SHALL export exactly these public functions after extraction: `get_connection`, `is_reachable`, `create_memory`, `get_memory`, `update_memory`, `list_memories`, `search_similar`, `create_relationship`, `get_relationships`, `get_processed_source_urls`
2. THE Data_Access_Layer SHALL export the constants `ALLOWED_UPDATE_FIELDS` and `DB_CONFIG`
3. THE Data_Access_Layer SHALL NOT export `hybrid_search`, `rerank`, or `increment_access_count` after extraction

### Requirement 6: mcp_server.py Import Update

**User Story:** As a developer, I want `mcp_server.py` to import search functions from `src.search` and CRUD functions from `src.db`, so that the presentation layer correctly references the new module boundaries.

#### Acceptance Criteria

1. WHEN the import update is performed, THE Presentation_Layer SHALL import `hybrid_search`, `rerank`, and `increment_access_count` from the Service_Layer
2. WHEN the import update is performed, THE Presentation_Layer SHALL import `create_memory`, `get_memory`, `update_memory`, `list_memories`, `create_relationship`, and `get_relationships` from the Data_Access_Layer
3. WHEN the import update is performed, THE Presentation_Layer SHALL remove the previously unused `search_similar` import entirely

### Requirement 7: golden_queries.py Import Update

**User Story:** As a developer, I want `scripts/golden_queries.py` to import `hybrid_search` and `rerank` from `src.search`, so that the script references the correct module.

#### Acceptance Criteria

1. WHEN the import update is performed, THE `scripts/golden_queries.py` module SHALL import `hybrid_search` and `rerank` from the Service_Layer instead of the Data_Access_Layer

### Requirement 8: Unchanged Consumer Modules

**User Story:** As a developer, I want modules that only use data-access primitives to require zero import changes, so that the refactor has minimal blast radius.

#### Acceptance Criteria

1. THE `src/dream_cycle/storage.py` module SHALL continue to import `create_memory`, `create_relationship`, `get_memory`, `search_similar`, and `update_memory` from the Data_Access_Layer without any changes
2. THE `src/ingest.py` module SHALL continue to import `create_memory` and `get_processed_source_urls` from the Data_Access_Layer without any changes
3. THE `src/dream_cycle_db.py` module SHALL continue to import `get_connection` from the Data_Access_Layer without any changes
4. THE `scripts/crawlee_ingest.py` module SHALL continue to import `get_processed_source_urls` and `is_reachable` from the Data_Access_Layer without any changes
5. THE `scripts/ingest_chats.py` module SHALL continue to import `is_reachable` from the Data_Access_Layer without any changes

### Requirement 9: Full Test Suite Passes After Each Step

**User Story:** As a developer, I want all 157 existing tests to pass after each extraction step, so that I have confidence the refactor introduces no regressions.

#### Acceptance Criteria

1. WHEN the Extraction step is completed, THE test suite SHALL pass all 157 existing tests
2. WHEN the mcp_server.py import update is completed, THE test suite SHALL pass all 157 existing tests
3. WHEN the golden_queries.py import update is completed, THE test suite SHALL pass all 157 existing tests

### Requirement 10: Zero Test Patch Target Changes

**User Story:** As a developer, I want zero existing test patch targets to require updating, so that the refactor does not touch test files.

#### Acceptance Criteria

1. THE test suite SHALL require zero changes to existing `unittest.mock.patch` targets because no tests directly patch `src.db.hybrid_search`, `src.db.rerank`, or `src.db.increment_access_count`
2. THE test suite SHALL require zero changes to `src.dream_cycle.storage.search_similar` patch targets because `search_similar` stays in the Data_Access_Layer
