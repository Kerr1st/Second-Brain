# Implementation Plan: Question-Aware Search

## Overview

Implement weighted tsvector search by adding a PL/pgSQL questions parser, replacing the search_vector trigger with weighted setweight calls, backfilling existing rows, and adding a Python mirror of the parser for testing. All changes are contained in a single migration file and a Python utility function — no changes to search, ingest, or MCP server code.

## Tasks

- [x] 1. Create the SQL migration with PL/pgSQL parser, weighted trigger, and backfill
  - [x] 1.1 Create `migrations/005_question_weighted_search.sql` with the `extract_questions_text()` PL/pgSQL function
    - Implement `CREATE OR REPLACE FUNCTION extract_questions_text(content TEXT) RETURNS TABLE(questions_text TEXT, remaining_content TEXT)`
    - Handle: case-insensitive header match, inline queries after colon, `- ` and `* ` list items with marker stripping, header line kept in remaining_content
    - Return `('', '')` for NULL input, `('', content)` when no header found
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x] 1.2 Add the replacement `memories_search_vector_update()` trigger function to the migration
    - `CREATE OR REPLACE FUNCTION memories_search_vector_update()` that calls `extract_questions_text()`
    - Build `setweight(to_tsvector('english', coalesce(title,'') || ' ' || q_text), 'A') || setweight(to_tsvector('english', r_content), 'B')`
    - Handle NULL title/content via COALESCE
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 1.3 Add the backfill UPDATE statement to the migration
    - `UPDATE memories m SET search_vector = ... FROM LATERAL extract_questions_text(coalesce(m.content,'')) AS q`
    - LATERAL ensures per-row evaluation of the parser function
    - Handle NULL content with COALESCE
    - _Requirements: 3.5, 5.1, 5.2, 5.3, 5.4_

- [x] 2. Implement the Python `extract_questions()` function in `src/depth.py`
  - [x] 2.1 Add `extract_questions(content: str) -> tuple[str, str]` to `src/depth.py`
    - Mirror the PL/pgSQL parser behavior exactly: case-insensitive header, inline queries after colon, `- ` and `* ` list items with marker stripping, terminated by empty line or non-list content, header line kept in remaining_content
    - Return `('', content)` when no header found
    - Place alongside existing `_QUESTIONS_RE` regex
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 2.2 Write property test: round-trip word preservation (Property 1)
    - **Property 1: Round-trip word preservation**
    - Generate content strings with/without questions sections via Hypothesis. Verify the set of words in `questions_text` ∪ `remaining_content` equals the set of words in the original content minus list markers.
    - **Validates: Requirements 1.8, 8.1**

  - [x] 2.3 Write property test: list marker stripping (Property 2)
    - **Property 2: List marker stripping**
    - Generate content with questions sections containing `- ` and `* ` markers. Verify `questions_text` has no leading `- ` or `* ` markers.
    - **Validates: Requirements 1.5, 6.3**

- [x] 3. Checkpoint — Verify Python parser and migration SQL
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Create test file and write integration tests
  - [x] 4.1 Create `tests/test_question_search.py` with test DB fixtures and edge case tests
    - Test: no questions section → weight A on title only
    - Test: empty questions section (header but no list items)
    - Test: multiple questions headers (only first extracted)
    - Test: NULL content handling
    - Uses existing `test_db`, `clean_tables`, and `sample_memory_factory` fixtures from `conftest.py`
    - _Requirements: 8.5, 2.2, 5.4_

  - [x] 4.2 Write integration test: trigger populates weighted search_vector on INSERT
    - Insert a memory with a questions section, verify search_vector contains Weight A and Weight B lexemes
    - _Requirements: 8.3, 2.1_

  - [x] 4.3 Write integration test: ts_rank scores questions-match higher than body-match
    - Insert two memories with same query terms — one in questions section, one in body only. Verify ts_rank returns higher score for the questions-section memory.
    - _Requirements: 4.1, 4.3, 8.2_

  - [x] 4.4 Write integration test: backfill corrects old-style unweighted search_vector
    - Insert a memory with a questions section. Manually overwrite its search_vector to the old unweighted style (`to_tsvector('english', title || ' ' || content)` with no weight labels). Run the backfill UPDATE. Verify the search_vector now contains Weight A and Weight B lexemes matching a fresh INSERT.
    - _Requirements: 5.2, 8.4_

  - [x] 4.5 Write integration test: migration is idempotent
    - Apply migration 005 twice, verify no errors
    - _Requirements: 3.4_

  - [x] 4.6 Write property test: cross-implementation equivalence (Property 3)
    - **Property 3: Cross-implementation equivalence**
    - Generate random content strings. Run both Python `extract_questions()` and PL/pgSQL `extract_questions_text()` via test DB. Compare outputs.
    - **Validates: Requirements 6.2, 6.5**

  - [x] 4.7 Write property test: backfill–trigger consistency (Property 4)
    - **Property 4: Backfill–trigger consistency**
    - Generate random (title, content) pairs. Insert via trigger, then run backfill SQL. Compare search_vector values.
    - **Validates: Requirements 5.2, 8.4**

- [x] 5. Final checkpoint — Ensure all 243+ existing tests and new tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- No changes to `src/search.py`, `src/mcp_server.py`, or `src/ingest.py` (Requirements 7.1, 7.2, 7.3)
- The existing `test_db` fixture in `conftest.py` auto-applies migration 005 via sorted glob — no fixture changes needed
- Property tests use Hypothesis (already in `requirements.txt`)
- Properties 1 and 2 are pure Python (no DB needed); Properties 3 and 4 require test DB
