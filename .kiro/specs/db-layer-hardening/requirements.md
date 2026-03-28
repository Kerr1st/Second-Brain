# Requirements Document

## Introduction

The Second Brain project uses PostgreSQL + pgvector for personal knowledge management with 74K+ memories. Currently, every database function across `src/db.py`, `src/search.py`, and `src/dream_cycle_db.py` opens and closes its own connection via `get_connection() → psycopg2.connect()`. With 30+ functions following this pattern and no connection pooling, the long-lived MCP server process pays repeated connection setup costs. Additionally, `src/search.py` builds SQL WHERE clauses via f-string interpolation of hardcoded conditions — safe (parameterized values) but fragile.

This spec bundles two improvements: (1) replace per-call connections with a `psycopg2.pool` connection pool and a context manager pattern, and (2) replace f-string WHERE clause construction in `hybrid_search()` with `psycopg2.sql.SQL` composable queries.

## Glossary

- **Pool**: A `psycopg2.pool.SimpleConnectionPool` (or `ThreadedConnectionPool`) instance that maintains a set of reusable database connections.
- **Connection_Manager**: A context manager returned by `get_connection()` that checks out a connection from the Pool on entry and returns it on exit.
- **DB_CONFIG**: The module-level dictionary in `src/db.py` containing PostgreSQL connection parameters (host, port, dbname, user, password).
- **Caller**: Any function in `db.py`, `search.py`, `dream_cycle_db.py`, or test files that obtains a database connection via `get_connection()`.
- **Composable_Query**: A query built using `psycopg2.sql.SQL`, `sql.Identifier`, and `sql.Composed` instead of Python f-string interpolation.
- **Test_Fixture**: The `test_db` session-scoped pytest fixture in `tests/conftest.py` that mutates `DB_CONFIG` in-place to point at the `memory_bank_test` database.

## Requirements

### Requirement 1: Connection Pool Initialization

**User Story:** As the system operator, I want database connections to be pooled, so that the MCP server and other long-lived processes reuse connections instead of opening a new one per function call.

#### Acceptance Criteria

1. WHEN the first call to `get_connection()` occurs, THE Pool SHALL be lazily initialized using the current values of DB_CONFIG.
2. THE Pool SHALL be configured with minimum and maximum connection counts read from environment variables `DB_POOL_MIN` (default 1) and `DB_POOL_MAX` (default 5), consistent with how DB_CONFIG already uses environment variables for connection parameters.
3. THE Pool SHALL be stored as a module-level variable in `src/db.py`.
4. IF the Pool has already been initialized, THEN THE `get_connection()` function SHALL return a connection from the existing Pool without creating a new Pool.

### Requirement 2: Connection Context Manager

**User Story:** As a developer, I want `get_connection()` to return a context manager, so that connections are automatically returned to the Pool when the calling block exits.

#### Acceptance Criteria

1. WHEN a Caller uses `with get_connection() as conn:`, THE Connection_Manager SHALL check out a connection from the Pool.
2. WHEN the `with` block exits normally, THE Connection_Manager SHALL return the connection to the Pool.
3. IF an exception occurs inside the `with` block, THEN THE Connection_Manager SHALL roll back the transaction and return the connection to the Pool. This makes explicit the existing implicit behavior where `conn.close()` in `finally` blocks rolls back uncommitted transactions.
4. THE Connection_Manager SHALL support `conn.cursor()`, `conn.commit()`, and all standard psycopg2 connection operations within the `with` block.

### Requirement 3: Caller Migration in db.py

**User Story:** As a developer, I want all functions in `db.py` to use the new context manager pattern, so that connections are pooled consistently.

#### Acceptance Criteria

1. WHEN any function in `db.py` needs a database connection, THE function SHALL use `with get_connection() as conn:` instead of `conn = get_connection()` with a manual `try/finally/conn.close()`.
2. THE migrated functions SHALL preserve their existing function signatures (parameters and return types).
3. THE migrated functions SHALL preserve their existing commit/rollback behavior.
4. THE `is_reachable()` function SHALL use the context manager pattern and return True when a connection can be checked out from the Pool.

### Requirement 4: Caller Migration in search.py

**User Story:** As a developer, I want all functions in `search.py` to use the new context manager pattern, so that search operations benefit from connection pooling.

#### Acceptance Criteria

1. WHEN `hybrid_search()` needs a database connection, THE function SHALL use `with get_connection() as conn:` instead of `conn = get_connection()` with a manual `try/finally/conn.close()`.
2. WHEN `increment_access_count()` needs a database connection, THE function SHALL use `with get_connection() as conn:` instead of `conn = get_connection()` with a manual `try/finally/conn.close()`.
3. THE migrated functions SHALL preserve their existing function signatures and return types.

### Requirement 5: Caller Migration in dream_cycle_db.py

