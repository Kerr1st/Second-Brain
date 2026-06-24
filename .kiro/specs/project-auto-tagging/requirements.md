# Requirements Document

## Introduction

The Second Brain's `project` column, cross-project rerank penalty (-0.15), and project filter in `hybrid_search` are all implemented but inert. Nothing populates the `project` field. The IDE chat parser extracts a `project_hint` from workspace context but the value is discarded before reaching `create_memory`. The CLI chat parser performs no project extraction at all. All 74K existing memories have `project = NULL`, which means they are treated as universal knowledge and the encoding-specificity scoping never activates.

Current data breakdown:
- **0 memories** have `project` set
- **73,257 IDE chat memories** — `extract_project_context()` extracts `project_hint` from fileTree context, but it's never stored in the `project` column
- **752 CLI chat memories** — the `conversation_id` key in the SQLite DB IS the workspace directory path (e.g., `/Users/example/Projects/RetailStore`), providing a direct project signal
- **10 other memories** (youtube, manual) — no workspace context, stay NULL (universal knowledge)
- The `chat_extract.py` pipeline writes markdown to staging but doesn't pass project through to ingestion
- `ingest_content()` already accepts a `project` parameter but nobody passes it

This feature closes the loop: derive project tags at parse/ingest time, flow them through to storage, and backfill existing memories so the cross-project penalty and project filter do their jobs.

## Glossary

- **IDE_Chat_Parser**: The module `src/parsers/ide_chat.py` that reads Kiro IDE `.chat` JSON files, strips boilerplate, extracts metadata (including `project_hint` from workspace `fileTree` context), and returns cleaned markdown conversations.
- **CLI_Chat_Parser**: The module `src/parsers/cli_chat.py` that reads Kiro CLI conversations from the SQLite database, strips tool markers, and returns cleaned markdown conversations.
- **Chat_Extract_Script**: The script `scripts/chat_extract.py` that orchestrates Phase 1 extraction by calling both parsers and writing cleaned markdown to the staging directory.
- **Ingestion_Pipeline**: The module `src/ingest.py` that accepts markdown content with metadata headers, chunks it, generates embeddings, and stores memories via `create_memory`.
- **Project_Tag**: A text value identifying the originating project/workspace for a memory. Stored in the `project` column of the `memories` table. NULL means universal knowledge (no cross-project penalty applied).
- **Backfill_Script**: A one-time migration script that infers and sets Project_Tags on existing memories where source metadata contains sufficient signal.
- **Source_Metadata**: The `metadata` JSONB column and `source_url`/`source_type` fields on a memory record, which may contain file paths, workspace identifiers, or project hints from the original extraction.
- **MCP_Server**: The `src/mcp_server.py` module exposing Second Brain tools (`memory_create`, `memory_search`, etc.) to AI agents via the MCP protocol.

## Requirements

### Requirement 1: IDE Chat Parser Project Extraction

**User Story:** As a memory system operator, I want the IDE chat parser to extract and surface the project identifier from workspace context, so that memories ingested from IDE chats carry the correct project tag.

#### Acceptance Criteria

1. WHEN an IDE `.chat` file contains a `fileTree` context entry with `expandedPaths`, THE IDE_Chat_Parser SHALL extract the top-level directory name from the first expanded path as the Project_Tag.
2. WHEN an IDE `.chat` file contains no `fileTree` context or the `expandedPaths` list is empty, THE IDE_Chat_Parser SHALL set the Project_Tag to NULL.
3. THE IDE_Chat_Parser SHALL include the Project_Tag in the returned metadata dictionary under the key `project_hint`.
4. THE IDE_Chat_Parser SHALL include a `Project:` header line in the formatted markdown output WHEN the Project_Tag is not NULL.

### Requirement 2: CLI Chat Parser Project Extraction

**User Story:** As a memory system operator, I want the CLI chat parser to extract a project identifier from the conversation's workspace path, so that CLI-sourced memories carry project tags.

#### Acceptance Criteria

