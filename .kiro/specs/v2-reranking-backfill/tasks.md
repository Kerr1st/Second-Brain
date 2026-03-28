# Implementation Plan: V2 Reranking Backfill

## Overview

Three independent, idempotent backfill scripts that populate `mem_class`, `depth_score`, and `last_accessed_at` for all existing memories. Each Python script shares a consistent CLI (`--batch-size`, `--dry-run`, `--limit`), uses batch-commit with per-row error isolation, and produces structured logging. The SQL script is a standalone file. Property-based tests use Hypothesis.

## Tasks

- [x] 0. Capture baseline retrieval metrics
  - Run `python scripts/golden_queries.py` and save output (MRR, hit rates at 1/3/5/10) before any backfill runs
  - This is a manual step — no code changes, just capture the "before" numbers
  - _Requirements: 5.1_

- [x] 1. Implement `backfill_mem_class.py`
  - [x] 1.1 Create `scripts/backfill_mem_class.py` with CLI parser and batch processing loop
    - Add `_parse_backfill_args()` with `--batch-size` (default 500), `--dry-run`, `--limit` arguments
    - Implement main loop: query memories WHERE `mem_class IS NULL ORDER BY id LIMIT batch_size`
    - For each row, call `classify_memory(type, source_type, content)` from `src/classify.py`
    - Batch UPDATE `mem_class` column, commit per batch (rollback if `--dry-run`)
    - Per-row try/except: log error with memory ID, increment error counter, continue
    - Log progress after each batch (processed count, remaining count)
    - Print final summary: total processed, skipped, errors, breakdown by classification (semantic, episodic, procedural)
    - Prefix all log output with `[DRY RUN]` when `--dry-run` is active
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 4.1, 4.2, 4.3, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 1.2 Write property test: mem_class backfill consistency (Property 1)
    - **Property 1: mem_class backfill consistency**
    - Generate random memories with varied `type`, `source_type`, `content` using `st.sampled_from(types) × st.text()`
    - Run backfill, verify stored `mem_class` equals `classify_memory(type, source_type, content)`
    - File: `tests/test_backfill_mem_class.py`
    - **Validates: Requirements 1.1, 1.2**

  - [x] 1.3 Write property test: mem_class backfill idempotence (Property 4)
    - **Property 4: mem_class backfill idempotence**
    - Generate memories, run backfill twice, verify second run processes zero rows
    - File: `tests/test_backfill_mem_class.py`
    - **Validates: Requirements 1.5**

  - [x] 1.4 Write unit tests for `backfill_mem_class.py`
    - Test CLI argument parsing: defaults, custom values, invalid inputs
    - Test batch progress logging output format
    - Test summary output contains all required fields
    - Test `[DRY RUN]` prefix in dry-run mode
    - Test per-row error isolation (inject failing memory, verify others still processed)
    - Test empty database (zero NULL rows) handled gracefully
    - File: `tests/test_backfill_mem_class.py`
    - _Requirements: 1.3, 1.4, 1.6, 1.7, 4.2, 6.4, 6.5_

