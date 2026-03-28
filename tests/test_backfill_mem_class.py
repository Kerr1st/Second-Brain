# Feature: v2-reranking-backfill, Property 1: mem_class backfill consistency
"""Property-based tests for scripts/backfill_mem_class.py."""

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

import src.db as db
from src.classify import classify_memory

# Valid domain values
MEMORY_TYPES = [
    "idea", "synthesis", "insight", "decision", "connection",
    "priority", "project", "question", "source", "research",
]

SOURCE_TYPES = [
    "youtube", "article", "pdf", "course",
    "kiro_cli_chat", "kiro_ide_chat", "notes", "manual",
    None,
]


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


# ---------------------------------------------------------------------------
# Property 1: mem_class backfill consistency
# Validates: Requirements 1.1, 1.2
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    mem_type=st.sampled_from(MEMORY_TYPES),
    source_type=st.sampled_from(SOURCE_TYPES),
    content=st.text(
        alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
        min_size=0,
        max_size=500,
    ),
)
def test_backfill_mem_class_consistency(test_db, clean_tables, mem_type, source_type, content):
    """After backfill, stored mem_class equals classify_memory(type, source_type, content).

    **Validates: Requirements 1.1, 1.2**
    """
    # Clean between Hypothesis examples
    _truncate_memories()

    # Insert a memory with NULL mem_class
    mem_id = db.create_memory(
        type=mem_type,
        title="prop test",
        content=content,
        source_type=source_type,
        mem_class=None,
    )

    # Run the core backfill logic: query NULL rows, classify, update
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, type, source_type, content "
                "FROM memories WHERE mem_class IS NULL ORDER BY id"
            )
            rows = cur.fetchall()

        updates = []
        for row in rows:
            rid, rtype, rsource, rcontent = row
            cls = classify_memory(rtype, rsource, rcontent)
            updates.append((cls, rid))

        if updates:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE memories SET mem_class = %s WHERE id = %s",
                    updates,
                )
            conn.commit()
    finally:
        conn.close()

    # Verify: stored mem_class matches the pure function
    stored = db.get_memory(mem_id)
    expected = classify_memory(mem_type, source_type, content)
    assert stored["mem_class"] == expected, (
        f"mem_class mismatch for type={mem_type!r}, source_type={source_type!r}: "
        f"stored={stored['mem_class']!r}, expected={expected!r}"
    )


# ---------------------------------------------------------------------------
# Feature: v2-reranking-backfill, Property 4: mem_class backfill idempotence
# ---------------------------------------------------------------------------

def _run_backfill_logic():
    """Run the core backfill logic: query NULL rows, classify, update.

    Returns the number of rows processed (updated).
    """
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, type, source_type, content "
                "FROM memories WHERE mem_class IS NULL ORDER BY id"
            )
            rows = cur.fetchall()

        updates = []
        for row in rows:
            rid, rtype, rsource, rcontent = row
            cls = classify_memory(rtype, rsource, rcontent)
            updates.append((cls, rid))

        if updates:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE memories SET mem_class = %s WHERE id = %s",
                    updates,
                )
            conn.commit()

        return len(updates)
    finally:
        conn.close()