1. THE CLI_Chat_Parser SHALL extract the Project_Tag from the `conversation_id` field, which is the workspace directory path (e.g., `/Users/example/Projects/RetailStore`).
2. THE CLI_Chat_Parser SHALL use the leaf directory name of the `conversation_id` path as the Project_Tag (e.g., `retailstore` from `/Users/example/Projects/RetailStore`).
3. WHEN the `conversation_id` does not contain a valid directory path (e.g., is empty or a non-path identifier), THE CLI_Chat_Parser SHALL set the Project_Tag to NULL.
4. WHEN the `conversation_id` path (which is always an absolute path) has fewer than 3 path components (i.e., is a home directory like `/Users/example` or root `/`), THE CLI_Chat_Parser SHALL set the Project_Tag to NULL.
5. THE CLI_Chat_Parser SHALL include a `Project:` header line in the formatted markdown output WHEN the Project_Tag is not NULL.
6. THE CLI_Chat_Parser SHALL return the Project_Tag in the result tuple alongside the conversation_id and markdown.

### Requirement 3: Chat Extract Script Passes Project Through

**User Story:** As a memory system operator, I want the Phase 1 extraction script to preserve the project tag from parsers into the staged markdown files, so that downstream ingestion can read it.

#### Acceptance Criteria

1. WHEN the IDE_Chat_Parser returns a metadata dictionary containing a non-NULL `project_hint`, THE Chat_Extract_Script SHALL ensure the staged markdown file includes the `Project:` header line with that value.
2. WHEN the CLI_Chat_Parser returns a conversation with a non-NULL Project_Tag, THE Chat_Extract_Script SHALL ensure the staged markdown file includes the `Project:` header line with that value.
3. THE Chat_Extract_Script SHALL not modify or discard the `Project:` header produced by either parser.

### Requirement 4: Ingestion Pipeline Reads and Stores Project Tag

**User Story:** As a memory system operator, I want the ingestion pipeline to read the project tag from markdown metadata headers and pass it to `create_memory`, so that ingested memories are stored with the correct project scope.

#### Acceptance Criteria

1. WHEN a markdown content block contains a `Project:` metadata header, THE Ingestion_Pipeline SHALL parse the value and pass it as the `project` parameter to `create_memory` for both the parent record and all chunk records.
2. WHEN a markdown content block does not contain a `Project:` metadata header, THE Ingestion_Pipeline SHALL pass `project=None` to `create_memory`.
3. WHEN `ingest_content` is called with an explicit `project` parameter, THE Ingestion_Pipeline SHALL use the explicit parameter value, overriding any `Project:` header in the content.
4. FOR ALL memories ingested through the pipeline, THE Ingestion_Pipeline SHALL store the same Project_Tag on the parent memory and all of its chunk children.

### Requirement 5: MCP Server Project Passthrough (Existing — No New Work)

**User Story:** As an AI agent user, I want the `memory_create` MCP tool to continue accepting and storing the project parameter, so that interactively created memories are correctly scoped.

**Note:** This requirement documents existing behavior. `memory_create` already accepts and stores the `project` parameter. No implementation changes needed for passthrough. However, Requirement 8.9 adds normalization to this tool.

#### Acceptance Criteria

1. THE MCP_Server `memory_create` tool SHALL accept an optional `project` parameter of type string.
2. WHEN a `project` parameter is provided to `memory_create`, THE MCP_Server SHALL pass the value to `create_memory` as the `project` field.
3. WHEN no `project` parameter is provided to `memory_create`, THE MCP_Server SHALL pass `project=None` to `create_memory`.

### Requirement 6: Backfill Existing Memories

**User Story:** As a memory system operator, I want to backfill project tags on the 74K existing memories that have `project = NULL`, so that the cross-project penalty and project filter apply retroactively.

**Data reality:**
- Zero IDE chat memories have `project_hint` in their `metadata` JSONB. 34,145 have `metadata.project = ".kiro"` (not useful). Only 70 have a `Project:` header in content text.
- CLI chat `source_url` values are staging filenames (e.g., `cli_5951158f-1d5.md`), not workspace paths. The workspace path is the `conversation_id` in the CLI SQLite DB, but that mapping is not stored in the memory record.
- Non-chat memories (youtube, manual, article) have no workspace context.

#### Acceptance Criteria

**IDE Chat Backfill (73,257 memories):**

