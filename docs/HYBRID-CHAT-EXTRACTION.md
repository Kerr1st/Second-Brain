# Hybrid Chat Extraction

> Referenced from `AGENTS.md` → Chat Ingestion section.
> This document details the two-phase approach to extracting value from Kiro CLI and IDE chat histories.

## Overview

Chat ingestion is split into two phases: a deterministic script for mechanical cleanup (runs unattended via launchd) and Kiro CLI in headless mode for intelligent processing. A staging directory decouples the two phases.

```
Phase 1: Script (launchd, 2:30 AM)       Phase 2: Kiro headless (launchd, 3:00 AM)
├── Read from 3 chat sources              ├── One chat per Kiro session
├── Structural stripping                  ├── kiro-cli --no-interactive --model claude-opus-4.8
├── Size filtering                        ├── Chunk by topic (LLM)
├── Content filtering                     ├── Extract metadata (LLM)
└── Write cleaned chats to staging/       ├── Generate embeddings (Bedrock)
                                          └── Store in PostgreSQL
```

## Phase 1: Deterministic Script

No LLM calls. Runs unattended on a schedule. Produces cleaned markdown files.

### Input Sources

| Source | Location | Read method |
|---|---|---|
| Kiro CLI | `~/Library/Application Support/kiro-cli/data.sqlite3` | SQLite query → `conversations_v2` table → JSON `value` → `transcript` array |
| Kiro IDE (current) | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/.../*.chat` | Read JSON → `chat` array |
| Kiro IDE (legacy) | `~/Library/Application Support/Kiro/User/globalStorage/amazonwebservices.aieditoragent/.../*.chat` | Same as current |

### Step 1: Structural Stripping

**IDE chats — strip by role and content pattern:**

```
Raw IDE .chat file (~80-150KB)
│
├── [0] human: "# System Prompt\n# Identity\nYou are Kiro..."  (~32KB)  → DROP
├── [1] bot: "I will follow these instructions."                        → DROP
├── [2] bot: ""                                                         → DROP
├── [3] tool: "<file tree>...</file tree>"                   (~50KB)    → DROP (extract project path → metadata)
├── [4] bot: ""                                                         → DROP
├── [5] tool: "<file name=\"...\">...</file>"                           → DROP
├── [6] human: "Implement the task from..."                             → KEEP
├── [7] bot: "I'll implement task 13..."                                → KEEP
├── ...remaining human/bot exchanges...                                 → KEEP
│
Result: ~5-50KB of actual conversation
```

Stripping rules:
- Drop `human` message [0] if content starts with `# System Prompt` or `# Identity`
- Drop all `tool` role messages
- Before dropping `tool` messages: extract project path from the chat's `context` field → save as metadata
- Drop `bot` messages where content is empty or matches `"I will follow these instructions"`

**CLI chats — strip by content pattern:**

```
Raw CLI transcript (array of strings)
│
├── [0] "> User's question..."                                         → KEEP
├── [1] "  [Tool uses: thinking]"                                      → DROP
├── [2] "  [Tool uses: fs_read]"                                       → DROP
├── [3] "Assistant's reasoning and response..."                        → KEEP
├── [4] "> User's follow-up..."                                        → KEEP
├── ...
│
Result: alternating user/assistant messages
```

Stripping rules:
- Drop items matching `^\s*\[Tool uses:.*\]$`
- Drop items that are tool output (heuristic: starts with `# Total entries:`, or is a large block of file content / directory listing)
- User messages start with `> ` prefix

### Step 2: Size-Based Filtering

After stripping, evaluate the remaining content:

| Condition | Action |
|---|---|
| Total remaining content < 200 characters | **Skip** — trivial interaction |
| Only 1 user message | **Skip** — single-turn, no context |
| Passes both checks | → proceed to Step 3 |

### Step 3: Content-Based Filtering

Check whether the conversation contains reasoning, not just mechanical commands:

| Condition | Action |
|---|---|
| No assistant paragraph longer than ~50 words | **Skip** — purely mechanical ("Done", "File created") |
| Passes check | → write to staging |

### Output: Staging Directory

Cleaned chats written to `~/second-brain/staging/chats/` as markdown files:

```
~/second-brain/staging/chats/
├── cli_803bc2b1.md
├── ide_06641446022e012431fc2d763ae2f637.md
├── ide_legacy_54f2c404927490623e48e053efe7bcff.md
└── ...
```

File format:

```markdown
# Chat: {conversation_id or filename}

Source-Type: kiro_cli_chat | kiro_ide_chat
Source-ID: {conversation_id or filename}
Date: {YYYY-MM-DD from timestamp}
Project: {extracted from IDE context, or CLI working directory if available}

---

**User:** {message}

**Assistant:** {message}

**User:** {message}

**Assistant:** {message}
```

This format is consistent with Crawlee's markdown-with-metadata-header pattern, making Phase 2 processing uniform across all source types.

### Deduplication

Before writing to staging, check if the file already exists in staging or if the `source_url` (conversation ID / filename) already exists in PostgreSQL. Skip if already processed.

### Schedule

launchd job at 2:30 AM daily. Plist at `scheduling/com.second-brain.chat-extract.plist`.

Runs after the 2:00 AM backup, before the 3:00 AM Crawlee ingestion.

## Phase 2: Intelligent Processing

LLM-powered. Runs via Kiro CLI in headless mode (`--no-interactive`). Processes one chat per Kiro session to avoid context window pressure.

### Execution Method

Kiro CLI headless with Claude Opus 4.6 (1M context):

```bash
kiro-cli chat --no-interactive --trust-all-tools --model claude-opus-4.8 \
  "Process the staged chat at ~/second-brain/staging/chats/{filename}. Read the file, chunk by topic, extract metadata, generate embeddings, and store in the Second Brain PostgreSQL database."
```

**One chat per Kiro session.** A cleaned chat can be 5–50KB. Claude Opus 4.8 has a 1,000,000 token window (~4MB), so context overflow is not a concern for any single chat. We still process one per session because:
- Each chat gets the LLM's full attention
- No context window overflow risk
- If one chat fails, the others aren't affected
- The wrapper script retries failures independently

### Wrapper Script

The launchd job at 3:00 AM runs a wrapper that loops through staging:

```bash
#!/bin/bash
# scripts/jobs/ingest_staged.sh — Phase 2 wrapper
cd ~/second-brain

LOGFILE="logs/ingest-$(date +%Y%m%d).log"
FAILED_DIR="staging/failed"
PROCESSED=0
FAILED=0
SKIPPED=0

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOGFILE"; }

# Pre-flight: check PostgreSQL is reachable
if ! pg_isready -h 127.0.0.1 -p 5432 -U memory_bank > /dev/null 2>&1; then
  log "ABORT: PostgreSQL is not reachable. Is the postgresql@17 service running?"
  exit 1
fi

for chat_file in staging/chats/*.md; do
  [ -f "$chat_file" ] || continue
  FILENAME=$(basename "$chat_file")
  log "Processing: $FILENAME"

  kiro-cli chat --no-interactive --trust-all-tools --model claude-opus-4.8 \
    "Process the staged chat at ~/second-brain/$chat_file. Read the file, chunk it by topic shifts, extract metadata (topics, decisions, action items, project, files discussed), generate embeddings via Bedrock, and store in the Second Brain PostgreSQL database. After successful storage, delete the staging file." \
    >> "$LOGFILE" 2>&1

  EXIT_CODE=$?
  if [ $EXIT_CODE -eq 0 ] && [ ! -f "$chat_file" ]; then
    log "OK: $FILENAME ingested and removed from staging"
    PROCESSED=$((PROCESSED + 1))
  else
    log "FAIL: $FILENAME (exit code $EXIT_CODE) — moved to $FAILED_DIR/"
    mv "$chat_file" "$FAILED_DIR/"
    FAILED=$((FAILED + 1))
  fi

  sleep 5
done

log "=== Run complete: processed=$PROCESSED failed=$FAILED ==="

# Write status file for monitoring
cat > logs/last_ingest_status.json << EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "processed": $PROCESSED,
  "failed": $FAILED,
  "pending": $(ls staging/chats/*.md 2>/dev/null | wc -l | tr -d ' '),
  "failed_files": $(ls staging/failed/*.md 2>/dev/null | wc -l | tr -d ' ')
}
EOF
```

Each iteration: Kiro starts → reads one chat → chunks → embeds → stores → exits. The staging file is deleted after successful ingestion, so the next run only processes new files.

### When It Runs

- **Daily (3:00 AM)**: launchd runs the wrapper script after Phase 1 (2:30 AM). Typically 5–20 new chats.
- **On-demand**: Run the wrapper script manually, or ask the agent in an interactive session to process a specific file.
- **Backfill**: Same wrapper script, just more files in staging. See Backfill Strategy below.

### Step 4: Chunk by Topic

LLM reads the cleaned chat and identifies topic boundaries. A single chat might produce 5–10 chunks. Each chunk is a coherent thread — a question explored, a decision made, a problem debugged.

The LLM is better at this than heuristics because it understands when a conversation shifts from "discussing database schema" to "talking about backup strategy" even without explicit markers.

### Step 5: Extract Metadata

For each chunk, the LLM extracts:

```json
{
  "topics": ["database schema", "pgvector"],
  "decisions": ["chose PostgreSQL over SQLite for hybrid search"],
  "action_items": [],
  "people": [],
  "project": "second-brain",
  "files_discussed": ["AGENTS.md", "schema.sql"]
}
```

Stored in the `metadata` JSONB column.

### Step 6: Embed + Store

- Generate embedding per chunk via Bedrock Titan/Cohere
- Store parent record: `type: source`, `source_type: kiro_cli_chat` or `kiro_ide_chat`, `source_url: {conversation_id}`, `content: full cleaned chat`
- Store child records: one per chunk, `parent_id` referencing the parent, each with its own embedding and metadata

### Backfill Strategy

1. Run Phase 1 script once against all ~12,500 IDE chats + ~96 CLI sessions
2. Estimated ~4,000–5,000 chats survive filtering → written to staging
3. Run the wrapper script — it processes one chat per Kiro session, sequentially
4. At ~1-2 minutes per chat (Kiro startup + LLM processing + embedding), expect ~4,000–10,000 minutes (~3–7 days running continuously)
5. To speed up: run multiple wrapper instances in parallel (e.g., split staging into 4 subdirectories, run 4 wrappers)
6. Monitor embedding costs — at ~5–10 chunks per chat, expect 20,000–50,000 embeddings
7. After backfill completes, switch to daily incremental mode

## Error Handling

### Directory Layout

```
~/second-brain/
├── staging/
│   ├── chats/          ← Phase 1 output, Phase 2 input
│   └── failed/         ← Chats that failed Phase 2 processing (for retry)
└── logs/
    ├── chat_extract-YYYYMMDD.log    ← Phase 1 daily log
    ├── ingest-YYYYMMDD.log          ← Phase 2 daily log
    └── last_ingest_status.json      ← Machine-readable status for monitoring
```

### Phase 1 Error Handling (chat_extract.py)

- If a single chat fails to parse (bad JSON, unexpected structure) → log the error, skip that chat, continue
- If the CLI SQLite is locked or unreadable → log error, skip CLI source, continue with IDE sources
- If staging directory is unwritable → abort with clear error
- End of run: log summary — "Processed X, skipped Y (trivial), failed Z (errors)"
- Write to `logs/chat_extract-YYYYMMDD.log`

### Phase 2 Error Handling (ingest_staged.sh)

- **Pre-flight check**: Before processing any files, verify PostgreSQL is reachable (native, `pg_isready -h 127.0.0.1`). If not, abort immediately — don't waste Kiro credits on sessions that can't write to the database.
- **Per-file isolation**: Each chat is a separate Kiro session. If one fails, the others still process.
- **Failure handling**: If Kiro exits non-zero or the staging file still exists after the session (meaning it wasn't successfully ingested and deleted), move the file to `staging/failed/` for later investigation.
- **Status file**: After each run, write `logs/last_ingest_status.json` with counts of processed, failed, and pending files. This is the single file to check for pipeline health.

### Retry Strategy

Files in `staging/failed/` can be retried by moving them back to `staging/chats/`:

```bash
mv ~/second-brain/staging/failed/*.md ~/second-brain/staging/chats/
```

The next scheduled run (or a manual run of the wrapper script) will pick them up. Investigate the log first to understand why they failed — if it's a transient error (API timeout, credit limit), retry will likely succeed. If it's a data issue (malformed chat), the file may need manual inspection.

### Monitoring

Check pipeline health at a glance:

```bash
cat ~/second-brain/logs/last_ingest_status.json
```

```json
{
  "timestamp": "2026-03-09T10:00:00Z",
  "processed": 12,
  "failed": 1,
  "pending": 0,
  "failed_files": 1
}
```

If `failed_files` is growing over time, something systemic is wrong (database down, auth expired, credit limit). If it's occasional, it's likely transient errors.

## Cost Considerations

| Phase | What | Cost |
|---|---|---|
| Phase 1 (script) | File I/O, regex, string operations | Free (runs locally) |
| Phase 2 chunking | Kiro headless + Opus 4.6-1m, ~5–50KB per chat | Kiro credits (reasoning tokens) |
| Phase 2 metadata | Opus 4.6-1m extracts structured data per chunk | Kiro credits (reasoning tokens) |
| Phase 2 embedding | Bedrock Titan/Cohere per chunk | Bedrock API calls |

Phase 1 is free. Phase 2 cost scales with the number of chats that survive filtering and the number of chunks produced. The staging directory lets you control the pace — process 50 chats today, 50 tomorrow, rather than all at once.