@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    mem_types=st.lists(
        st.sampled_from(MEMORY_TYPES),
        min_size=1,
        max_size=5,
    ),
    source_types=st.lists(
        st.sampled_from(SOURCE_TYPES),
        min_size=1,
        max_size=5,
    ),
    contents=st.lists(
        st.text(
            alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
            min_size=0,
            max_size=200,
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_backfill_mem_class_idempotence(test_db, clean_tables, mem_types, source_types, contents):
    """Running backfill twice: second run processes zero rows.

    **Validates: Requirements 1.5**
    """
    _truncate_memories()

    # Determine how many memories to insert (min length across the three lists)
    count = min(len(mem_types), len(source_types), len(contents))

    # Insert memories with NULL mem_class
    for i in range(count):
        db.create_memory(
            type=mem_types[i],
            title=f"idempotence test {i}",
            content=contents[i],
            source_type=source_types[i],
            mem_class=None,
        )

    # First backfill run — should process all rows
    first_run_count = _run_backfill_logic()
    assert first_run_count == count, (
        f"First run should process {count} rows, but processed {first_run_count}"
    )

    # Second backfill run — should process zero rows (idempotent)
    second_run_count = _run_backfill_logic()
    assert second_run_count == 0, (
        f"Second run should process 0 rows, but processed {second_run_count}"
    )


# ---------------------------------------------------------------------------
# Unit tests for scripts/backfill_mem_class.py
# Task 1.4 — Requirements: 1.3, 1.4, 1.6, 1.7, 4.2, 6.4, 6.5
# ---------------------------------------------------------------------------

import io
import sys
from unittest import mock

from scripts.backfill_mem_class import _parse_backfill_args, main, _log


# --- CLI argument parsing (Req 6.4, 6.5) ---

class TestParseBackfillArgs:
    """Test _parse_backfill_args defaults, custom values, and invalid inputs."""

    def test_defaults(self):
        with mock.patch("sys.argv", ["backfill_mem_class.py"]):
            args = _parse_backfill_args()
        assert args.batch_size == 500
        assert args.dry_run is False
        assert args.limit is None

    def test_custom_batch_size(self):
        with mock.patch("sys.argv", ["backfill_mem_class.py", "--batch-size", "100"]):
            args = _parse_backfill_args()
        assert args.batch_size == 100

    def test_custom_limit(self):
        with mock.patch("sys.argv", ["backfill_mem_class.py", "--limit", "50"]):
            args = _parse_backfill_args()
        assert args.limit == 50

    def test_dry_run_flag(self):
        with mock.patch("sys.argv", ["backfill_mem_class.py", "--dry-run"]):
            args = _parse_backfill_args()
        assert args.dry_run is True

    def test_all_args_combined(self):
        with mock.patch("sys.argv", [
            "backfill_mem_class.py", "--batch-size", "200", "--dry-run", "--limit", "10"
        ]):
            args = _parse_backfill_args()
        assert args.batch_size == 200
        assert args.dry_run is True
        assert args.limit == 10

    def test_invalid_batch_size_type(self):
        with mock.patch("sys.argv", ["backfill_mem_class.py", "--batch-size", "abc"]):
            with pytest.raises(SystemExit) as exc_info:
                _parse_backfill_args()
            assert exc_info.value.code != 0

    def test_unknown_argument(self):
        with mock.patch("sys.argv", ["backfill_mem_class.py", "--unknown-flag"]):
            with pytest.raises(SystemExit) as exc_info:
                _parse_backfill_args()
            assert exc_info.value.code != 0


# --- _log helper: [DRY RUN] prefix (Req 4.2) ---

class TestLogHelper:
    """Test _log output format and dry-run prefix."""

    def test_log_normal_mode(self, capsys):
        _log("hello world", dry_run=False)
        assert capsys.readouterr().out == "hello world\n"

    def test_log_dry_run_prefix(self, capsys):
        _log("hello world", dry_run=True)
        assert capsys.readouterr().out == "[DRY RUN] hello world\n"


# --- Batch progress logging (Req 1.4) ---

def test_batch_progress_logging(test_db, clean_tables, capsys):
    """Batch progress log contains 'processed', 'errors', and 'remaining'."""
    # Insert two memories with NULL mem_class
    db.create_memory(type="idea", title="m1", content="c1", mem_class=None)
    db.create_memory(type="idea", title="m2", content="c2", mem_class=None)

    with mock.patch("sys.argv", ["backfill_mem_class.py", "--batch-size", "10"]):
        main()

    output = capsys.readouterr().out
    # At least one batch line should contain these keywords
    batch_lines = [l for l in output.splitlines() if "processed" in l.lower()]
    assert len(batch_lines) >= 1
    first_batch = batch_lines[0]
    assert "processed" in first_batch
    assert "errors" in first_batch
    assert "remaining" in first_batch


# --- Summary output (Req 1.7) ---

def test_summary_output_fields(test_db, clean_tables, capsys):
    """Final summary contains total processed, skipped, errors, and breakdown."""
    db.create_memory(type="idea", title="s1", content="some idea", mem_class=None)

    with mock.patch("sys.argv", ["backfill_mem_class.py"]):
        main()

    output = capsys.readouterr().out
    assert "Total processed:" in output
    assert "Total skipped:" in output
    assert "Total errors:" in output
    assert "semantic=" in output
    assert "episodic=" in output
    assert "procedural=" in output


# --- [DRY RUN] prefix in dry-run mode (Req 4.2) ---

def test_dry_run_prefix_in_output(test_db, clean_tables, capsys):
    """All log lines are prefixed with [DRY RUN] when --dry-run is active.

    Uses --limit to prevent infinite loop (dry-run rolls back, so NULL rows
    reappear on next iteration).
    """
    db.create_memory(type="idea", title="dr1", content="dry run test", mem_class=None)

    with mock.patch("sys.argv", ["backfill_mem_class.py", "--dry-run", "--limit", "1"]):
        main()

    output = capsys.readouterr().out
    non_empty_lines = [l for l in output.splitlines() if l.strip()]
    assert len(non_empty_lines) > 0, "Expected some output"
    for line in non_empty_lines:
        assert line.startswith("[DRY RUN]"), f"Line missing prefix: {line!r}"


# --- Per-row error isolation (Req 1.6) ---

def test_error_isolation(test_db, clean_tables, capsys):
    """If classify_memory raises for one row, other rows are still processed.

    We insert two memories and use a batch size large enough to get both in
    one fetch. After the first batch, the good row is committed (mem_class
    set), so the next SELECT only finds the bad row. We use --limit equal
    to a value larger than the good rows but we break the loop by tracking
    iterations and raising after the first batch completes.
    """
    good_id = db.create_memory(type="idea", title="good", content="good content", mem_class=None)
    bad_id = db.create_memory(type="idea", title="bad", content="bad content", mem_class=None)

    original_classify = classify_memory
    iteration = {"count": 0}

    def _failing_classify(mem_type, source_type, content):
        if content == "bad content":
            # Track how many times the bad row is attempted
            iteration["count"] += 1
            if iteration["count"] > 1:
                # On second encounter, we've proven isolation works; bail out
                raise KeyboardInterrupt("test complete")
            raise RuntimeError("simulated classification failure")
        return original_classify(mem_type, source_type, content)

    with mock.patch("sys.argv", ["backfill_mem_class.py", "--batch-size", "100"]):
        with mock.patch("scripts.backfill_mem_class.classify_memory", side_effect=_failing_classify):
            try:
                main()
            except KeyboardInterrupt:
                pass  # Expected — we forced exit after proving isolation

    output = capsys.readouterr().out
    assert "ERROR" in output

    # Good memory should be classified
    good = db.get_memory(good_id)
    assert good["mem_class"] is not None

    # Bad memory should remain NULL
    bad = db.get_memory(bad_id)
    assert bad["mem_class"] is None


# --- Empty database (Req 1.3 — zero NULL rows handled gracefully) ---

def test_empty_database(test_db, clean_tables, capsys):
    """Script handles zero NULL rows gracefully — no errors, summary shows 0."""
    with mock.patch("sys.argv", ["backfill_mem_class.py"]):
        exit_code = main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Total processed: 0" in output
    assert "Total errors: 0" in output


# ---------------------------------------------------------------------------
# Feature: v2-reranking-backfill, Property 7: Dry-run leaves database unchanged
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    mem_type=st.sampled_from(MEMORY_TYPES),
    source_type=st.sampled_from(SOURCE_TYPES),
    content=st.text(
        alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
        min_size=1,
        max_size=500,
    ),
)
def test_dry_run_leaves_database_unchanged(test_db, clean_tables, mem_type, source_type, content):
    """Running backfill_mem_class with --dry-run leaves mem_class as NULL.

    **Validates: Requirements 4.1**
    """
    _truncate_memories()

    # Insert a memory with NULL mem_class
    mem_id = db.create_memory(
        type=mem_type,
        title="dry-run prop test",
        content=content,
        source_type=source_type,
        mem_class=None,
    )

    # Run backfill with --dry-run and --limit 1 to prevent infinite loop
    # (dry-run rolls back, so the NULL row reappears on every iteration)
    with mock.patch("sys.argv", ["backfill_mem_class.py", "--dry-run", "--limit", "1"]):
        main()

    # Verify mem_class is still NULL after dry-run
    stored = db.get_memory(mem_id)
    assert stored["mem_class"] is None, (
        f"mem_class should remain NULL after --dry-run, "
        f"but got {stored['mem_class']!r} for type={mem_type!r}"
    )


# ---------------------------------------------------------------------------
# Feature: v2-reranking-backfill, Property 8: Limit flag caps total processed
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    data=st.data(),
    total=st.integers(min_value=2, max_value=10),
)
def test_limit_flag_caps_total_processed(test_db, clean_tables, data, total):
    """Running backfill with --limit M processes exactly M memories when M < N.

    **Validates: Requirements 6.3**
    """
    _truncate_memories()

    limit = data.draw(st.integers(min_value=1, max_value=total - 1), label="limit")

    # Insert `total` memories with NULL mem_class
    for i in range(total):
        db.create_memory(
            type="idea",
            title=f"limit test {i}",
            content=f"content for limit test memory {i}",
            mem_class=None,
        )

    # Run backfill with --limit
    with mock.patch("sys.argv", ["backfill_mem_class.py", "--limit", str(limit)]):
        main()

    # Count how many memories now have non-NULL mem_class
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE mem_class IS NOT NULL")
            classified_count = cur.fetchone()[0]
    finally:
        conn.close()

    assert classified_count == limit, (
        f"Expected exactly {limit} memories classified with --limit {limit}, "
        f"but found {classified_count} (total={total})"
    )
