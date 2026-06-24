# Design Document

## Overview

This design replaces per-call `psycopg2.connect()` with a connection pool and context manager in `src/db.py`, migrates all 28 callers across three source files and six test files, and converts `hybrid_search()` WHERE clause construction from f-strings to `psycopg2.sql.SQL` composables.

## Architecture

### Connection Pool and Context Manager (Requirements 1, 2, 6)

The pool and context manager are implemented entirely in `src/db.py`. No new modules are introduced.

**Module-level state in `src/db.py`:**

```python
import os
from contextlib import contextmanager
from psycopg2.pool import SimpleConnectionPool

_pool: SimpleConnectionPool | None = None
```

**`get_connection()` becomes a context manager that lazily initializes the pool:**

```python
@contextmanager
def get_connection():
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(
            int(os.environ.get("DB_POOL_MIN", "1")),
            int(os.environ.get("DB_POOL_MAX", "5")),
            **DB_CONFIG,
        )
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
```

Key design decisions:
- `SimpleConnectionPool` over `ThreadedConnectionPool`: The MCP server runs on a single-threaded stdio transport. The dream cycle scripts are also single-threaded. No thread safety needed, and `SimpleConnectionPool` has less overhead.
- `@contextmanager` over a class: Fewer lines, standard pattern, and the `yield`-based approach makes the checkout/rollback/return flow obvious.
- Lazy initialization: The pool is created on first use, not at import time. This means `DB_CONFIG` can be mutated by test fixtures before the first `get_connection()` call, and the pool will use the mutated values.
- Rollback on exception only: The `except` block rolls back and re-raises. Normal exit does nothing — callers that need persistence call `conn.commit()` explicitly, preserving existing behavior.
- No rollback on normal exit: If a caller forgets to `commit()`, the transaction is neither committed nor rolled back by the context manager. The connection returns to the pool with an uncommitted transaction, which psycopg2 will implicitly roll back on the next use. This matches the current behavior where `conn.close()` implicitly rolls back.

**`close_pool()` for lifecycle management:**

```python
def close_pool():
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
```

Called by the `test_db` fixture after mutating `DB_CONFIG` and on teardown. Not called during normal MCP server operation — the pool lives for the process lifetime.

### Caller Migration Pattern (Requirements 3, 4, 5)

Every caller follows the same mechanical transformation:

**Before:**
```python
conn = get_connection()
try:
    with conn.cursor() as cur:
        cur.execute(...)
    conn.commit()
    return result
finally:
    conn.close()
```

**After:**
```python
with get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute(...)
    conn.commit()
    return result
```

The transformation removes the `try/finally/conn.close()` wrapper and replaces `conn = get_connection()` with `with get_connection() as conn:`. No function signatures change. No commit/rollback behavior changes.

**Caller inventory (28 call sites):**

| File | Functions | Count |
|---|---|---|
| `src/db.py` | `is_reachable`, `create_memory`, `get_memory`, `update_memory`, `list_memories`, `search_similar`, `create_relationship`, `get_relationships`, `get_processed_source_urls`, `find_temporal_neighbors` | 10 |
| `src/search.py` | `hybrid_search`, `increment_access_count` | 2 |
| `src/dream_cycle_db.py` | `create_run`, `complete_run`, `store_candidate`, `get_recent_rejections`, `get_user_rejections`, `get_accepted_dissents`, `get_last_briefing_time`, `should_run_briefing`, `get_memory_stats`, `mark_user_rejected`, `get_golden_queries`, `get_tier1_metrics`, `get_tier2_metrics`, `get_evaluator_verdicts_for_run`, `was_feedback_injected`, `get_previous_run_id` | 16 |

**Special case — `is_reachable()`:**

```python
def is_reachable():
    try:
        with get_connection() as conn:
            pass
        return True
    except psycopg2.OperationalError:
        return False
```

This tests that a connection can be checked out from the pool. If the pool itself fails to initialize (e.g., database is down), `SimpleConnectionPool.__init__` raises `OperationalError`, which is caught. If the pool exists but can't provide a connection, `getconn()` raises `PoolError`, which should also be caught. The `except` clause is widened to `except Exception` to handle both cases.

### Composable SQL in hybrid_search() (Requirement 7)

The WHERE clause construction in `hybrid_search()` is converted from f-string interpolation to `psycopg2.sql.SQL` composition. Only the structural parts change — value binding continues to use `%s` placeholders.

**Before:**
```python
conditions = ["embedding IS NOT NULL"]
params_base = []
if type:
    conditions.append("type = %s")
    params_base.append(type)
# ...
where = "WHERE " + " AND ".join(conditions)
# ...
cur.execute(f"""
    SELECT id, 1 - (embedding <=> %s::vector) AS similarity
    FROM memories {where}
    ORDER BY embedding <=> %s::vector LIMIT %s
""", vec_params)
```

