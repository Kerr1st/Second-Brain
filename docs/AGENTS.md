# Second Brain — Agent Memory System

## Purpose

This system is a personal knowledge management and ideation platform powered by AI. It gives AI agents (Kiro CLI, Claude Code) persistent memory across sessions, turning them from stateless tools into thinking partners that know how you work, what you're working on, and what you've learned.

The core problem it solves: AI agents forget everything between sessions. Session 100 is no better than session 1. This system makes the agent accumulate institutional knowledge over time — your decisions, ideas, research, syntheses, and open questions — so it can reason across your entire knowledge base and help you think.

## Workflow

1. You gather information (YouTube, articles, PDFs, courses, AI chats, notes)
2. The system ingests and indexes that information
3. You synthesize and share your thinking with the agent
4. The agent stores your ideas, connects them to existing knowledge, and tracks open threads
5. Over time, you ask the agent what to work on next
6. The agent reasons across everything — your ideas, research, priorities, and open questions — to brainstorm and recommend

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Session Bootstrap                             │
│  Kiro Skills & CLAUDE.md                                │
│  "How I think and work"                                 │
│  Static, human-curated rules and preferences            │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Unified Knowledge Store                       │
│  PostgreSQL + pgvector (local Docker)                   │
│  Exposed via MCP server                                 │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  memories    │  │  sources     │  │ relationships │  │
│  │  (your       │  │  (ingested   │  │ (connections  │  │
│  │  thinking)   │──│  content)    │──│  between      │  │
│  │              │  │              │  │  everything)  │  │
│  │  ideas       │  │  YouTube     │  │               │  │
│  │  syntheses   │  │  articles    │  │  supports     │  │
│  │  decisions   │  │  PDFs        │  │  contradicts  │  │
│  │  insights    │  │  courses     │  │  extends      │  │
│  │  questions   │  │  AI chats    │  │  inspires     │  │
│  │  priorities  │  │  notes       │  │  derived_from │  │
│  └─────────────┘  └──────────────┘  └───────────────┘  │
├─────────────────────────────────────────────────────────┤
│  Capture API (planned)                                  │
│  Lightweight HTTP endpoint for multi-channel capture:   │
│  Slack webhook → extract metadata → embed → store       │
│  Mobile shortcut → POST → store                         │
│  Browser extension → POST → store                       │
│  Email forward → parse → store                          │
├─────────────────────────────────────────────────────────┤
│  Content Acquisition: Crawlee                           │
│  ~/Work/Tools/Crawlee/                                  │
│  scraper.js → web pages → markdown files                │
│  youtube.js → transcripts → markdown files              │
│  Output: ~/Work/Tools/Crawlee/knowledge/sources/        │
├─────────────────────────────────────────────────────────┤
│  Ingestion Pipeline                                     │
│  Reads content and writes to PostgreSQL:                │
│  Crawlee markdown → chunk → embed → store               │
│  AI Chats → parse → chunk → embed → store (see below)  │
│  Notes → embed → store                                  │
├─────────────────────────────────────────────────────────┤
│  Execution Engine                                       │
│  Kiro CLI / Claude Code                                 │
│  Amazon Bedrock (Claude for reasoning,                  │
│  Titan/Cohere for embeddings)                           │
├─────────────────────────────────────────────────────────┤
│  Backup (dual-site, encrypted)                          │
│  rclone + GPG → Google Drive (primary)                  │
│                → AWS S3 (secondary)                     │
│  Encryption keys in Google Secret Mgr + AWS Secrets Mgr │
│  Daily backups at 2AM, weekly verification Sundays 3AM  │
└─────────────────────────────────────────────────────────┘
```

## Technology Choices

| Component | Technology | Rationale |
|---|---|---|
| Knowledge store | PostgreSQL + pgvector | SQL + semantic search + relationships in one open-source DB |
| Agent interface | MCP server | Standard protocol, works with Kiro, Claude Code, Cursor, etc. |
| LLM | Amazon Bedrock (Claude) | Reasoning, synthesis, brainstorming |
| Embeddings | Amazon Bedrock (Titan/Cohere) | Vector generation for semantic search |
| Container | Docker | Local PostgreSQL + pgvector, portable |
| Backup transport | rclone | Open-source, 70+ cloud storage backends |
| Backup encryption | GPG (AES-256) | Industry standard symmetric encryption |
| Backup primary | Google Drive | Off-AWS disaster recovery |
| Backup secondary | AWS S3 | Off-Google disaster recovery |
| Key storage | Google Secret Manager + AWS Secrets Manager | Dual-site key recovery |
| Scheduling | macOS launchd | Native, reliable, no dependencies |
| Content acquisition | Crawlee (`~/Work/Tools/Crawlee/`) | Custom Node.js tool for web scraping and YouTube transcript extraction; outputs clean markdown |

## Database Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,           -- idea, synthesis, research, insight, question, decision, priority, project, connection, source
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,                 -- agent-generated summary for long content
    embedding vector(1024),
    tags TEXT[] DEFAULT '{}',
    source_url TEXT,              -- origin: YouTube URL, article URL, etc.
    source_type TEXT,             -- youtube, article, pdf, course, kiro_cli_chat, kiro_ide_chat, notes, manual
    metadata JSONB DEFAULT '{}', -- flexible extracted metadata (people, topics, action_items, project, files)
    status TEXT DEFAULT 'active', -- active, explored, archived, superseded
    confidence FLOAT DEFAULT 1.0,
    parent_id UUID REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE memory_relationships (
    source_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    target_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,  -- supports, contradicts, extends, inspires, blocks, requires, derived_from
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (source_id, target_id, relation_type)
);

CREATE INDEX ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON memories (type, status);
CREATE INDEX ON memories USING gin (tags);
CREATE INDEX ON memories USING gin (metadata);
CREATE INDEX ON memories (created_at DESC);
CREATE INDEX ON memories (source_type);
```

