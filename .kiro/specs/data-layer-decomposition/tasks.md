# Implementation Plan: Data Layer Decomposition

## Overview

Extract `hybrid_search`, `rerank`, and `increment_access_count` from `src/db.py` into a new `src/search.py` module, then update 2 consumer files (`mcp_server.py`, `scripts/golden_queries.py`). Purely structural — no behavioral changes, zero test patch target changes, zero dream_cycle import changes. All 157 existing tests must pass after each step.

## Tasks

- [x] 1. Atomic extraction — create `src/search.py` and remove 3 functions from `src/db.py`
  - [x] 1.1 Create `src/search.py` with `hybrid_search`, `rerank`, and `increment_access_count` copied verbatim from `src/db.py`, plus required imports (`math`, `re`, `from datetime import datetime, timezone`, `from psycopg2.extras import RealDictCursor`, `from src.db import get_connection`); simultaneously remove those 3 functions and their now-exclusive imports (`import math`, `import re`, `from datetime import datetime, timezone`) from `src/db.py`
    - The extraction MUST be atomic: both the creation and removal happen in one step
    - `search_similar` stays in `db.py` — do NOT move it
    - Imports remaining in `db.py` after extraction: `import os`, `import json`, `import psycopg2`, `from psycopg2.extras import RealDictCursor`
    - Imports needed by `search.py`: `import math`, `import re`, `from datetime import datetime, timezone`, `from psycopg2.extras import RealDictCursor`, `from src.db import get_connection`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 5.1, 5.2, 5.3_

- [x] 2. Checkpoint — run `pytest tests/ -v` and verify all 157 tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Update `src/mcp_server.py` imports
  - [x] 3.1 Change `mcp_server.py` to import `hybrid_search`, `rerank`, `increment_access_count` from `src.search` instead of `src.db`; remove the unused `search_similar` import entirely; keep all other `src.db` imports unchanged
    - Before: `from src.db import (create_memory, get_memory, update_memory, list_memories, search_similar, create_relationship, get_relationships, hybrid_search, rerank, increment_access_count,)`
    - After: `from src.db import (create_memory, get_memory, update_memory, list_memories, create_relationship, get_relationships,)` and `from src.search import hybrid_search, rerank, increment_access_count`
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 4. Checkpoint — run `pytest tests/ -v` and verify all 157 tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Update `scripts/golden_queries.py` imports
  - [x] 5.1 Change `scripts/golden_queries.py` to import `hybrid_search` and `rerank` from `src.search` instead of `src.db`
    - Before: `from src.db import hybrid_search, rerank`
    - After: `from src.search import hybrid_search, rerank`
    - _Requirements: 7.1_

- [x] 6. Checkpoint — run `pytest tests/ -v` and verify all 157 tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Property tests — verify correctness properties from the design document
  - [x] 7.1 Write property test: `hybrid_search` behavioral equivalence (Property 1)
    - **Property 1: hybrid_search behavioral equivalence after extraction**
    - Verify `from src.search import hybrid_search` resolves correctly and the function signature accepts `(query_text, query_embedding, limit, type, status)`
    - Verify `hybrid_search` imports `get_connection` from `src.db` (not inlined)
    - **Validates: Requirements 2.1, 2.3**
  - [x] 7.2 Write property test: `rerank` is a pure function with deterministic scoring (Property 2)
    - **Property 2: rerank is a pure function with deterministic scoring**
    - For any list of result dicts with rrf_score and any query text, `rerank` computes `rerank_score` = `0.35×rrf_score + 0.20×content_overlap + 0.20×title_overlap + 0.15×recency + 0.10×length_score + type_boost + reinforcement`, makes zero DB calls, and returns results sorted by rerank_score descending
    - Use Hypothesis to generate result dicts with varying rrf_score, content, title, created_at, type, access_count
    - **Validates: Requirements 3.1, 3.2, 3.5**
  - [x] 7.3 Write property test: `increment_access_count` no-op on empty list (Property 3)
    - **Property 3: increment_access_count no-op on empty list**
    - Verify calling `increment_access_count([])` makes zero database connections
    - **Validates: Requirements 4.1, 4.2**
  - [x] 7.4 Write property test: `db.py` retains correct public interface (Property 4)
    - **Property 4: db.py retains correct public interface after extraction**
    - Verify `src.db` exports exactly: `get_connection`, `is_reachable`, `create_memory`, `get_memory`, `update_memory`, `list_memories`, `search_similar`, `create_relationship`, `get_relationships`, `get_processed_source_urls`, `ALLOWED_UPDATE_FIELDS`, `DB_CONFIG`
    - Verify `hybrid_search`, `rerank`, `increment_access_count` are NOT in `dir(src.db)`
    - **Validates: Requirements 1.2, 1.4, 5.1, 5.2, 5.3**
  - [x] 7.5 Write property test: all existing test patches remain valid (Property 5)
    - **Property 5: All existing test patches remain valid after extraction**
    - Verify no test files contain `patch("src.db.hybrid_search"`, `patch("src.db.rerank"`, or `patch("src.db.increment_access_count"`
    - Verify `src.dream_cycle.storage.search_similar` patch targets still resolve
    - **Validates: Requirements 10.1, 10.2**
  - [x] 7.6 Write property test: import topology is correct (Property 6)
    - **Property 6: Import topology is correct after refactor**
    - Verify `mcp_server.py` imports `hybrid_search`, `rerank`, `increment_access_count` from `src.search` and does NOT import `search_similar`
    - Verify `golden_queries.py` imports `hybrid_search`, `rerank` from `src.search`
    - Verify `dream_cycle/storage.py` imports are unchanged (still from `src.db`)
    - **Validates: Requirements 6.1, 6.2, 6.3, 7.1, 8.1**

- [x] 8. Final checkpoint — run `pytest tests/ -v` and verify all 157 tests pass (plus new property tests)
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each checkpoint verifies all 157 existing tests pass before proceeding
- Zero test patch targets need updating — no tests directly patch `src.db.hybrid_search`, `src.db.rerank`, or `src.db.increment_access_count`
- `dream_cycle/storage.py` imports are completely unchanged — `search_similar` stays in `db.py`
- The extraction in task 1.1 MUST be atomic — never leave the codebase in a state where functions are duplicated or missing
