# Requirements Document

## Introduction

Memory Migration enables importing knowledge from external AI tools (Claude, ChatGPT, Notion) into the Second Brain PostgreSQL + pgvector system. Years of conversations and notes are siloed in these platforms and inaccessible to MCP-powered agents. This feature provides import scripts that parse each platform's export format, apply the existing ingestion pipeline (chunk → embed → store with parent/child hierarchy), and guarantee idempotent imports via source_url deduplication.

## Glossary

- **Second_Brain**: The PostgreSQL + pgvector personal knowledge management system with 74K+ memories, exposed via MCP server.
- **Import_Script**: A Python script in `scripts/` that reads an external platform's export data and feeds it through the ingestion pipeline.
- **Claude_Export**: A JSON archive exported from claude.ai (Settings → Export Data), containing conversation history with messages, timestamps, and metadata.
- **ChatGPT_Export**: A JSON archive exported from chat.openai.com (Settings → Data Controls → Export Data), containing `conversations.json` with conversation history, messages, and metadata.
- **Notion_Export**: A collection of markdown and/or CSV files exported from Notion's built-in export feature, representing pages, databases, and nested content.
- **Ingestion_Pipeline**: The existing `ingest_content()` function in `src/ingest.py` that parses metadata headers, chunks by section, generates embeddings, and stores parent + child records in PostgreSQL.
- **Deduplication**: The process of checking `source_url` uniqueness via `get_processed_source_urls()` in `src/db.py` to prevent duplicate records when an import is run multiple times.
- **Parent_Record**: A memory record with no embedding that serves as the grouping record for a conversation or page, storing full content.
- **Child_Chunk**: A memory record with an embedding and a `parent_id` reference, representing a section of the parent content used for vector search.
- **Source_Type**: A string field on memory records identifying the origin platform (e.g., `claude_chat`, `chatgpt_chat`, `notion_page`).
- **Content_Filter**: Size and quality thresholds (minimum character count, minimum user messages, minimum paragraph word count) that exclude trivial or low-value content from ingestion.

## Requirements

### Requirement 1: Claude Conversation Import

**User Story:** As a Second Brain user, I want to import my Claude conversation history, so that years of reasoning and decisions from Claude sessions become searchable and available to MCP agents.

#### Acceptance Criteria

1. WHEN a valid Claude export JSON file path is provided, THE Import_Script SHALL parse all conversations from the export and produce markdown content with metadata headers for each conversation.
2. WHEN a Claude conversation contains fewer than 2 human messages or fewer than 200 total characters of content, THE Import_Script SHALL skip that conversation.
3. WHEN a Claude conversation contains assistant messages, THE Import_Script SHALL strip tool-use blocks, system prompts, and platform boilerplate before formatting.
4. THE Import_Script SHALL assign `source_type` of `claude_chat` and a `source_url` derived from the conversation UUID for each imported conversation.
5. WHEN a conversation's `source_url` already exists in the database for `source_type` `claude_chat`, THE Import_Script SHALL skip that conversation without creating duplicate records.
6. WHEN a Claude conversation exceeds the chunking threshold, THE Import_Script SHALL create a Parent_Record with full content and Child_Chunk records with embeddings, following the existing `ingest_content()` pipeline.
7. THE Import_Script SHALL extract and store conversation metadata including conversation title, creation date, and model used (when available) in the memory metadata field.
8. THE Import_Script SHALL report a summary of processed, skipped, and failed conversation counts upon completion.
9. THE Import_Script SHALL reside at `scripts/migrate_claude.py` and accept the export file path as a command-line argument.

### Requirement 2: ChatGPT Conversation Import

**User Story:** As a Second Brain user, I want to import my ChatGPT conversation history, so that knowledge from OpenAI chat sessions becomes part of my unified knowledge base.

#### Acceptance Criteria

1. WHEN a valid ChatGPT export directory or `conversations.json` file path is provided, THE Import_Script SHALL parse all conversations and produce markdown content with metadata headers for each conversation.
2. WHEN a ChatGPT conversation contains fewer than 2 user messages or fewer than 200 total characters of content, THE Import_Script SHALL skip that conversation.
3. WHEN a ChatGPT conversation contains assistant messages, THE Import_Script SHALL strip system messages, tool calls, tool results, and platform-specific formatting before producing markdown.
4. THE Import_Script SHALL assign `source_type` of `chatgpt_chat` and a `source_url` derived from the conversation ID for each imported conversation.
5. WHEN a conversation's `source_url` already exists in the database for `source_type` `chatgpt_chat`, THE Import_Script SHALL skip that conversation without creating duplicate records.
6. WHEN a ChatGPT conversation exceeds the chunking threshold, THE Import_Script SHALL create a Parent_Record with full content and Child_Chunk records with embeddings, following the existing `ingest_content()` pipeline.
7. THE Import_Script SHALL extract and store conversation metadata including conversation title, creation date, and model slug (when available) in the memory metadata field.
8. THE Import_Script SHALL report a summary of processed, skipped, and failed conversation counts upon completion.
9. THE Import_Script SHALL reside at `scripts/migrate_chatgpt.py` and accept the export path as a command-line argument.