**User Story:** As a developer, I want all functions in `dream_cycle_db.py` to use the new context manager pattern, so that dream cycle operations use pooled connections.

#### Acceptance Criteria

1. WHEN any function in `dream_cycle_db.py` needs a database connection, THE function SHALL use `with get_connection() as conn:` instead of `conn = get_connection()` with a manual `try/finally/conn.close()`.
2. THE migrated functions SHALL preserve their existing function signatures and return types.
3. THE migrated functions SHALL preserve their existing commit/rollback behavior.

### Requirement 6: Pool Lifecycle Management

**User Story:** As the system operator, I want the connection pool to be properly managed across process lifecycle events, so that connections are cleaned up and the pool works correctly with test overrides.

#### Acceptance Criteria

1. THE `db.py` module SHALL expose a `close_pool()` function that closes all connections in the Pool and resets the Pool to uninitialized state.
2. WHEN `close_pool()` is called, THE Pool SHALL close all idle connections and reset the Pool to uninitialized state. `close_pool()` SHALL only be called when no connections are checked out (e.g., during test fixture setup/teardown or process shutdown).
3. THE Test_Fixture SHALL continue to work by mutating `DB_CONFIG` in-place and then calling `close_pool()` so that the next `get_connection()` call creates a new Pool with the updated configuration.

### Requirement 7: Composable SQL in hybrid_search()

**User Story:** As a developer, I want the WHERE clause construction in `hybrid_search()` to use `psycopg2.sql.SQL` composable queries, so that the query building is structurally safe and follows psycopg2 best practices.

#### Acceptance Criteria

1. WHEN `hybrid_search()` builds the WHERE clause for the vector search query, THE function SHALL use `psycopg2.sql.SQL` and `sql.Composed` instead of f-string interpolation.
2. WHEN `hybrid_search()` builds the WHERE clause for the full-text search query, THE function SHALL use `psycopg2.sql.SQL` and `sql.Composed` instead of f-string interpolation.
3. THE Composable_Query construction SHALL produce SQL output identical in semantics to the current f-string approach for all combinations of optional filters (type, status, project).
4. THE Composable_Query SHALL use `psycopg2.sql.SQL` for structural parts of the query (column names, WHERE clauses, table names) while continuing to use `%s` parameter placeholders for user-supplied values (type, status, project, embedding, limit). Both coexist in the same query via `sql.SQL("...{where}...").format(where=composed_where)` where structural composition uses `sql.SQL` and value binding uses `%s` with psycopg2's standard parameterization.

### Requirement 8: Test Compatibility

**User Story:** As a developer, I want all 242+ existing tests to continue passing after these changes, so that the refactoring introduces no regressions.

#### Acceptance Criteria

1. WHEN the test suite runs with the Test_Fixture, THE Pool SHALL initialize using the test database configuration (`memory_bank_test`).
2. WHEN the Test_Fixture mutates DB_CONFIG, THE Test_Fixture SHALL call `close_pool()` so that the next `get_connection()` call creates a new Pool with the updated configuration. WHEN the Test_Fixture restores the original DB_CONFIG on teardown, THE Test_Fixture SHALL call `close_pool()` again so subsequent test sessions start fresh.
3. THE `clean_tables` fixture SHALL continue to function correctly with pooled connections.
4. THE full test suite SHALL pass with zero new failures after all changes are applied.

### Requirement 9: Test Mock and Test Caller Migration

**User Story:** As a developer, I want all test files that mock or directly call `get_connection()` to be updated for the context manager pattern, so that the test suite remains functional after the migration.

#### Acceptance Criteria

1. WHEN a test uses `@patch("src.dream_cycle_db.get_connection")` or any equivalent patch of `get_connection`, THE mock SHALL return a context manager (e.g., via `MagicMock` configured with `__enter__` and `__exit__`) so that `with get_connection() as conn:` works correctly in the code under test. This affects 17+ mock-based tests across `tests/test_dream_cycle_db.py` and `tests/test_dream_cycle.py`.
2. WHEN a test previously asserted `mock_conn.close.assert_called_once()`, THE assertion SHALL be removed or replaced with an appropriate assertion (e.g., verifying the context manager was used), since the context manager pattern returns connections to the Pool rather than calling `close()`.
3. WHEN a test file calls `conn = db.get_connection()` directly for setup/teardown SQL with manual `try/finally/conn.close()`, THE test code SHALL be migrated to use `with db.get_connection() as conn:`. This applies to `tests/conftest.py` (`test_db` and `clean_tables` fixtures), `tests/test_question_search.py` (10 call sites), `tests/test_ingest_v2.py` (6 call sites), and any other test files with direct `get_connection()` calls (e.g., `tests/test_db.py`, `tests/test_search.py`, `tests/test_mcp_server.py`).
4. THE migrated test code SHALL preserve the existing test semantics (same SQL executed, same assertions on results).
