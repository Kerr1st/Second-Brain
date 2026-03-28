# Feature: v2-reranking-backfill, Property 6: last_accessed_at backfill idempotence
"""Property-based tests for scripts/backfill_last_accessed.sql."""

from datetime import datetime

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

import src.db as db


def _truncate_memories():
    """Truncate memories table between Hypothesis examples."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE memory_relationships CASCADE")
            cur.execute("TRUNCATE memories CASCADE")
        conn.commit()
    finally:
        conn.close()


def _run_last_accessed_backfill():
    """Run the last_accessed_at backfill SQL and return the number of rows affected."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET last_accessed_at = updated_at "
                "WHERE last_accessed_at IS NULL"
            )
            rowcount = cur.rowcount
        conn.commit()
        return rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Property 6: last_accessed_at backfill idempotence
# Validates: Requirements 3.2
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    updated_ats=st.lists(
        st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2030, 1, 1)),
        min_size=1,
        max_size=5,
    ),
)
def test_last_accessed_backfill_idempotence(test_db, clean_tables, updated_ats):
    """Running the backfill UPDATE twice: second run affects zero rows.

    **Validates: Requirements 3.2**
    """
    _truncate_memories()

    # Insert memories (create_memory does not set last_accessed_at, so it's NULL)
    mem_ids = []
    for i, ts in enumerate(updated_ats):
        mem_id = db.create_memory(
            type="idea",
            title=f"last_accessed test {i}",
            content=f"content {i}",
        )
        mem_ids.append(mem_id)

    # Set updated_at to the generated datetime for each memory
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            for mem_id, ts in zip(mem_ids, updated_ats):
                cur.execute(
                    "UPDATE memories SET updated_at = %s WHERE id = %s",
                    (ts, mem_id),
                )
        conn.commit()
    finally:
        conn.close()

    # First backfill run — should affect all rows
    first_count = _run_last_accessed_backfill()
    assert first_count == len(updated_ats), (
        f"First run should affect {len(updated_ats)} rows, but affected {first_count}"
    )

    # Second backfill run — should affect zero rows (idempotent)
    second_count = _run_last_accessed_backfill()
    assert second_count == 0, (
        f"Second run should affect 0 rows, but affected {second_count}"
    )


# ---------------------------------------------------------------------------
# Unit tests for backfill_last_accessed.sql
# Requirements: 3.1, 3.2, 3.3, 3.4
# ---------------------------------------------------------------------------

import os
from pathlib import Path


SQL_FILE = Path(__file__).resolve().parent.parent / "scripts" / "backfill_last_accessed.sql"


class TestBackfillLastAccessedSQL:
    """Unit tests for scripts/backfill_last_accessed.sql.

    Validates: Requirements 3.1, 3.2, 3.3, 3.4
    """

    # --- SQL file content checks (Req 3.1, 3.4) ---

    def test_sql_file_contains_select_count(self):
        """The SQL file SHALL include a SELECT count(*) preview query (Req 3.4)."""
        content = SQL_FILE.read_text()
        assert "SELECT count(*)" in content or "SELECT COUNT(*)" in content

    def test_sql_file_contains_update_statement(self):
        """The SQL file SHALL include the UPDATE statement (Req 3.1)."""
        content = SQL_FILE.read_text()
        assert "UPDATE memories" in content
        assert "SET last_accessed_at = updated_at" in content
        assert "WHERE last_accessed_at IS NULL" in content

    # --- Functional tests (Req 3.1, 3.2, 3.3) ---

    def test_backfill_sets_last_accessed_to_updated_at(self, test_db, clean_tables):
        """UPDATE sets last_accessed_at = updated_at for NULL rows (Req 3.1)."""
        # Insert a memory (last_accessed_at defaults to NULL)
        mem_id = db.create_memory(type="idea", title="unit test mem", content="c")

        # Set updated_at to a known value
        known_ts = datetime(2024, 6, 15, 12, 0, 0)
        conn = db.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET updated_at = %s WHERE id = %s",
                    (known_ts, mem_id),
                )
            conn.commit()
        finally:
            conn.close()

        # Run the backfill
        _run_last_accessed_backfill()

        # Verify last_accessed_at equals updated_at
        row = db.get_memory(mem_id)
        assert row["last_accessed_at"] is not None
        assert row["last_accessed_at"] == row["updated_at"]
        assert row["last_accessed_at"].replace(tzinfo=None) == known_ts

    def test_non_null_rows_not_modified(self, test_db, clean_tables):
        """Rows with existing last_accessed_at are NOT modified (Req 3.3)."""
        mem_id = db.create_memory(type="idea", title="already set", content="c")

        # Manually set both updated_at and last_accessed_at to different values
        original_last_accessed = datetime(2024, 1, 1, 0, 0, 0)
        different_updated_at = datetime(2025, 6, 1, 0, 0, 0)
        conn = db.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET last_accessed_at = %s, updated_at = %s WHERE id = %s",
                    (original_last_accessed, different_updated_at, mem_id),
                )
            conn.commit()
        finally:
            conn.close()

        # Run the backfill
        _run_last_accessed_backfill()

        # Verify last_accessed_at was NOT changed to updated_at
        row = db.get_memory(mem_id)
        assert row["last_accessed_at"].replace(tzinfo=None) == original_last_accessed

    def test_idempotence_second_run_zero_rows(self, test_db, clean_tables):
        """Second backfill run affects zero rows (Req 3.2)."""
        # Insert two memories
        db.create_memory(type="idea", title="idem 1", content="a")
        db.create_memory(type="idea", title="idem 2", content="b")

        first_count = _run_last_accessed_backfill()
        assert first_count == 2

        second_count = _run_last_accessed_backfill()
        assert second_count == 0
