# Design Document

## Overview

Three import scripts convert exported data from Claude, ChatGPT, and Notion into the markdown-with-metadata-header format that `ingest_content()` expects, then delegate to the existing pipeline for chunking, embedding, and storage. The scripts share a common pattern: load export → iterate items → filter → format as markdown → call `ingest_content()`. A shared utility module (`scripts/migration_utils.py`) provides the common formatting, filtering, and CLI logic.

## Architecture

```
Export Files (JSON/MD/CSV)
    │
    ▼
┌──────────────────────────┐
│  migrate_claude.py       │
│  migrate_chatgpt.py      │──── Each script: parse source format → filter → format markdown
│  migrate_notion.py       │
└──────────┬───────────────┘
           │  yields (source_url, markdown)
           ▼
┌──────────────────────────┐
│  migration_utils.py      │──── Shared: format_markdown_header(), passes_filter(), run_migration()
└──────────┬───────────────┘
           │  calls
           ▼
┌──────────────────────────┐
│  src/ingest.py           │──── ingest_content() → parse_metadata_header → chunk_by_section → embed → store
│  src/db.py               │──── get_processed_source_urls() for dedup, create_memory() called by pipeline
└──────────────────────────┘
```

## Design Decisions

### 1. Format to markdown, delegate to `ingest_content()` only

The scripts do NOT call `create_memory()` directly. They format content into the markdown-with-metadata-header format and call `ingest_content()`, which handles chunking, embedding, classification, depth scoring, and relationship discovery. This ensures imported memories are structurally identical to existing ingested content.

**Rationale:** Calling `create_memory()` directly would bypass chunking, embedding, classification, depth scoring, and relationship discovery. The ingestion pipeline already handles the full parent/chunk hierarchy.

### 2. Metadata encoded in markdown header via `extra_fields`

Source-specific metadata (model, message count, timestamps, parent page path) is encoded into the markdown header as extra fields (e.g., `Model: gpt-4`, `Message-Count: 12`). These survive through `parse_metadata_header()` in `ingest_content()` and are stored in the memory's `metadata` JSONB field. The parsers yield only `(source_url, markdown)` — no separate metadata dict.

**Rationale:** The ingestion pipeline already extracts metadata from the markdown header. Encoding metadata there means it flows through the existing pipeline without any changes to `ingest_content()`. A separate metadata dict would be dead code since `run_migration()` only passes `source_url` and `markdown` to `ingest_content()`.

### 3. Shared utility module for common patterns

All three scripts share: metadata header formatting, content filtering (min chars, min messages), CLI argument parsing (including `--dry-run` and `--limit`), progress logging, summary printing, and the dedup-then-ingest loop. These live in `scripts/migration_utils.py`.

**Rationale:** The three scripts differ only in how they parse their source format. Everything else is identical.

### 4. `MigrationError` for validation failures

`run_migration()` raises `MigrationError` (not `sys.exit()`) for validation failures: missing export path, missing expected file, DB unreachable. Each script's `main()` catches `MigrationError` and exits with a clear error message.

**Rationale:** Raising exceptions instead of calling `sys.exit()` makes `run_migration()` testable — tests can assert on the exception without process termination.

### 5. Source URL scheme for deduplication

Each source type uses a URI scheme: `claude://{uuid}`, `chatgpt://{id}`, `notion://{slug}`. These are stored in `memories.source_url` and checked via `get_processed_source_urls()` before import.

**Rationale:** The existing dedup mechanism checks `source_url` uniqueness per `source_type`. Using a URI scheme makes source URLs globally unique and traceable.

### 6. Content filtering matches existing parsers

The same thresholds used in `cli_chat.py` and `ide_chat.py` apply: `MIN_CONTENT_CHARS = 200`, `MIN_USER_MESSAGES = 2` (for chat sources), `MIN_PARAGRAPH_WORDS = 50`. Notion pages only check `MIN_CONTENT_CHARS` since they aren't conversations.

### 7. Message stripping per source

- **Claude:** Strip messages with `sender` not in ("human", "assistant"). Strip tool-use content blocks (content items where `type` is not "text") via `_extract_text()`.
- **ChatGPT:** Skip messages with `author.role` in ("system", "tool"). Extract text from `content.parts`, skipping non-string parts (images, code execution results).
- **Notion:** Minimal stripping — remove block ID hashes from filenames via `strip_notion_id()`, strip inline database references and Notion URL fragments containing 32-char hex block IDs via `strip_notion_artifacts()`. File content is otherwise used as-is.