- [x] 2. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implement `backfill_depth_score.py`
  - [x] 3.1 Create `scripts/backfill_depth_score.py` with CLI parser and batch processing loop
    - Add `_parse_backfill_args()` with `--batch-size` (default 500), `--dry-run`, `--limit` arguments
    - Implement main loop: query memories WHERE `metadata->>'depth_score' IS NULL ORDER BY id LIMIT batch_size`
    - For each row, call `compute_depth_score(content)` from `src/depth.py`
    - Python-side JSONB merge: read existing metadata dict, add `depth_score` key, write back full dict
    - Batch UPDATE `metadata` column, commit per batch (rollback if `--dry-run`)
    - Per-row try/except: log error with memory ID, increment error counter, continue
    - Log progress after each batch (processed count, remaining count)
    - Print final summary: total processed, skipped, errors, min/max/mean depth scores
    - Prefix all log output with `[DRY RUN]` when `--dry-run` is active
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 4.1, 4.2, 4.3, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 3.2 Write property test: depth_score backfill consistency (Property 2)
    - **Property 2: depth_score backfill consistency**
    - Generate random memories with varied `content` using `st.text(min_size=0, max_size=5000)`
    - Run backfill, verify stored `metadata->>'depth_score'` equals `compute_depth_score(content)`
    - File: `tests/test_backfill_depth_score.py`
    - **Validates: Requirements 2.1**

  - [x] 3.3 Write property test: metadata preservation during depth_score merge (Property 3)
    - **Property 3: Metadata preservation during depth_score merge**
    - Generate random existing metadata dicts using `st.dictionaries(st.text(), st.text())`
    - Run depth_score backfill, verify all pre-existing metadata keys and values preserved unchanged
    - File: `tests/test_backfill_depth_score.py`
    - **Validates: Requirements 2.2**

  - [x] 3.4 Write property test: depth_score backfill idempotence (Property 5)
    - **Property 5: depth_score backfill idempotence**
    - Generate memories, run backfill twice, verify second run processes zero rows
    - File: `tests/test_backfill_depth_score.py`
    - **Validates: Requirements 2.5**

  - [x] 3.5 Write unit tests for `backfill_depth_score.py`
    - Test CLI argument parsing: defaults, custom values, invalid inputs
    - Test batch progress logging output format
    - Test summary output contains min/max/mean statistics
    - Test `[DRY RUN]` prefix in dry-run mode
    - Test per-row error isolation (inject failing memory, verify others still processed)
    - Test empty database (zero NULL rows) handled gracefully
    - Test metadata merge preserves existing keys (specific example)
    - File: `tests/test_backfill_depth_score.py`
    - _Requirements: 2.3, 2.4, 2.6, 2.7, 4.2, 6.4, 6.5_

- [x] 4. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement `backfill_last_accessed.sql` and cross-script property tests
  - [x] 5.1 Create `scripts/backfill_last_accessed.sql`
    - Include `SELECT count(*) AS rows_to_update FROM memories WHERE last_accessed_at IS NULL;`
    - Include `UPDATE memories SET last_accessed_at = updated_at WHERE last_accessed_at IS NULL;`
    - Add comments explaining purpose and idempotent behavior
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x] 5.2 Write property test: last_accessed_at backfill idempotence (Property 6)
    - **Property 6: last_accessed_at backfill idempotence**
    - Insert memories with NULL `last_accessed_at` and varied `updated_at` using `st.datetimes()`
    - Run SQL UPDATE twice, verify second execution affects zero rows
    - File: `tests/test_backfill_last_accessed.py`
    - **Validates: Requirements 3.2**

  - [x] 5.3 Write property test: dry-run leaves database unchanged (Property 7)
    - **Property 7: Dry-run leaves database unchanged**
    - Generate memories with NULL `mem_class` or NULL `depth_score`
    - Run corresponding backfill script with `--dry-run`, verify all values still NULL
    - File: `tests/test_backfill_mem_class.py`
    - **Validates: Requirements 4.1**

  - [x] 5.4 Write property test: limit flag caps total processed (Property 8)
    - **Property 8: Limit flag caps total processed**
    - Generate N memories, run with `--limit M` where M < N using `st.integers(min_value=1, max_value=50)`
    - Verify exactly M memories processed
    - File: `tests/test_backfill_mem_class.py`
    - **Validates: Requirements 6.3**

  - [x] 5.5 Write unit tests for `backfill_last_accessed.sql`
    - Verify SQL file contains the SELECT count(*) and UPDATE statements
    - Test UPDATE sets `last_accessed_at = updated_at` for NULL rows
    - Test non-NULL rows are not modified
    - Test idempotence: second run affects zero rows
    - File: `tests/test_backfill_last_accessed.py`
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 6. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Capture post-backfill retrieval metrics and compare
  - Run `python scripts/golden_queries.py` and compare output against baseline from Task 0
  - This is a manual step — no code changes, just capture the "after" numbers and diff
  - _Requirements: 5.2_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis with `@settings(max_examples=100)`
- All tests use existing `test_db`, `clean_tables`, and `sample_memory_factory` fixtures from `conftest.py`
- `scripts/golden_queries.py` baseline and post-backfill runs are captured in Tasks 0 and 7 (Requirement 5) — no code changes needed
- CLI parser is duplicated in each Python script per design decision (no shared module)