### Requirement 3: Notion Page Import

**User Story:** As a Second Brain user, I want to import my Notion pages and databases, so that notes, documents, and structured data from Notion become searchable in the Second Brain.

#### Acceptance Criteria

1. WHEN a valid Notion export directory path is provided, THE Import_Script SHALL recursively discover all markdown files and CSV files within the export.
2. WHEN a Notion markdown file is discovered, THE Import_Script SHALL parse the file content, extract the page title from the filename or first heading, and produce content with a metadata header.
3. WHEN a Notion CSV file is discovered, THE Import_Script SHALL parse each row as a separate entry, using column headers as field labels and the database name as context.
4. WHEN a Notion page contains fewer than 200 characters of meaningful content after stripping Notion-specific formatting, THE Import_Script SHALL skip that page.
5. THE Import_Script SHALL strip Notion-specific artifacts including inline database references, block IDs appended to filenames, and Notion URL fragments.
6. THE Import_Script SHALL assign `source_type` of `notion_page` and a `source_url` derived from the file path (with Notion block ID when present) for each imported page.
7. WHEN a page's `source_url` already exists in the database for `source_type` `notion_page`, THE Import_Script SHALL skip that page without creating duplicate records.
8. WHEN a Notion page exceeds the chunking threshold, THE Import_Script SHALL create a Parent_Record with full content and Child_Chunk records with embeddings, following the existing `ingest_content()` pipeline.
9. THE Import_Script SHALL preserve Notion's hierarchical page structure by storing the parent page path in the memory metadata field.
10. THE Import_Script SHALL report a summary of processed, skipped, and failed page counts upon completion.
11. THE Import_Script SHALL reside at `scripts/migrate_notion.py` and accept the export directory path as a command-line argument.

### Requirement 4: Shared Import Infrastructure

**User Story:** As a developer maintaining the Second Brain, I want the import scripts to follow consistent patterns, so that the codebase remains maintainable and new import sources can be added with minimal effort.

#### Acceptance Criteria

1. THE Import_Script for each source SHALL use `ingest_content()` from `src/ingest.py` for all database writes, ensuring content passes through the full pipeline (chunking, embedding, classification, depth scoring, relationship discovery).
2. THE Import_Script for each source SHALL use `get_processed_source_urls(source_type)` from `src/db.py` to load already-imported URLs before processing, performing deduplication in a single batch query rather than per-conversation lookups.
3. THE Import_Script for each source SHALL format content as markdown with a metadata header consistent with the existing format used by `src/parsers/cli_chat.py` and `src/parsers/ide_chat.py` (title, Source-Type, Source-ID, Date, followed by `---` separator).
4. THE Import_Script for each source SHALL accept `--dry-run` as a command-line flag that prints statistics without writing to the database.
5. THE Import_Script for each source SHALL accept `--limit N` as a command-line flag that caps the number of conversations or pages processed.
6. THE Import_Script for each source SHALL log progress every 100 items processed, including running counts of processed, skipped, and failed items.
7. THE Import_Script for each source SHALL use the existing connection pooling via `get_connection()` context manager from `src/db.py`.
8. IF an individual conversation or page fails during import, THEN THE Import_Script SHALL log the error with the source identifier and continue processing remaining items.

### Requirement 5: Content Quality Filtering

**User Story:** As a Second Brain user, I want imported content to meet the same quality standards as existing ingested content, so that trivial or low-value conversations do not pollute search results.

#### Acceptance Criteria

1. THE Import_Script for Claude and ChatGPT sources SHALL apply a minimum user message count filter of 2 messages, consistent with the existing `MIN_USER_MESSAGES` threshold.
2. THE Import_Script for Claude and ChatGPT sources SHALL apply a minimum total content character filter of 200 characters, consistent with the existing `MIN_CONTENT_CHARS` threshold.
3. THE Import_Script for Claude and ChatGPT sources SHALL apply a substantive content filter requiring at least one assistant paragraph with 50 or more words, consistent with the existing `MIN_PARAGRAPH_WORDS` threshold.
4. THE Import_Script for Notion sources SHALL apply a minimum content character filter of 200 characters after stripping Notion-specific formatting artifacts.
5. WHEN a conversation or page is skipped due to content filtering, THE Import_Script SHALL count the skip in the `skipped` category of the final statistics report.

### Requirement 6: Idempotent Import Execution

**User Story:** As a Second Brain user, I want to safely re-run any import script without creating duplicate memories, so that I can resume interrupted imports or re-import after export updates.

#### Acceptance Criteria

1. WHEN an Import_Script is executed against an export that has been partially imported, THE Import_Script SHALL process only the conversations or pages not yet present in the database.
2. THE Import_Script SHALL determine presence by checking `source_url` against the set returned by `get_processed_source_urls(source_type)` before processing each item.
3. WHEN an Import_Script is executed against an export that has been fully imported, THE Import_Script SHALL report all items as skipped and create zero new records.
4. THE Import_Script SHALL load the full set of already-processed source URLs once at startup rather than querying per item, consistent with the `ingest_batch()` pattern.