### 8. ChatGPT message tree traversal

ChatGPT exports use a `mapping` dict where each node has a `parent` pointer, forming a tree. `linearize_mapping()` reconstructs linear order by walking from root to leaf following `children` pointers, taking the first child at each branch point.

### 9. Export format expectations

- **Claude:** Expects the unzipped export directory containing `conversations.json`. The script validates this file exists.
- **ChatGPT:** Expects the unzipped export directory containing `conversations.json`. Same validation.
- **Notion:** Expects the export directory containing `.md` and/or `.csv` files (possibly nested). No single expected file — `expected_file=None`.

### 10. Notion `_path_slug()` for collision avoidance

Notion source URLs use `_path_slug(rel_path)` which incorporates the full relative path, not just the filename. This prevents collisions when pages with the same name exist in different directories (e.g., `ProjectA/Notes.md` and `ProjectB/Notes.md` get distinct source URLs).

### 11. Notion CSV title heuristic

`find_title_column(headers)` returns a column named "Name" or "Title" (case-insensitive) if present, otherwise falls back to the first column. Remaining columns are concatenated as the content body with column headers as bold labels.

## Component Details

### `scripts/migration_utils.py`

```python
MIN_CONTENT_CHARS = 200
MIN_USER_MESSAGES = 2
MIN_PARAGRAPH_WORDS = 50

class MigrationError(Exception): ...

def format_markdown_header(title, source_type, source_url, date_str, extra_fields=None) -> str:
def format_chat_as_markdown(title, messages, source_type, source_url, date_str, extra_fields=None) -> str:
def passes_chat_filter(messages) -> bool:
def passes_page_filter(body) -> bool:
def run_migration(name, export_path, parse_fn, source_type, expected_file=None, dry_run=False, limit=None):
    """Raises MigrationError on validation failure. Logs progress every 100 items."""
```

### `scripts/migrate_claude.py`

```python
def _extract_text(content_blocks) -> str:
    """Extract plain text from Claude's content block array, skipping tool-use blocks."""

def parse_claude_export(export_path) -> Iterator[tuple[str, str]]:
    """Yield (source_url, markdown) for each conversation passing filters.
    Metadata encoded in markdown header via extra_fields."""
```

### `scripts/migrate_chatgpt.py`

```python
def linearize_mapping(mapping) -> list[dict]:
    """Walk mapping tree root→leaf following first-child pointers."""

def parse_chatgpt_export(export_path) -> Iterator[tuple[str, str]]:
    """Yield (source_url, markdown) for each conversation passing filters.
    Metadata encoded in markdown header via extra_fields."""
```

### `scripts/migrate_notion.py`

```python
def strip_notion_id(filename) -> str:
def strip_notion_artifacts(content) -> str:
def find_title_column(headers) -> str:
def _path_slug(rel_path) -> str:
    """Full-path slug for collision avoidance across directories."""

def parse_notion_export(export_path) -> Iterator[tuple[str, str]]:
    """Yield (source_url, markdown) for each page/row passing filters.
    Metadata encoded in markdown header via extra_fields."""
```

### Data Flow

```
Claude JSON ──→ parse_claude_export() ──┐
                                        │  yields (source_url, markdown)
ChatGPT JSON ─→ parse_chatgpt_export() ─┤──→ run_migration() ──→ ingest_content() ──→ PostgreSQL
                                        │       │                    │
Notion MD/CSV → parse_notion_export() ──┘       │                    ├─ parent record (no embedding)
                                                │                    ├─ chunk records (with embeddings)
                                                │                    └─ relationships (semantic + temporal)
                                                │
                                                ├─ get_processed_source_urls() for dedup check
                                                ├─ --limit N caps items processed
                                                └─ progress logged every 100 items
```

### Metadata in Markdown Header

Metadata is encoded as extra header fields that survive through `parse_metadata_header()`:

| Header Field | Claude | ChatGPT | Notion |
|---|---|---|---|
| `Source-Type` | claude_chat | chatgpt_chat | notion_page |
| `Source-ID` | claude://{uuid} | chatgpt://{id} | notion://{slug} |
| `Date` | created_at date | create_time date | file mtime date |
| `Model` | model (when available) | model_slug (when available) | — |
| `Message-Count` | count of messages | count of messages | — |
| `Original-Created-At` | created_at ISO | create_time ISO | — |
| `Original-Updated-At` | updated_at ISO | update_time ISO | — |
| `Parent-Page-Path` | — | — | relative parent dir path |
