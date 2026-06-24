# Implementation Plan: DB Layer Hardening

## Overview

Replace per-call `psycopg2.connect()` with a `SimpleConnectionPool` + `@contextmanager` in `src/db.py`, migrate all 28 source callers and all test callers to `with get_connection() as conn:`, convert `hybrid_search()` WHERE clauses to `psycopg2.sql.SQL` composables, and update all mock-based and direct-caller tests.

## Tasks

- [x] 1. Implement connection pool and context manager in `src/db.py`
  - [x] 1.1 Add pool infrastructure and convert `get_connection()` to a context manager
    - Add imports: `from contextlib import contextmanager`, `from psycopg2.pool import SimpleConnectionPool`
    - Add module-level `_pool: SimpleConnectionPool | None = None`
    - Replace `get_connection()` with `@contextmanager` that lazily initializes `_pool` using `DB_POOL_MIN` (default 1) and `DB_POOL_MAX` (default 5) env vars and `DB_CONFIG`, checks out via `_pool.getconn()`, yields `conn`, rolls back on exception, and returns via `_pool.putconn(conn)` in `finally`
    - Add `close_pool()` function that calls `_pool.closeall()` and sets `_pool = None`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 6.1, 6.2_

  - [x] 1.2 Update `is_reachable()` to use context manager
    - Replace `conn = get_connection(); conn.close()` with `with get_connection() as conn: pass`
    - Widen except clause to `except Exception` to catch both `OperationalError` and `PoolError`
    - _Requirements: 3.4_

- [x] 2. Update test fixtures in `tests/conftest.py`
  - [x] 2.1 Update `test_db` fixture to use pool lifecycle
    - Import `close_pool` from `src.db`
    - After `db.DB_CONFIG.update(...)`, call `close_pool()` so next `get_connection()` creates pool with test config
    - Replace `conn = db.get_connection(); try: _apply_migrations(conn); finally: conn.close()` with `with db.get_connection() as conn: _apply_migrations(conn)`
    - On teardown after restoring original config, call `close_pool()` again
    - _Requirements: 6.3, 8.1, 8.2_

  - [x] 2.2 Update `clean_tables` fixture to use context manager
    - Replace `conn = db.get_connection(); try: ... finally: conn.close()` with `with db.get_connection() as conn:`
    - _Requirements: 8.3_

- [x] 3. Checkpoint
  - Ensure pool infrastructure and fixture changes work together. Run `pytest tests/test_db.py -x` to verify basic connectivity. Ask the user if questions arise.

- [x] 4. Migrate callers in `src/db.py`
  - [x] 4.1 Migrate all 10 functions in `src/db.py` to context manager pattern
    - Convert `create_memory`, `get_memory`, `update_memory`, `list_memories`, `search_similar`, `create_relationship`, `get_relationships`, `get_processed_source_urls`, `find_temporal_neighbors` from `conn = get_connection(); try: ... finally: conn.close()` to `with get_connection() as conn:`
    - Preserve all function signatures, return types, and commit behavior
    - _Requirements: 3.1, 3.2, 3.3_

- [x] 5. Migrate callers in `src/search.py`
  - [x] 5.1 Migrate `hybrid_search()` and `increment_access_count()` to context manager pattern
    - Convert both functions from `conn = get_connection(); try: ... finally: conn.close()` to `with get_connection() as conn:`
    - Preserve function signatures and return types
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 5.2 Convert `hybrid_search()` WHERE clauses to `psycopg2.sql.SQL` composables
    - Add `from psycopg2 import sql` import
    - Replace `conditions` list of strings with `sql.SQL(...)` objects
    - Replace `where = "WHERE " + " AND ".join(conditions)` with `where = sql.SQL("WHERE ") + sql.SQL(" AND ").join(conditions)`
    - Build vector query via `sql.SQL("SELECT id, 1 - (embedding <=> %s::vector) AS similarity FROM memories {where} ORDER BY embedding <=> %s::vector LIMIT %s").format(where=where)`
    - Build FTS query similarly with `fts_where = where + sql.SQL(" AND search_vector IS NOT NULL")`
    - Leave the final `SELECT * FROM memories WHERE id = ANY(...)` as a plain string (no dynamic WHERE)
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 6. Migrate callers in `src/dream_cycle_db.py`
  - [x] 6.1 Migrate all 16 functions in `src/dream_cycle_db.py` to context manager pattern
    - Convert `create_run`, `complete_run`, `store_candidate`, `get_recent_rejections`, `get_user_rejections`, `get_accepted_dissents`, `get_last_briefing_time`, `should_run_briefing`, `get_memory_stats`, `mark_user_rejected`, `get_golden_queries`, `get_tier1_metrics`, `get_tier2_metrics`, `get_evaluator_verdicts_for_run`, `was_feedback_injected`, `get_previous_run_id` from `conn = get_connection(); try: ... finally: conn.close()` to `with get_connection() as conn:`
    - Note: `should_run_briefing()` has one direct `get_connection()` call site (checking new memories/dream cycle runs) plus calls `get_last_briefing_time()` which has its own `get_connection()` — both functions are in the 16-function list and both must be migrated
    - Preserve all function signatures, return types, and commit behavior
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 7. Checkpoint
  - Ensure all source file migrations are complete and consistent. Run `pytest tests/test_db.py tests/test_search.py -x` to verify basic connectivity. Note: the full suite cannot pass yet — mock-based tests in `test_dream_cycle_db.py` and `test_dream_cycle.py` will fail until tasks 8–10 update the mocks to return context managers. Ask the user if questions arise.

