# Tasks

## Task 1: Create shared migration utilities ✅

**Requirements:** R4 (Shared Import Infrastructure), R5 (Content Quality Filtering), R6 (Idempotent Import Execution)

**File:** `scripts/migration_utils.py`

- `MIN_CONTENT_CHARS`, `MIN_USER_MESSAGES`, `MIN_PARAGRAPH_WORDS` constants (matching `src/parsers/cli_chat.py`)
- `MigrationError` exception class for validation failures (testable alternative to `sys.exit()`)
- `format_markdown_header()` — builds metadata header + `---` separator
- `format_chat_as_markdown()` — header + message formatting
- `passes_chat_filter()` — min user messages, min total chars, min paragraph words
- `passes_page_filter()` — min body length for non-chat content
- `run_migration(name, export_path, parse_fn, source_type, expected_file=None, dry_run=False, limit=None)` — shared entrypoint: validates path, raises `MigrationError` on failure, checks `is_reachable()`, loads dedup set, iterates `parse_fn` yielding `(source_url, markdown)` tuples, calls `ingest_content()`, logs progress every 100 items, prints summary

---

## Task 2: Implement Claude import script ✅

**Requirements:** R1 (Claude Import), R5 (Content Quality Filtering), R6 (Idempotent Import Execution)

**File:** `scripts/migrate_claude.py`

- `_extract_text(content_blocks)` — extracts plain text from Claude's content block array, skipping tool-use blocks
- `parse_claude_export(export_path)` generator yielding `(source_url, markdown)` — loads `conversations.json`, filters to human/assistant messages, encodes metadata (model, message count, timestamps) in markdown header via `extra_fields`
- `main()` with argparse: positional `export_path`, `--dry-run`, `--limit N`; catches `MigrationError`

---

## Task 3: Implement ChatGPT import script ✅

**Requirements:** R2 (ChatGPT Import), R5 (Content Quality Filtering), R6 (Idempotent Import Execution)

**File:** `scripts/migrate_chatgpt.py`

- `linearize_mapping(mapping)` — walks mapping tree root→leaf following first-child pointers
- `parse_chatgpt_export(export_path)` generator yielding `(source_url, markdown)` — loads `conversations.json`, linearizes mapping, filters to user/assistant, encodes metadata (model_slug, message count, timestamps) in markdown header via `extra_fields`
- `main()` with argparse: positional `export_path`, `--dry-run`, `--limit N`; catches `MigrationError`

---

## Task 4: Implement Notion import script ✅

**Requirements:** R3 (Notion Import), R5 (Content Quality Filtering), R6 (Idempotent Import Execution)

**File:** `scripts/migrate_notion.py`

- `strip_notion_id(filename)` — removes 32-char hex hash from filenames
- `strip_notion_artifacts(content)` — removes inline database references and Notion URL fragments containing block IDs
- `find_title_column(headers)` — "Name"/"Title" (case-insensitive) or first column
- `_path_slug(rel_path)` — full-path slug for collision avoidance across directories
- `parse_notion_export(export_path)` generator yielding `(source_url, markdown)` — recursively finds `.md`/`.csv` files, strips artifacts, encodes metadata (parent page path) in markdown header via `extra_fields`
- `main()` with argparse: positional `export_path`, `--dry-run`, `--limit N`; catches `MigrationError`

---

## Task 5: Unit, property, and integration tests ✅

**Requirements:** R4 (Shared Import Infrastructure), R5 (Content Quality Filtering), R6 (Idempotent Import Execution)

**File:** `tests/test_migration.py`

102 tests total:

- **migration_utils** (40 tests): header formatting, round-trip through `parse_metadata_header()`, chat filter (3 rejection paths + acceptance + monotonicity property), page filter (boundary), `run_migration` (path validation via `MigrationError`, dry-run counting, limit enforcement), constants consistency with `cli_chat.py`
- **migrate_claude** (12 tests): `_extract_text` (5 cases including tool-use stripping), `parse_claude_export` (7 cases: yield, filter, strip, model in header, source_url format, round-trip, message-count in header)
- **migrate_chatgpt** (12 tests): `linearize_mapping` (5 cases: linear, empty, None, branching first-child, non-string parts), `parse_chatgpt_export` (7 cases: yield, system/tool skip, filter, model_slug in header, epoch→ISO in header, round-trip, message-count in header)
- **migrate_notion** (35 tests): `strip_notion_id` (5 cases + idempotency property), `_path_slug` (5 cases: simple, nested, hash strip, collision avoidance, case insensitive), `strip_notion_artifacts` (4 cases including preserving non-block-ID URLs), `find_title_column` (5 cases), MD parsing (7 cases: yield, filter, artifact strip, parent path, parent path in header, recursive, collision avoidance), CSV parsing (5 cases: yield, filter, row index, title heuristic, fallback)
- **Integration round-trip** (3 tests): one per source — parse → `ingest_content()` → verify parent/chunk records in PostgreSQL → re-run → verify zero new records (dedup)

**Pre-merge manual step:** Run `--dry-run` against real Claude and ChatGPT exports to verify format assumptions hold.
