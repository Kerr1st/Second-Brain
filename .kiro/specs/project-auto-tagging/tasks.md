# Implementation Plan: Project Auto-Tagging

## Overview

Wire the existing but inert `project` column through the full pipeline: shared normalization function → parser extraction → chat extract passthrough → ingestion storage → MCP normalization → backfill script. All project tags flow through `normalize_project_tag()` in `src/project.py`.

## Tasks

- [x] 1. Create shared normalization module `src/project.py`
  - [x] 1.1 Implement `normalize_project_tag()` function
    - Create `src/project.py` with the pure `normalize_project_tag(raw)` function
    - Handle: `None` → `None`, strip whitespace, lowercase, extract final path component (split on `/` and `\`), empty → `None`, dot-prefixed → `None`, absolute paths (starting with `/`) with < 3 components → `None`, non-string types → `None`. The < 3 components rule does NOT apply to relative paths or bare directory names (e.g., `RetailStore` from IDE fileTree).
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x] 1.2 Write property test: normalization is idempotent and lowercase
    - **Property 1: Normalization is idempotent and lowercase**
    - Generate random strings (paths, dot-prefixed, whitespace, empty, Unicode). Verify `normalize(normalize(x)) == normalize(x)` and that non-None output is lowercase, no whitespace, no path separators, no dot prefix, non-empty
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6**

  - [x] 1.3 Write property test: normalization excludes dot-prefixed and short paths
    - **Property 2: Normalization excludes dot-prefixed and short paths**
    - Generate dot-prefixed final components and absolute paths with < 3 components. Verify all return `None`
    - **Validates: Requirements 8.5, 8.6**

  - [x] 1.4 Write unit tests for `normalize_project_tag()` with exact examples from design
    - Test all examples from the Data Models table: `None`, `""`, `"  "`, `"RetailStore"`, paths, home dirs, dot-prefixed, etc.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [x] 2. Wire IDE chat parser to normalize project extraction
  - [x] 2.1 Modify `extract_project_context()` in `src/parsers/ide_chat.py`
    - Import `normalize_project_tag` from `src.project`
    - Apply `normalize_project_tag()` to the extracted `project_hint` before returning it in the metadata dict
    - Existing `format_as_markdown()` already emits `Project:` header when `project_hint` is truthy — no change needed there
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 8.1, 8.3, 8.5_

  - [x] 2.2 Write property test: IDE parser extraction preserves normalized project
    - **Property 3: IDE parser extraction preserves normalized project**
    - Generate random `.chat` data structures with varying `fileTree` contexts. Verify `project_hint` matches `normalize_project_tag()` applied to the first expanded path's top-level directory
    - **Validates: Requirements 1.1, 1.2, 1.3**

  - [x] 2.3 Write unit test for IDE parser with a real `.chat` JSON fixture
    - Test `extract_project_context()` with fileTree present, absent, and with dot-prefixed paths
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 3. Wire CLI chat parser to extract and emit project
  - [x] 3.1 Modify `src/parsers/cli_chat.py` to extract project from `conversation_id`
    - Import `normalize_project_tag` from `src.project`
    - In `parse_conversation()`: extract project via `normalize_project_tag(conversation_id)`
    - Modify `format_as_markdown()` to accept optional `project` parameter and emit `Project:` header when non-None
    - Modify `parse_conversation()` to return 3-tuple `(conv_id, markdown, project)` instead of 2-tuple
    - Modify `parse_all()` to yield 3-tuples
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 8.2, 8.3, 8.6_

  - [x] 3.2 Update `scripts/chat_extract.py` to unpack CLI 3-tuples
    - Update the CLI chat loop to unpack `(conv_id, markdown, project)` from `parse_all()`
    - The `Project:` header is already in the markdown from the parser — no additional work needed beyond unpacking
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 3.3 Write property test: markdown includes Project header iff project is non-NULL
    - **Property 4: Markdown formatting includes Project header iff project is non-NULL**
    - Generate random metadata dicts with/without project. Verify `Project:` header presence/absence in formatted output for both IDE and CLI formatters
    - **Validates: Requirements 1.4, 2.5**

  - [x] 3.4 Write unit tests for CLI parser project extraction
    - Test `parse_conversation()` with real workspace paths, home dirs, and empty conversation_ids
    - Test `format_as_markdown()` with and without project parameter
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Wire ingestion pipeline to read and store project tag
  - [x] 5.1 Modify `ingest_content()` in `src/ingest.py` to resolve project from header or parameter
    - Import `normalize_project_tag` from `src.project`
    - After `parse_metadata_header()`, resolve project: explicit `project` param overrides `meta.get("project")` header value
    - Apply `normalize_project_tag()` to the resolved value
    - Pass normalized project to both parent `create_memory()` and all chunk `create_memory()` calls
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 5.2 Write property test: ingestion project resolution — explicit overrides header
    - **Property 5: Ingestion project resolution — explicit overrides header**
    - Mock `create_memory` and `generate_embedding`. Generate random markdown with/without `Project:` header and with/without explicit `project` param. Verify correct precedence
    - **Validates: Requirements 4.1, 4.2, 4.3**

  - [x] 5.3 Write property test: parent-child project consistency
    - **Property 6: Parent-child project consistency in ingestion**
    - Mock `create_memory` to capture calls. Ingest content that produces multiple chunks. Verify all `project` args are identical
    - **Validates: Requirements 4.4, 7.1**

  - [x] 5.4 Write unit test for `parse_metadata_header()` reading `Project:` header
    - Test that `Project:` header is correctly parsed into `meta["project"]`
    - _Requirements: 4.1, 4.2_

- [x] 6. Normalize project in MCP server tools
  - [x] 6.1 Modify `memory_create` and `memory_search` in `src/mcp_server.py`
    - Import `normalize_project_tag` from `src.project`
    - In `memory_create`: apply `normalize_project_tag()` to the `project` parameter before passing to `create_memory()`
    - In `memory_search`: apply `normalize_project_tag()` to the `project` parameter before passing to `hybrid_search()` and `rerank()`
    - _Requirements: 8.8, 8.9_

  - [x] 6.2 Write unit tests for MCP project normalization
    - Mock `create_memory` and verify normalized project is passed
    - Mock `hybrid_search`/`rerank` and verify normalized project is passed to search
    - _Requirements: 8.8, 8.9_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Create backfill script `scripts/backfill_projects.py`
  - [x] 8.1 Implement Phase A: IDE chat backfill
    - Create `scripts/backfill_projects.py`
    - Query parent memories with `source_type = 'kiro_ide_chat'` and `parent_id IS NULL`
    - For each parent: extract chat filename from `source_url`, find `.chat` file on disk, re-parse with `extract_project_context()` → `normalize_project_tag()`
    - Fallback: if `.chat` file missing, parse `Project:` header from memory's `content` field
    - UPDATE parent's `project` column, then UPDATE all children (`parent_id = parent.id`)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.10, 6.11, 8.7_

  - [x] 8.2 Implement Phase B: CLI chat backfill
    - Query parent memories with `source_type = 'kiro_cli_chat'` and `parent_id IS NULL`
    - For each parent, read `metadata->>'source_id'` which contains the full `conversation_id` (workspace path)
    - Apply `normalize_project_tag()` to the workspace path directly — no SQLite read needed
    - Fall back to reading CLI SQLite DB `conversations_v2` table only for memories missing `source_id` in metadata
    - UPDATE parent and children project columns
    - _Requirements: 6.6, 6.7, 6.8, 6.10, 6.11, 8.7_

  - [x] 8.3 Implement Phase C: skip non-chat memories and add logging
    - Skip memories with `source_type` not in `('kiro_ide_chat', 'kiro_cli_chat')`
    - Add logging: counts per source_type (updated, excluded dot-prefix/home, left NULL, errors)
    - Commit per batch (500 memories) for resilience
    - Add `--dry-run` flag support
    - _Requirements: 6.9, 6.12, 6.13_

  - [x] 8.4 Write property test: backfill idempotency
    - **Property 7: Backfill idempotency**
    - Use test database. Insert sample memories, run backfill logic twice, verify project values are identical after both runs
    - **Validates: Requirements 6.12**

  - [x] 8.5 Write property test: non-chat memories remain NULL after backfill
    - **Property 8: Non-chat memories remain NULL after backfill**
    - Use test database. Insert memories with various `source_type` values. Run backfill. Verify non-chat memories still have `project = NULL`
    - **Validates: Requirements 6.9**

  - [x] 8.6 Write unit tests for backfill edge cases
    - Test: `.chat` file missing, content has `Project:` header → uses header
    - Test: neither source available → stays NULL
    - Test: parent-child consistency after backfill
    - _Requirements: 6.4, 6.5, 6.10, 7.2_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (unit tests only — property tests are required as the primary correctness mechanism)
- Each task references specific requirements for traceability
- All property tests use Hypothesis with `@settings(max_examples=100)`
- Pure function tests (normalization, formatting) don't need database fixtures
- Database tests use `test_db` and `clean_tables` fixtures from `tests/conftest.py`
- Embedding calls are mocked with the existing `mock_embedding` / `_deterministic_embedding` fixture
