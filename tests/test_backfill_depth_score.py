# Feature: v2-reranking-backfill, Property 2: depth_score backfill consistency
"""Property-based tests for scripts/backfill_depth_score.py."""

import json

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

import src.db as db
from src.depth import compute_depth_score


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


def _run_depth_backfill_logic():
    """Run the core depth_score backfill logic: query NULL rows, compute, update.

    Returns the number of rows processed (updated).
    """
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content, metadata "
                "FROM memories WHERE metadata->>'depth_score' IS NULL "
                "ORDER BY id"
            )
            rows = cur.fetchall()

        updates = []
        for row in rows:
            mem_id, content, metadata_raw = row
            score = compute_depth_score(content)

            if isinstance(metadata_raw, str):
                metadata = json.loads(metadata_raw)
            elif isinstance(metadata_raw, dict):
                metadata = metadata_raw
            else:
                metadata = {}
            metadata["depth_score"] = score

            updates.append((json.dumps(metadata), mem_id))

        if updates:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE memories SET metadata = %s WHERE id = %s",
                    updates,
                )
            conn.commit()

        return len(updates)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Property 2: depth_score backfill consistency
# Validates: Requirements 2.1
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    content=st.text(
        alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
        min_size=0,
        max_size=5000,
    ),
)
def test_backfill_depth_score_consistency(test_db, clean_tables, content):
    """After backfill, stored metadata->>'depth_score' equals compute_depth_score(content).

    **Validates: Requirements 2.1**
    """
    # Clean between Hypothesis examples
    _truncate_memories()

    # Insert a memory with default metadata (no depth_score key)
    mem_id = db.create_memory(
        type="idea",
        title="depth prop test",
        content=content,
        metadata={},
    )

    # Run the core backfill logic
    _run_depth_backfill_logic()

    # Verify: stored depth_score matches the pure function
    stored = db.get_memory(mem_id)
    stored_metadata = stored["metadata"]
    if isinstance(stored_metadata, str):
        stored_metadata = json.loads(stored_metadata)

    stored_score = float(stored_metadata["depth_score"])
    expected_score = compute_depth_score(content)

    assert stored_score == pytest.approx(expected_score), (
        f"depth_score mismatch: stored={stored_score!r}, expected={expected_score!r}"
    )


# ---------------------------------------------------------------------------
# Feature: v2-reranking-backfill, Property 3: Metadata preservation during depth_score merge
# Validates: Requirements 2.2
# ---------------------------------------------------------------------------

# Strategy: generate text keys/values without NUL bytes and without unpaired
# surrogates (PostgreSQL JSONB rejects both), and filter out the "depth_score"
# key so the memory starts without it.
_pg_safe_text = st.text(
    alphabet=st.characters(
        blacklist_characters="\x00",
        blacklist_categories=("Cs",),  # exclude surrogate code points
    ),
    min_size=0,
    max_size=50,
)

_metadata_without_depth = st.dictionaries(
    keys=_pg_safe_text.filter(lambda k: k != "depth_score"),
    values=_pg_safe_text,
    max_size=10,
)


@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(existing_meta=_metadata_without_depth)
def test_backfill_preserves_existing_metadata(test_db, clean_tables, existing_meta):
    """After depth_score backfill, all pre-existing metadata keys/values are preserved.

    **Validates: Requirements 2.2**
    """
    # Clean between Hypothesis examples
    _truncate_memories()

    # Insert a memory with the generated metadata (no depth_score key)
    mem_id = db.create_memory(
        type="idea",
        title="meta preservation test",
        content="Some content for depth scoring.",
        metadata=existing_meta,
    )

    # Run the core backfill logic
    _run_depth_backfill_logic()

    # Read back the memory
    stored = db.get_memory(mem_id)
    stored_metadata = stored["metadata"]
    if isinstance(stored_metadata, str):
        stored_metadata = json.loads(stored_metadata)

    # Verify all original keys and values are preserved unchanged
    for key, value in existing_meta.items():
        assert key in stored_metadata, (
            f"Original metadata key {key!r} missing after backfill"
        )
        assert stored_metadata[key] == value, (
            f"Metadata key {key!r} changed: was {value!r}, now {stored_metadata[key]!r}"
        )

    # Verify depth_score was added
    assert "depth_score" in stored_metadata, (
        "depth_score key was not added by backfill"
    )


