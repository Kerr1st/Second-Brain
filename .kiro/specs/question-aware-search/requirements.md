# Requirements Document: Question-Aware Search

## Introduction

Question-Aware Search gives the "Questions this answers:" section extra weight in BM25 full-text search so that memories are findable by the natural-language questions they answer, not just by their body content. The approach uses PostgreSQL tsvector weight labels (A/B/C/D) within the existing `search_vector` column — no new columns, no new search pass, no schema migration for new columns.

The `search_vector` trigger in `memories_search_vector_update()` currently builds an unweighted tsvector from `title || content`. This spec changes the trigger to assign weight 'A' to the title and the extracted "Questions this answers:" text, and weight 'B' to the remaining content. The built-in `ts_rank` function automatically prioritizes matches in higher-weighted lexemes, so `hybrid_search` benefits without code changes to the ranking call.

A backfill step updates all existing memories' `search_vector` to apply the new weighting. A parser function in PL/pgSQL extracts the questions section from content at write time inside the trigger.

## Glossary

- **Search_Vector**: The `search_vector` TSVECTOR column on the `memories` table, populated by a BEFORE INSERT/UPDATE trigger, used for BM25 full-text search in Hybrid_Search.
- **Search_Vector_Trigger**: The `memories_search_vector_update()` PL/pgSQL function and its associated `trg_memories_search_vector` trigger that auto-populates Search_Vector on INSERT or UPDATE of title/content.
- **Questions_Section**: The "Questions this answers:" block in a memory's content, consisting of a header line followed by bullet-list items (lines starting with "- " or "* "), terminated by an empty line or non-list content.
- **Weight_A**: The highest tsvector weight label in PostgreSQL, assigned to lexemes from the title and Questions_Section so that `ts_rank` scores matches in these sections higher.
- **Weight_B**: The second-highest tsvector weight label, assigned to lexemes from the remaining content (everything outside the Questions_Section).
- **Hybrid_Search**: The `hybrid_search()` function in `src/search.py` that combines pgvector cosine similarity search with PostgreSQL full-text search via Reciprocal Rank Fusion (RRF).
- **Backfill**: A one-time UPDATE statement in the migration that recomputes Search_Vector for all existing rows using the new weighted trigger logic.
- **Questions_Parser**: A PL/pgSQL helper function (`extract_questions_text()`) that extracts the Questions_Section text from content, returning the questions text and the remaining content as separate values.
- **Memory**: A row in the `memories` PostgreSQL table representing a single unit of knowledge.

## Requirements

### Requirement 1: PL/pgSQL Questions Parser

**User Story:** As a developer, I want a PL/pgSQL function that extracts the "Questions this answers:" section from memory content so that the trigger can assign different tsvector weights to questions versus body text.

#### Acceptance Criteria

1. THE Questions_Parser SHALL accept a TEXT parameter (the memory content) and return two TEXT values: the extracted questions text and the remaining content.
2. WHEN the content contains a line starting with "Questions this answers:" (case-insensitive), THE Questions_Parser SHALL extract subsequent lines starting with "- " or "* " as the questions text.
3. WHEN the Questions_Section is terminated by an empty line or a line that does not start with "- " or "* ", THE Questions_Parser SHALL stop extracting questions at that point.
4. WHEN the content does not contain a "Questions this answers:" header, THE Questions_Parser SHALL return an empty string for the questions text and the full content as the remaining content.
5. THE Questions_Parser SHALL strip the list markers ("- " or "* ") from each question line before including the text in the questions output.
6. THE Questions_Parser SHALL also extract inline queries appearing after the colon on the header line itself (e.g., "Questions this answers: How do I X?").
7. THE Questions_Parser SHALL keep the header line ("Questions this answers:") in the remaining_content output so that no words are lost from the original content except list markers.
8. FOR ALL valid content strings, parsing the content into (questions_text, remaining_content) and concatenating them SHALL preserve all words present in the original content except list markers ("- ", "* ") — round-trip word preservation.

### Requirement 2: Weighted Search Vector Trigger

**User Story:** As a developer, I want the `search_vector` trigger to assign weight 'A' to the title and questions text and weight 'B' to the remaining content so that `ts_rank` automatically prioritizes matches in the questions section.

#### Acceptance Criteria

1. WHEN a memory is inserted or its title/content is updated, THE Search_Vector_Trigger SHALL build the Search_Vector as: `setweight(to_tsvector('english', title || ' ' || questions_text), 'A') || setweight(to_tsvector('english', remaining_content), 'B')`.
2. WHEN the content has no Questions_Section, THE Search_Vector_Trigger SHALL assign weight 'A' to the title only and weight 'B' to the full content.
3. WHEN the content has a Questions_Section, THE Search_Vector_Trigger SHALL call the Questions_Parser to separate questions text from remaining content.
4. THE Search_Vector_Trigger SHALL replace the existing `memories_search_vector_update()` function using `CREATE OR REPLACE FUNCTION`.
5. THE Search_Vector_Trigger SHALL handle NULL title or NULL content values using COALESCE, consistent with the current trigger behavior.

### Requirement 3: Migration

**User Story:** As a developer, I want the trigger update and backfill delivered as a single numbered SQL migration so that it integrates with the existing `migrate.sh` system and is applied idempotently.

#### Acceptance Criteria