**After:**
```python
from psycopg2 import sql

conditions = [sql.SQL("embedding IS NOT NULL")]
params_base = []
if type:
    conditions.append(sql.SQL("type = %s"))
    params_base.append(type)
if status:
    conditions.append(sql.SQL("status = %s"))
    params_base.append(status)
if project:
    conditions.append(sql.SQL("(project = %s OR project IS NULL)"))
    params_base.append(project)
where = sql.SQL("WHERE ") + sql.SQL(" AND ").join(conditions)

vec_query = sql.SQL(
    "SELECT id, 1 - (embedding <=> %s::vector) AS similarity"
    " FROM memories {where}"
    " ORDER BY embedding <=> %s::vector LIMIT %s"
).format(where=where)
cur.execute(vec_query, vec_params)
```

The FTS query follows the same pattern, with the additional `AND search_vector IS NOT NULL` condition appended:

```python
fts_where = where + sql.SQL(" AND search_vector IS NOT NULL")
fts_query = sql.SQL(
    "SELECT id, ts_rank(search_vector, to_tsquery('english', %s)) AS rank"
    " FROM memories {where}"
    " ORDER BY rank DESC LIMIT %s"
).format(where=fts_where)
cur.execute(fts_query, fts_params)
```

The final `SELECT * FROM memories WHERE id = ANY(%s::uuid[])` query has no dynamic WHERE clause and remains a plain string — no conversion needed.

**Scope boundary:** Only `hybrid_search()` is converted. The f-string WHERE clauses in `db.py` (`list_memories`, `search_similar`) are simpler and lower-risk; converting them is out of scope per Requirement 7 which specifically targets `hybrid_search()`.

### Test Fixture Updates (Requirements 8, 9)

**`tests/conftest.py` — `test_db` fixture:**

```python
from src.db import close_pool

@pytest.fixture(scope="session")
def test_db():
    original_config = db.DB_CONFIG.copy()
    _create_test_db()
    db.DB_CONFIG.update({...})  # point at memory_bank_test
    close_pool()  # invalidate any pool created with old config

    with db.get_connection() as conn:
        _apply_migrations(conn)

    yield db.DB_CONFIG

    db.DB_CONFIG.update(original_config)
    close_pool()  # clean up for subsequent sessions
```

**`tests/conftest.py` — `clean_tables` fixture:**

```python
@pytest.fixture()
def clean_tables(test_db):
    yield
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_relationships")
            cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
            cur.execute("DELETE FROM memories")
        conn.commit()
```

**Mock-based tests (`test_dream_cycle_db.py`, `test_dream_cycle.py`):**

The current mock pattern sets `mock_get_conn.return_value = mock_conn`, which works when `get_connection()` returns a connection directly. With the context manager, `get_connection()` returns a context manager that yields a connection.

The fix uses the mock's built-in context manager support:

```python
@patch("src.dream_cycle_db.get_connection")
def test_create_run_returns_uuid(self, mock_get_conn):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (expected_id,)
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    result = create_run("scheduled")

    self.assertEqual(result, str(expected_id))
    mock_conn.commit.assert_called_once()
    # mock_conn.close.assert_called_once()  ← REMOVED
```

Changes per mock-based test:
1. Replace `mock_get_conn.return_value = mock_conn` with `mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)` and `mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)`.
2. Remove all `mock_conn.close.assert_called_once()` assertions.

To reduce boilerplate, a helper can be introduced at the top of each test file:

```python
def _mock_context_manager(mock_get_conn, mock_conn):
    """Configure mock_get_conn to work as a context manager yielding mock_conn."""
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
```

**Direct-caller test files:**

Tests in `test_question_search.py`, `test_ingest_v2.py`, `test_db.py`, `test_search.py`, and `test_mcp_server.py` that call `conn = db.get_connection()` with `try/finally/conn.close()` are mechanically converted to `with db.get_connection() as conn:`, identical to the source file migration pattern.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Pool exhaustion if a caller leaks a connection (doesn't use context manager) | Deadlock on `getconn()` | All callers are migrated in this change. No external callers exist. The context manager guarantees `putconn()` in `finally`. |
| `close_pool()` called while a connection is checked out | `closeall()` closes the in-use connection, causing errors in the caller | `close_pool()` is only called in test fixture setup/teardown (between tests, no connections checked out) and process shutdown. Documented in Req 6.2. |
| `_apply_migrations(conn)` calls `conn.commit()` and `conn.rollback()` — these must still work inside the context manager | Migration failures could leave pool in bad state | The context manager yields the raw psycopg2 connection. `commit()` and `rollback()` work normally. The context manager only adds rollback-on-exception, which doesn't interfere with explicit `rollback()` calls in `_apply_migrations`. |
| `sql.SQL` composition produces different whitespace or formatting than f-strings | Tests that assert on exact SQL strings could fail | No tests assert on the exact SQL of `hybrid_search()`. The composable queries produce semantically identical SQL. Integration tests validate behavior, not SQL text. |

## Out of Scope

- Converting `list_memories()` or `search_similar()` in `db.py` to composable SQL (Req 7 targets only `hybrid_search()`).
- Adding `ThreadedConnectionPool` or async pooling.
- Changing function signatures or return types.
- Adding new feature tests beyond the correctness property tests defined in the requirements.