## Memory Types

| Type | Purpose | Example |
|---|---|---|
| `research` | Raw information gathered | Notes from a YouTube video on agent memory patterns |
| `synthesis` | Your analysis of information | "Combining pgvector with MCP replaces flat file memory banks" |
| `idea` | Hypotheses, project concepts | "Build an MCP server that auto-ingests YouTube transcripts" |
| `connection` | Links between concepts | "Hmem's depth levels mirror how human memory works" |
| `priority` | What to work on and why | "Memory system must be running before AgentCore setup" |
| `question` | Open threads to explore | "Can pgvector handle graph-style queries at personal scale?" |
| `insight` | Aha moments, realizations | "The real value is the agent suggesting what to work on" |
| `decision` | Choices made and rationale | "Chose PostgreSQL over SQLite for hybrid search" |
| `project` | Active project status | "Second brain: design complete, implementation pending" |
| `source` | Ingested external content | Full transcript of a YouTube video, article text |

## Relationship Types

| Type | Meaning |
|---|---|
| `supports` | This memory provides evidence for the target |
| `contradicts` | This memory conflicts with the target |
| `extends` | This memory builds on the target |
| `inspires` | This memory sparked the target idea |
| `blocks` | This memory is a blocker for the target |
| `requires` | This memory depends on the target |
| `derived_from` | This memory was synthesized from the target source |

## Crawlee Integration

Crawlee (`~/Work/Tools/Crawlee/`) is the content acquisition tool. It scrapes web pages and extracts YouTube transcripts, outputting clean markdown files. The Second Brain ingestion pipeline reads from Crawlee's output — Crawlee never writes to the database directly.

### Crawlee Output Locations

| Content type | Output path | Format |
|---|---|---|
| YouTube transcripts | `~/Work/Tools/Crawlee/knowledge/sources/*.md` | Markdown with metadata header |
| Web pages | `~/Work/Tools/Crawlee/knowledge/sources/*.md` + `output/*.md` | Markdown with metadata header (knowledge/) + legacy format (output/) |
| Master index | `~/Work/Tools/Crawlee/knowledge/index.md` | Markdown table (title, URL, type, date) |
| Type indexes | `~/Work/Tools/Crawlee/knowledge/indexes/*.md` | Per-type markdown tables |
| Summaries | `~/Work/Tools/Crawlee/knowledge/summaries/*.summary.md` | Markdown |

### File Formats

YouTube transcript files include a structured metadata header:

```
# {title}

Source: {youtube_url}
Type: youtube-transcript
Date: {YYYY-MM-DD}
Uploader: {channel_name}
Duration: {MM:SS}
Video ID: {id}

---

## Transcript

{transcript text}
```

Web page files (in `knowledge/sources/`) include the same metadata header:

```
# {page_title}

Source: {url}
Type: web-page
Date: {YYYY-MM-DD}

---

{page content as markdown}
```

### Ingestion Handoff

All Crawlee content types write to `knowledge/sources/` with consistent metadata headers and register in `knowledge/index.md`. This gives the ingestion pipeline a single discovery mechanism regardless of source type.

1. **Discovery** — Read `knowledge/index.md` to find new entries not yet in PostgreSQL (compare by `source_url`)
2. **Read** — Load the markdown file from `knowledge/sources/`
3. **Parse metadata** — Extract title, source URL, type, date from the header (all types use the same header format)
4. **Chunk** — Split content by topic/section (not fixed-size)
5. **Embed** — Generate vector per chunk via Bedrock Titan/Cohere
6. **Store** — Parent record as `source` type with `source_type` mapped from Crawlee's `Type:` header, chunks as child records with `parent_id`

The index-based discovery means the pipeline only processes new content, avoiding re-ingestion of already-stored sources.

## Chat Ingestion

AI chat conversations are a primary source of decisions, reasoning, and context. Two separate storage systems require two parsers feeding a shared ingestion path. A hybrid two-phase approach splits the work: a deterministic script handles mechanical cleanup (runs unattended via launchd), and an LLM-powered step handles intelligent chunking and metadata extraction.

