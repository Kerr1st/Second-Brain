"""Property-based tests for the DB connection pool and context manager.

Feature: db-layer-hardening
Properties 1–5: Connection return guarantee, lazy initialization consistency,
signature preservation, SQL equivalence, pool reset idempotence.
"""

import os
from unittest.mock import patch, MagicMock

from hypothesis import given, settings, strategies as st
from psycopg2.pool import ThreadedConnectionPool

import src.db as db
import src.search as search
import src.dream_cycle_db as dream_cycle_db
from src.db import get_connection, close_pool, _pool


# ---------------------------------------------------------------------------
# Property 1: Connection Return Guarantee
# **Validates: Requirements 2.2, 2.3**
# ---------------------------------------------------------------------------

class TestConnectionReturnGuarantee:
    """Feature: db-layer-hardening, Property 1: Connection Return Guarantee

    For any sequence of operations (commit, rollback, exception), the
    connection is always returned to the pool via putconn().

    **Validates: Requirements 2.2, 2.3**
    """

    @given(
        ops=st.lists(
            st.sampled_from(["commit", "rollback", "exception", "noop"]),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=50, deadline=None)
    def test_connection_always_returned(self, test_db, ops):
        """For any sequence of operations inside a with-block, the connection
        is returned to the pool after the block exits."""
        close_pool()

        for op in ops:
            try:
                with get_connection() as conn:
                    if op == "commit":
                        conn.commit()
                    elif op == "rollback":
                        conn.rollback()
                    elif op == "exception":
                        raise ValueError("test exception")
                    # noop: just enter and exit
            except ValueError:
                pass  # expected for "exception" op

        # After all operations, pool should still be functional
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone()[0] == 1

        close_pool()


# ---------------------------------------------------------------------------
# Property 2: Lazy Initialization Consistency
# **Validates: Requirements 1.1, 6.3**
# ---------------------------------------------------------------------------

class TestLazyInitializationConsistency:
    """Feature: db-layer-hardening, Property 2: Lazy Initialization Consistency

    After close_pool(), mutate DB_CONFIG to the test database values, then
    call get_connection(). Verify the pool was created with the current
    DB_CONFIG values (not stale ones).

    **Validates: Requirements 1.1, 6.3**
    """

    def test_pool_uses_current_config_after_close(self, test_db):
        """After close_pool(), the next get_connection() creates a pool
        using the current DB_CONFIG values."""
        close_pool()

        # DB_CONFIG is already pointing at test DB (set by test_db fixture)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                current_db = cur.fetchone()[0]

        assert current_db == test_db["dbname"], (
            f"Expected pool to connect to the disposable database, got {current_db}"
        )
        close_pool()

    @given(
        pool_min=st.integers(min_value=1, max_value=2),
        pool_max=st.integers(min_value=2, max_value=5),
    )
    @settings(max_examples=10, deadline=None)
    def test_pool_respects_env_vars(self, test_db, pool_min, pool_max):
        """Pool min/max are read from env vars on lazy init."""
        close_pool()

        with patch.dict(os.environ, {
            "DB_POOL_MIN": str(pool_min),
            "DB_POOL_MAX": str(pool_max),
        }):
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")

            # Pool was created — verify it's a ThreadedConnectionPool
            assert db._pool is not None
            assert isinstance(db._pool, ThreadedConnectionPool)
            assert db._pool.minconn == pool_min
            assert db._pool.maxconn == pool_max

        close_pool()


# ---------------------------------------------------------------------------
# Property 5: Pool Reset Idempotence
# **Validates: Requirements 6.1, 6.2**
# ---------------------------------------------------------------------------

class TestPoolResetIdempotence:
    """Feature: db-layer-hardening, Property 5: Pool Reset Idempotence

    Call close_pool() N times in sequence (including when pool is already
    None). Verify no exceptions are raised and _pool is None after each
    call. Then verify get_connection() creates a fresh pool.

    **Validates: Requirements 6.1, 6.2**
    """

    @given(n=st.integers(min_value=1, max_value=10))
    @settings(max_examples=20, deadline=None)
    def test_close_pool_idempotent(self, test_db, n):
        """close_pool() can be called N times without error, _pool is None
        after each call, and get_connection() still works after."""
        # Ensure pool exists first
        with get_connection() as conn:
            pass

        for _ in range(n):
            close_pool()
            assert db._pool is None

        # Pool should still be creatable
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone()[0] == 1

        close_pool()
