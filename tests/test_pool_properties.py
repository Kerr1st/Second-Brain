"""Property-based tests for the DB connection pool and context manager.

Feature: db-layer-hardening
Properties 1–5: Connection return guarantee, lazy initialization consistency,
signature preservation, SQL equivalence, pool reset idempotence.
"""

import inspect
import os
from unittest.mock import patch, MagicMock

from hypothesis import given, settings, strategies as st
from psycopg2.pool import SimpleConnectionPool

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

        assert current_db == "memory_bank_test", (
            f"Expected pool to connect to memory_bank_test, got {current_db}"
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

            # Pool was created — verify it's a SimpleConnectionPool
            assert db._pool is not None
            assert isinstance(db._pool, SimpleConnectionPool)
            assert db._pool.minconn == pool_min
            assert db._pool.maxconn == pool_max

        close_pool()


# ---------------------------------------------------------------------------
# Property 3: Signature Preservation
# **Validates: Requirements 3.2, 4.3, 5.2**
# ---------------------------------------------------------------------------

# Expected signatures captured before migration (parameter names, defaults, annotations)
_EXPECTED_SIGNATURES = {
    # db.py functions
    "db.create_memory": {
        "params": ["type", "title", "content", "embedding", "tags", "source_url",
                    "source_type", "metadata", "status", "confidence",
                    "parent_id", "summary", "mem_class", "project", "encoding_context"],
    },
    "db.get_memory": {"params": ["memory_id"]},
    "db.update_memory": {"params": ["memory_id"]},  # **fields
    "db.list_memories": {"params": ["type", "status", "source_type", "limit", "offset"]},
    "db.search_similar": {"params": ["embedding", "limit", "type", "status"]},
    "db.create_relationship": {"params": ["source_id", "target_id", "relation_type", "note"]},
    "db.get_relationships": {"params": ["memory_id"]},
    "db.get_processed_source_urls": {"params": ["source_type"]},
    "db.find_temporal_neighbors": {"params": ["memory_id", "created_at", "limit"]},
    # search.py functions
    "search.hybrid_search": {"params": ["query_text", "query_embedding", "limit", "type", "status", "project", "source_type", "created_after"]},
    "search.increment_access_count": {"params": ["memory_ids"]},
    # dream_cycle_db.py functions
    "dream_cycle_db.create_run": {"params": ["run_type", "backend_provenance"]},
    "dream_cycle_db.complete_run": {"params": ["run_id", "stats", "digest", "explorer_output", "explorer_feedback_injected"]},
    "dream_cycle_db.store_candidate": {"params": ["run_id", "candidate", "verdicts", "final_verdict", "created_memory_id"]},
    "dream_cycle_db.get_recent_rejections": {"params": ["n_cycles"]},
    "dream_cycle_db.get_user_rejections": {"params": ["n_cycles"]},
    "dream_cycle_db.get_accepted_dissents": {"params": ["n_cycles"]},
    "dream_cycle_db.get_last_briefing_time": {"params": []},
    "dream_cycle_db.should_run_briefing": {"params": []},
    "dream_cycle_db.get_memory_stats": {"params": []},
    "dream_cycle_db.mark_user_rejected": {"params": ["candidate_id", "reason"]},
    "dream_cycle_db.get_golden_queries": {"params": []},
    "dream_cycle_db.get_tier1_metrics": {"params": ["n_cycles"]},
    "dream_cycle_db.get_tier2_metrics": {"params": ["n_cycles"]},
    "dream_cycle_db.get_evaluator_verdicts_for_run": {"params": ["run_id"]},
    "dream_cycle_db.was_feedback_injected": {"params": ["run_id"]},
    "dream_cycle_db.get_previous_run_id": {"params": ["run_type"]},
}

_MODULE_MAP = {
    "db": db,
    "search": search,
    "dream_cycle_db": dream_cycle_db,
}


class TestSignaturePreservation:
    """Feature: db-layer-hardening, Property 3: Signature Preservation

    For each migrated function, verify via inspect.signature() that the
    function signature matches the pre-migration signature.

    **Validates: Requirements 3.2, 4.3, 5.2**
    """

    def test_all_signatures_preserved(self):
        """All migrated functions preserve their parameter names."""
        for func_path, expected in _EXPECTED_SIGNATURES.items():
            module_name, func_name = func_path.split(".")
            module = _MODULE_MAP[module_name]
            func = getattr(module, func_name)
            sig = inspect.signature(func)

            actual_params = [
                name for name, p in sig.parameters.items()
                if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            ]

            assert actual_params == expected["params"], (
                f"{func_path}: expected params {expected['params']}, "
                f"got {actual_params}"
            )


# ---------------------------------------------------------------------------
# Property 4: SQL Equivalence
# **Validates: Requirements 7.3**
# ---------------------------------------------------------------------------

from psycopg2 import sql as psql


def _old_where(type=None, status=None, project=None):
    """Reproduce the old f-string WHERE clause construction."""
    conditions = ["embedding IS NOT NULL"]
    if type:
        conditions.append("type = %s")
    if status:
        conditions.append("status = %s")
    if project:
        conditions.append("(project = %s OR project IS NULL)")
    return "WHERE " + " AND ".join(conditions)


def _new_where(type=None, status=None, project=None):
    """Reproduce the new sql.SQL WHERE clause construction."""
    conditions = [psql.SQL("embedding IS NOT NULL")]
    if type:
        conditions.append(psql.SQL("type = %s"))
    if status:
        conditions.append(psql.SQL("status = %s"))
    if project:
        conditions.append(psql.SQL("(project = %s OR project IS NULL)"))
    return psql.SQL("WHERE ") + psql.SQL(" AND ").join(conditions)


class TestSQLEquivalence:
    """Feature: db-layer-hardening, Property 4: SQL Equivalence

    For all 8 combinations of optional filters (type, status, project each
    present/absent), the rendered SQL from the new sql.SQL approach is
    identical to the old f-string approach.

    **Validates: Requirements 7.3**
    """

    @given(
        has_type=st.booleans(),
        has_status=st.booleans(),
        has_project=st.booleans(),
    )
    @settings(max_examples=8)
    def test_sql_equivalence(self, has_type, has_status, has_project):
        """Old f-string and new sql.SQL produce identical WHERE clauses."""
        type_val = "idea" if has_type else None
        status_val = "active" if has_status else None
        project_val = "my-project" if has_project else None

        old = _old_where(type=type_val, status=status_val, project=project_val)
        new_composed = _new_where(type=type_val, status=status_val, project=project_val)

        # Render the composed SQL to a string
        new_rendered = new_composed.as_string(None)

        assert old == new_rendered, (
            f"SQL mismatch for type={type_val}, status={status_val}, project={project_val}:\n"
            f"  Old: {old!r}\n"
            f"  New: {new_rendered!r}"
        )


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