- [x] 8. Migrate mock-based tests in `tests/test_dream_cycle_db.py`
  - [x] 8.1 Update all `@patch("src.dream_cycle_db.get_connection")` mocks to return context managers
    - Replace `mock_get_conn.return_value = mock_conn` with `mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)` and `mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)`
    - Consider adding a `_mock_context_manager(mock_get_conn, mock_conn)` helper at the top of the file to reduce boilerplate
    - Remove all `mock_conn.close.assert_called_once()` assertions
    - _Requirements: 9.1, 9.2, 9.4_

- [x] 9. Migrate mock-based tests in `tests/test_dream_cycle.py`
  - [x] 9.1 Update all `get_connection` mocks in `tests/test_dream_cycle.py` to return context managers
    - Same pattern as task 8.1: configure `__enter__`/`__exit__` on mock return value, remove `.close()` assertions
    - _Requirements: 9.1, 9.2, 9.4_

- [x] 10. Migrate direct `get_connection()` callers in remaining test files
  - [x] 10.1 Migrate `tests/test_question_search.py` (10 call sites)
    - Convert all `conn = db.get_connection(); try: ... finally: conn.close()` to `with db.get_connection() as conn:`
    - _Requirements: 9.3, 9.4_

  - [x] 10.2 Migrate `tests/test_ingest_v2.py` (6 call sites)
    - Convert all direct `get_connection()` calls to context manager pattern
    - _Requirements: 9.3, 9.4_

  - [x] 10.3 Migrate `tests/test_db.py`, `tests/test_search.py`, `tests/test_mcp_server.py`
    - Convert any direct `get_connection()` calls to context manager pattern
    - Update any mocks of `get_connection` to return context managers
    - _Requirements: 9.3, 9.4_

- [x] 11. Checkpoint
  - Ensure all test migrations are complete. Run `pytest -x` to verify the full test suite passes with zero new failures. Ask the user if questions arise.
  - _Requirements: 8.4_

- [x] 12. Property-based tests
  - [x] 12.1 Write property test: connection is always returned to pool
    - **Property 1: Connection Return Guarantee**
    - For any sequence of operations (commit, rollback, exception), the connection is always returned to the pool via `putconn()`. Generate random operation sequences and verify pool connection count is restored after each `with get_connection()` block.
    - **Validates: Requirements 2.2, 2.3**

  - [x] 12.2 Write property test: pool is lazily initialized from current DB_CONFIG
    - **Property 2: Lazy Initialization Consistency**
    - After `close_pool()`, mutate `DB_CONFIG` to arbitrary valid values, then call `get_connection()`. Verify the pool was created with the current `DB_CONFIG` values (not stale ones).
    - **Validates: Requirements 1.1, 6.3**

  - [x] 12.3 Write property test: caller migration preserves function signatures
    - **Property 3: Signature Preservation**
    - For each migrated function in `db.py`, `search.py`, `dream_cycle_db.py`, verify via `inspect.signature()` that the function signature matches the pre-migration signature (parameter names, defaults, annotations unchanged).
    - **Validates: Requirements 3.2, 4.3, 5.2**

  - [x] 12.4 Write property test: composable SQL produces identical semantics to f-string approach
    - **Property 4: SQL Equivalence**
    - For all 8 combinations of optional filters (type, status, project each present/absent), generate the WHERE clause via both the old f-string approach and the new `sql.SQL` approach, and verify the rendered SQL strings are semantically identical.
    - **Validates: Requirements 7.3**

  - [x] 12.5 Write property test: close_pool resets pool state
    - **Property 5: Pool Reset Idempotence**
    - Call `close_pool()` N times in sequence (including when pool is already None). Verify no exceptions are raised and `_pool` is None after each call. Then verify `get_connection()` creates a fresh pool.
    - **Validates: Requirements 6.1, 6.2**

- [x] 13. Final checkpoint — run full test suite
  - Run `pytest` to ensure all 242+ tests pass with zero new failures. Ask the user if questions arise.
  - _Requirements: 8.4_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major phase
- Property tests validate universal correctness properties from the design
- The migration is mechanical and repetitive — the same `try/finally/conn.close()` → `with get_connection() as conn:` transformation applies everywhere