See `HYBRID-CHAT-EXTRACTION.md` for the full extraction spec, including file formats, stripping rules, filtering thresholds, staging directory layout, and backfill strategy.

### Source Locations (macOS)

| Source | Location | Format |
|---|---|---|
| Kiro CLI | `~/Library/Application Support/kiro-cli/data.sqlite3` | SQLite → `conversations_v2` table, JSON `value` with `transcript` array of strings |
| Kiro IDE (current) | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/.../*.chat` | JSON with `chat` array of `{role, content}` objects |
| Kiro IDE (legacy) | `~/Library/Application Support/Kiro/User/globalStorage/amazonwebservices.aieditoragent/.../*.chat` | Same JSON format as current |

Current volume: ~96 CLI sessions, ~12,400 IDE chats (current), ~49 IDE chats (legacy).

### Hybrid Pipeline Summary

```
Phase 1: Script (launchd, 2:30 AM)       Phase 2: Kiro headless (launchd, 3:00 AM)
├── Read from 3 chat sources              ├── One chat per Kiro session (Opus 4.6-1m)
├── Structural stripping                  ├── Chunk by topic (LLM)
├── Size filtering                        ├── Extract metadata (LLM)
├── Content filtering                     ├── Generate embeddings (Bedrock)
└── Write cleaned chats to staging/       └── Store in PostgreSQL
```

Staging directory: `~/second-brain/staging/chats/` — cleaned markdown files with metadata headers, same format as Crawlee output.

## Backup & Disaster Recovery

Full recovery is possible from either Google Drive or AWS alone. See `DISASTER-RECOVERY.md` for the complete runbook.

| Schedule | What | Where |
|---|---|---|
| Daily 2AM | Encrypted pg_dump + JSON export | Google Drive + S3 |
| Daily 2AM | Config files, skills, scripts | Google Drive + S3 |
| Weekly Sun 3AM | Verification (decrypt + validate both sites) | Local log |

Encryption key stored in: local file + Google Secret Manager + AWS Secrets Manager.

## Current Status

Docker running. PostgreSQL + pgvector operational. Full pipeline tested end-to-end. MCP server built and tested.

## Next Steps (in order)

1. ~~Start Docker Desktop~~ ✅
2. ~~Run migrations~~ ✅
3. ~~Install Python deps~~ ✅
4. ~~Test db.py~~ ✅
5. ~~Test ingest.py end-to-end~~ ✅
6. ~~Build MCP server~~ ✅
7. Connect MCP server to Kiro CLI
8. Run Phase 2 backfill against staging/chats/ (5,561 files)

## Project Status

- [x] Architecture design
- [x] Schema design
- [x] Disaster recovery runbook
- [x] Repo structure and module stubs (`src/`, `scripts/`, `docker/`, `scheduling/`)
- [x] Database migrations system (`migrations/migrate.sh`, numbered SQL files)
- [x] Hybrid chat extraction spec (`docs/HYBRID-CHAT-EXTRACTION.md`)
- [x] Error handling, logging, and monitoring design (staging/failed/, logs/, status JSON)
- [x] Crawlee unified knowledge store (web pages now write to knowledge/index.md alongside YouTube)
- [ ] PostgreSQL + pgvector Docker setup (blocked: Docker Desktop license)
- [ ] Database schema creation (auto-runs via `migrations/migrate.sh`)
- [x] `src/db.py` — PostgreSQL connection, CRUD, semantic search, relationships, deduplication (10 functions)
- [x] `src/embeddings.py` — Bedrock Titan v2 embedding generation (1536-dim, truncation, batch)
- [x] `src/ingest.py` — shared ingestion pipeline (parse header → chunk by section → embed → store)
- [x] `src/mcp_server.py` — MCP server (7 tools: create, search, read, update, relate, list, graph)
- [x] `src/parsers/crawlee.py` — Crawlee knowledge store parser — 290 sources (1 YouTube + 289 web pages)
- [x] `src/parsers/cli_chat.py` — Kiro CLI chat parser (Phase 1) — 68/97 pass (70%)
- [x] `src/parsers/ide_chat.py` — Kiro IDE chat parser (Phase 1) — 5,493/12,447 pass (44%)
- [x] `scripts/chat_extract.py` — daily Phase 1 chat extraction — 5,561 total, backfill complete
- [x] `scripts/ingest_staged.sh` — Phase 2 wrapper (Kiro headless + Opus 4.6-1m)
- [x] `scripts/crawlee_ingest.py` — daily Crawlee ingestion
- [ ] Capture API (multi-channel: Slack, mobile, browser, email)
- [ ] Metadata extraction on ingest (people, topics, action items)
- [ ] Memory migration (Claude, ChatGPT, Notion imports)
- [ ] Weekly synthesis review (scheduled digest)
- [ ] Backup scripts (rclone + GPG)
- [x] Backup scheduling (launchd plists — 4 plists in scheduling/, ready to symlink)
- [x] Weekly verification scheduling (included in launchd plists above)
- [ ] Encryption key setup (Google + AWS)
- [ ] Integration testing