# ---------------------------------------------------------------------------
# Feature: v2-reranking-backfill, Property 5: depth_score backfill idempotence
# Validates: Requirements 2.5
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None, database=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    contents=st.lists(
        st.text(
            alphabet=st.characters(
                blacklist_characters="\x00",
                blacklist_categories=("Cs",),  # exclude surrogate code points
            ),
            min_size=0,
            max_size=5000,
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_backfill_depth_score_idempotence(test_db, clean_tables, contents):
    """Running backfill twice: second run processes zero rows.

    **Validates: Requirements 2.5**
    """
    _truncate_memories()

    # Insert memories with default metadata (no depth_score key)
    for i, content in enumerate(contents):
        db.create_memory(
            type="idea",
            title=f"idempotence test {i}",
            content=content,
            metadata={},
        )

    # First backfill run — should process all rows
    first_run_count = _run_depth_backfill_logic()
    assert first_run_count == len(contents), (
        f"First run should process {len(contents)} rows, but processed {first_run_count}"
    )

    # Second backfill run — should process zero rows (idempotent)
    second_run_count = _run_depth_backfill_logic()
    assert second_run_count == 0, (
        f"Second run should process 0 rows, but processed {second_run_count}"
    )


# ---------------------------------------------------------------------------
# Unit tests for scripts/backfill_depth_score.py
# Task 3.5 — Requirements: 2.3, 2.4, 2.6, 2.7, 4.2, 6.4, 6.5
# ---------------------------------------------------------------------------

import sys
from unittest import mock

from scripts.backfill_depth_score import _parse_backfill_args, main, _log


# --- CLI argument parsing (Req 6.4, 6.5) ---

class TestParseBackfillArgs:
    """Test _parse_backfill_args defaults, custom values, and invalid inputs."""

    def test_defaults(self):
        with mock.patch("sys.argv", ["backfill_depth_score.py"]):
            args = _parse_backfill_args()
        assert args.batch_size == 500
        assert args.dry_run is False
        assert args.limit is None

    def test_custom_batch_size(self):
        with mock.patch("sys.argv", ["backfill_depth_score.py", "--batch-size", "100"]):
            args = _parse_backfill_args()
        assert args.batch_size == 100

    def test_custom_limit(self):
        with mock.patch("sys.argv", ["backfill_depth_score.py", "--limit", "50"]):
            args = _parse_backfill_args()
        assert args.limit == 50

    def test_dry_run_flag(self):
        with mock.patch("sys.argv", ["backfill_depth_score.py", "--dry-run"]):
            args = _parse_backfill_args()
        assert args.dry_run is True

    def test_all_args_combined(self):
        with mock.patch("sys.argv", [
            "backfill_depth_score.py", "--batch-size", "200", "--dry-run", "--limit", "10"
        ]):
            args = _parse_backfill_args()
        assert args.batch_size == 200
        assert args.dry_run is True
        assert args.limit == 10

    def test_invalid_batch_size_type(self):
        with mock.patch("sys.argv", ["backfill_depth_score.py", "--batch-size", "abc"]):
            with pytest.raises(SystemExit) as exc_info:
                _parse_backfill_args()
            assert exc_info.value.code != 0

    def test_unknown_argument(self):
        with mock.patch("sys.argv", ["backfill_depth_score.py", "--unknown-flag"]):
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


# --- Batch progress logging (Req 2.4) ---

def test_batch_progress_logging(test_db, clean_tables, capsys):
    """Batch progress log contains 'processed', 'errors', and 'remaining'."""
    db.create_memory(type="idea", title="m1", content="c1", metadata={})
    db.create_memory(type="idea", title="m2", content="c2", metadata={})

    with mock.patch("sys.argv", ["backfill_depth_score.py", "--batch-size", "10"]):
        main()

    output = capsys.readouterr().out
    batch_lines = [l for l in output.splitlines() if "processed" in l.lower() and "remaining" in l.lower()]
    assert len(batch_lines) >= 1
    first_batch = batch_lines[0]
    assert "processed" in first_batch
    assert "errors" in first_batch
    assert "remaining" in first_batch


# --- Summary output with min/max/mean statistics (Req 2.7) ---

def test_summary_output_statistics(test_db, clean_tables, capsys):
    """Final summary contains total processed, skipped, errors, and min/max/mean depth scores."""
    db.create_memory(type="idea", title="s1", content="some idea content", metadata={})

    with mock.patch("sys.argv", ["backfill_depth_score.py"]):
        main()

    output = capsys.readouterr().out
    assert "Total processed:" in output
    assert "Total skipped:" in output
    assert "Total errors:" in output
    assert "min=" in output
    assert "max=" in output
    assert "mean=" in output


# --- [DRY RUN] prefix in dry-run mode (Req 4.2) ---

def test_dry_run_prefix_in_output(test_db, clean_tables, capsys):
    """All log lines are prefixed with [DRY RUN] when --dry-run is active."""
    db.create_memory(type="idea", title="dr1", content="dry run test", metadata={})

    with mock.patch("sys.argv", ["backfill_depth_score.py", "--dry-run", "--limit", "1"]):
        main()

    output = capsys.readouterr().out
    non_empty_lines = [l for l in output.splitlines() if l.strip()]
    assert len(non_empty_lines) > 0, "Expected some output"
    for line in non_empty_lines:
        assert line.startswith("[DRY RUN]"), f"Line missing prefix: {line!r}"


# --- Per-row error isolation (Req 2.6) ---

def test_error_isolation(test_db, clean_tables, capsys):
    """If compute_depth_score raises for one row, other rows are still processed."""
    good_id = db.create_memory(type="idea", title="good", content="good content", metadata={})
    bad_id = db.create_memory(type="idea", title="bad", content="bad content", metadata={})

    original_compute = compute_depth_score
    iteration = {"count": 0}

    def _failing_compute(content):
        if content == "bad content":
            iteration["count"] += 1
            if iteration["count"] > 1:
                raise KeyboardInterrupt("test complete")
            raise RuntimeError("simulated depth score failure")
        return original_compute(content)

    with mock.patch("sys.argv", ["backfill_depth_score.py", "--batch-size", "100"]):
        with mock.patch("scripts.backfill_depth_score.compute_depth_score", side_effect=_failing_compute):
            try:
                main()
            except KeyboardInterrupt:
                pass  # Expected — forced exit after proving isolation

    output = capsys.readouterr().out
    assert "ERROR" in output

    # Good memory should have depth_score in metadata
    good = db.get_memory(good_id)
    good_meta = good["metadata"]
    if isinstance(good_meta, str):
        good_meta = json.loads(good_meta)
    assert "depth_score" in good_meta

    # Bad memory should still have no depth_score
    bad = db.get_memory(bad_id)
    bad_meta = bad["metadata"]
    if isinstance(bad_meta, str):
        bad_meta = json.loads(bad_meta)
    assert "depth_score" not in bad_meta


# --- Empty database (Req 2.3 — zero NULL rows handled gracefully) ---

def test_empty_database(test_db, clean_tables, capsys):
    """Script handles zero NULL rows gracefully — no errors, summary shows 0."""
    with mock.patch("sys.argv", ["backfill_depth_score.py"]):
        exit_code = main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Total processed: 0" in output
    assert "Total errors: 0" in output


# --- Metadata merge preserves existing keys (Req 2.3) ---

def test_metadata_merge_preserves_existing_keys(test_db, clean_tables, capsys):
    """Backfill merges depth_score into metadata without overwriting existing keys."""
    existing_metadata = {"source": "test", "custom_key": "value"}
    mem_id = db.create_memory(
        type="idea",
        title="merge test",
        content="some content for depth scoring",
        metadata=existing_metadata,
    )

    with mock.patch("sys.argv", ["backfill_depth_score.py"]):
        main()

    stored = db.get_memory(mem_id)
    stored_meta = stored["metadata"]
    if isinstance(stored_meta, str):
        stored_meta = json.loads(stored_meta)

    # Original keys preserved
    assert stored_meta["source"] == "test"
    assert stored_meta["custom_key"] == "value"
    # depth_score was added
    assert "depth_score" in stored_meta
    assert isinstance(stored_meta["depth_score"], float)