1. THE migration SHALL be numbered `005_question_weighted_search.sql` and placed in the `migrations/` directory.
2. THE migration SHALL create the Questions_Parser function using `CREATE OR REPLACE FUNCTION`.
3. THE migration SHALL replace the Search_Vector_Trigger function using `CREATE OR REPLACE FUNCTION`.
4. THE migration SHALL be idempotent — re-running the migration SHALL produce no errors.
5. THE migration SHALL include a backfill UPDATE that recomputes Search_Vector for all existing rows using the new weighted trigger logic.
6. THE migration SHALL NOT add any new columns, indexes, or tables to the schema.

### Requirement 4: Search Ranking Verification

**User Story:** As a developer, I want to verify that `ts_rank` in `hybrid_search` respects the new tsvector weights so that queries matching questions rank higher than queries matching only body content.

#### Acceptance Criteria

1. WHEN `hybrid_search` calls `ts_rank(search_vector, query)`, THE `ts_rank` function SHALL return a higher score for a memory where the query matches Weight_A lexemes (questions/title) than for a memory where the same query matches only Weight_B lexemes (body content).
2. THE `hybrid_search` function in `src/search.py` SHALL require no code changes — the existing `ts_rank` call already respects tsvector weights.
3. WHEN two memories contain the same query terms but one has those terms in the Questions_Section and the other has them only in the body, THE memory with the terms in the Questions_Section SHALL receive a higher BM25 rank from `ts_rank`.

### Requirement 5: Backfill Existing Memories

**User Story:** As a developer, I want all existing memories' search vectors recomputed with the new weighting so that the improved ranking applies retroactively, not just to newly created memories.

#### Acceptance Criteria

1. WHEN the migration is applied, THE backfill SHALL update the Search_Vector for every row in the `memories` table.
2. THE backfill SHALL use the same Questions_Parser and weighting logic as the trigger, ensuring consistency between backfilled and newly inserted rows.
3. THE backfill SHALL compute the weighted tsvector inline using the Questions_Parser function directly in the UPDATE statement (e.g., `UPDATE memories SET search_vector = <weighted_tsvector_expression>`), NOT by triggering the trigger via a dummy content update. This is explicit and avoids relying on trigger side effects.
4. IF a memory's content is NULL, THEN THE backfill SHALL produce a Search_Vector equivalent to `setweight(to_tsvector('english', coalesce(title, '')), 'A')`.

### Requirement 6: Python Questions Extractor

**User Story:** As a developer, I want a Python utility function that extracts the "Questions this answers:" section from content so that Python code (tests, depth scorer) can reuse the same parsing logic without duplicating it.

#### Acceptance Criteria

1. THE Python Questions Extractor SHALL be a function in `src/depth.py` (alongside the existing `_QUESTIONS_RE` regex) that accepts a content string and returns a tuple of (questions_text, remaining_content).
2. THE Python Questions Extractor SHALL follow the same parsing rules as the PL/pgSQL Questions_Parser: case-insensitive header match, inline queries after the colon on the header line (per R1.6), "- " or "* " list items with marker stripping, terminated by empty line or non-list content, and header line kept in remaining_content (per R1.7).
3. THE Python Questions Extractor SHALL strip list markers ("- " or "* ") from each question line.
4. WHEN the content has no Questions_Section, THE Python Questions Extractor SHALL return an empty string for questions_text and the full content as remaining_content.
5. FOR ALL valid content strings, THE Python Questions Extractor and the PL/pgSQL Questions_Parser SHALL produce equivalent (questions_text, remaining_content) splits (behavioral equivalence across implementations).

### Requirement 7: No Changes Required to MCP Server or Ingest Pipeline

**User Story:** As a developer, I want to confirm that `mcp_server.py` and `ingest.py` require zero code changes so that the feature is entirely contained in the migration and the Python test/utility layer.

#### Acceptance Criteria

1. THE `src/mcp_server.py` module SHALL require no code changes — `memory_create` calls `create_memory()` which fires the trigger, and the trigger handles the weighted tsvector generation.
2. THE `src/ingest.py` module SHALL require no code changes — `ingest_content()` calls `create_memory()` which fires the trigger.
3. THE `src/search.py` `hybrid_search()` function SHALL require no code changes — the existing `ts_rank(search_vector, query)` call already respects tsvector weights.

### Requirement 8: Tests

**User Story:** As a developer, I want tests that verify the questions extraction, weighted trigger behavior, and ranking improvement so that I can be confident the feature works correctly and doesn't regress.

#### Acceptance Criteria

1. THE test suite SHALL include a property-based test verifying that the Python Questions Extractor round-trips: for all generated content strings containing a Questions_Section, all words from the original content appear in either the questions_text or remaining_content output.
2. THE test suite SHALL include a test verifying that a memory with query terms in the Questions_Section receives a higher `ts_rank` score than a memory with the same terms only in the body content (requires Test_DB).
3. THE test suite SHALL include a test verifying that the trigger correctly populates Search_Vector with weighted lexemes after INSERT (requires Test_DB).
4. THE test suite SHALL include a test verifying that the backfill produces the same Search_Vector as a fresh INSERT with the same content (requires Test_DB).
5. THE test suite SHALL include edge case tests for: content with no Questions_Section, content with an empty Questions_Section (header but no list items), and content with multiple Questions_Section headers (only the first is extracted).
6. THE Test_DB fixture (session-scoped) SHALL apply the new migration `005_question_weighted_search.sql` so that the trigger and parser function are available in the test database for AC 2–4.