1. THE Backfill_Script SHALL re-parse the original IDE `.chat` files from disk using `extract_project_context()` to re-extract the `project_hint` for each chat.
2. THE Backfill_Script SHALL match re-parsed `.chat` files to existing memories via the `source_url` field (which contains the chat filename).
3. WHEN a re-parsed `.chat` file yields a non-NULL, non-excluded `project_hint`, THE Backfill_Script SHALL update the matching memory's `project` column to the normalized project value.
4. WHEN the original `.chat` file no longer exists on disk, THE Backfill_Script SHALL fall back to parsing the `Project:` header from the memory's `content` field (covers ~70 memories).
5. WHEN neither the `.chat` file nor a `Project:` content header yields a project, THE Backfill_Script SHALL leave the `project` column as NULL.

**CLI Chat Backfill (752 memories):**

6. THE Backfill_Script SHALL read the `metadata->>'source_id'` field from each CLI memory record, which contains the full `conversation_id` (workspace path), and apply `normalize_project_tag()` to derive the project name. THE Backfill_Script MAY fall back to reading the CLI SQLite database (`conversations_v2` table) for any memories missing the `source_id` metadata field.
7. THE Backfill_Script SHALL match CLI memories to conversations by reading the `metadata->>'source_id'` field from the memory record, which contains the full `conversation_id` (workspace path) written by the CLI parser's `Source-ID:` header.
8. WHEN a match is found and the workspace path yields a valid project name, THE Backfill_Script SHALL update the memory's `project` column.

**General:**

9. FOR memories with `source_type` NOT IN ('kiro_ide_chat', 'kiro_cli_chat') (e.g., youtube, manual, article), THE Backfill_Script SHALL leave the `project` column as NULL (universal knowledge).
10. WHEN a memory has a non-NULL `parent_id`, THE Backfill_Script SHALL assign the same Project_Tag as the parent memory (parent-first processing order).
11. THE Backfill_Script SHALL process parent memories before child memories to ensure parent-child consistency.
12. THE Backfill_Script SHALL operate idempotently: running the Backfill_Script multiple times SHALL produce the same result as running it once.
13. THE Backfill_Script SHALL log the count of memories updated per source_type, the count matched but excluded (dot-prefixed directories, home directories), and the count left as NULL.

### Requirement 7: Project Tag Consistency Across Parent-Child Memories

**User Story:** As a memory system operator, I want parent and child (chunk) memories to always share the same project tag, so that search results are not inconsistent.

#### Acceptance Criteria

1. THE Ingestion_Pipeline SHALL assign the same Project_Tag to a parent memory and all chunk memories derived from that parent.
2. IF a chunk memory is found with a different Project_Tag than its parent, THEN THE Backfill_Script SHALL correct the chunk's Project_Tag to match the parent.

### Requirement 8: Project Tag Validation

**User Story:** As a memory system operator, I want project tags to be normalized and validated, so that minor variations in workspace paths do not create duplicate project identifiers and non-project directories are excluded.

#### Acceptance Criteria

1. THE IDE_Chat_Parser SHALL normalize the extracted Project_Tag to lowercase, stripped of leading/trailing whitespace.
2. THE CLI_Chat_Parser SHALL normalize the extracted Project_Tag to lowercase, stripped of leading/trailing whitespace.
3. WHEN a Project_Tag contains path separators (`/` or `\`), THE parser extracting the tag SHALL use only the final path component as the Project_Tag.
4. WHEN a Project_Tag is an empty string after normalization, THE parser SHALL treat it as NULL.
5. WHEN a Project_Tag starts with a dot (e.g., `.kiro`, `.git`, `.vscode`), THE parser SHALL treat it as NULL (dot-prefixed directories are configuration, not projects).
6. WHEN the workspace path is an absolute path (starts with `/`) AND has fewer than 3 path components (i.e., is a home directory like `/Users/example` or root `/`), THE parser SHALL treat the Project_Tag as NULL. This rule does NOT apply to relative paths or bare directory names (e.g., `RetailStore` from IDE fileTree extraction), which are valid project tags.
7. THE Backfill_Script SHALL apply the same normalization and exclusion rules (AC 1-6) when deriving project tags from historical data.
8. WHEN the `memory_search` MCP tool receives a `project` parameter, THE MCP_Server SHALL normalize it using the same rules (lowercase, strip whitespace) before passing to `hybrid_search` and `rerank`.
9. WHEN the `memory_create` MCP tool receives a `project` parameter, THE MCP_Server SHALL normalize it using the same rules (lowercase, strip whitespace) before passing to `create_memory`.
