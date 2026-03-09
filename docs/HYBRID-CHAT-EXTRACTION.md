# Hybrid Chat Extraction

> Referenced from `AGENTS.md` → Chat Ingestion section.
> This document details the two-phase approach to extracting value from Kiro CLI and IDE chat histories.

## Overview

Chat ingestion is split into two phases: a deterministic script for mechanical cleanup (runs unattended via launchd) and an LLM-powered step for intelligent processing (runs via MCP server or agent session). A staging directory decouples the two phases.

```
Phase 1: Script (launchd, 2:30 AM)       Phase 2: Agent / MCP server
├── Read from 3 chat sources              ├── Read from staging queue
├── Structural stripping                  ├── Chunk by topic (LLM)
├── Size filtering                        ├── Extract metadata (LLM)
├── Content filtering                     ├── Generate embeddings (Bedrock)
└── Write cleaned chats to staging/       └── Store in PostgreSQL
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

launchd job at 2:30 AM daily. Plist at `~/Library/LaunchAgents/com.second-brain.chat-extraction.plist`.

Runs after the 2:00 AM backup, before the 3:00 AM Crawlee ingestion.

## Phase 2: Intelligent Processing

LLM-powered. Runs via MCP server tool call or agent session. Reads from the staging directory.

### When It Runs

Three modes:
- **On-demand**: You ask the agent "process the chat queue" during a session
- **Scheduled**: MCP server exposes a `process_staging_queue` tool, called by a scheduled job
- **Batch (backfill)**: Run once against the full staging directory after initial Phase 1 backfill

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
3. Process staging in batches (e.g., 100 chats per agent session) to manage cost
4. Monitor embedding costs — at ~5-10 chunks per chat, expect 20,000–50,000 embeddings
5. After backfill completes, switch to daily incremental mode

## Cost Considerations

| Phase | What | Cost |
|---|---|---|
| Phase 1 (script) | File I/O, regex, string operations | Free (runs locally) |
| Phase 2 chunking | LLM reads ~5-50KB per chat, outputs chunk boundaries | Reasoning tokens |
| Phase 2 metadata | LLM extracts structured data per chunk | Reasoning tokens |
| Phase 2 embedding | Bedrock Titan/Cohere per chunk | Embedding API calls |

Phase 1 is free. Phase 2 cost scales with the number of chats that survive filtering and the number of chunks produced. The staging directory lets you control the pace — process 50 chats today, 50 tomorrow, rather than all at once.
