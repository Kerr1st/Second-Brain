# Second Brain — Agent Memory System

## Purpose

This system is a personal knowledge management and ideation platform powered by AI. It gives AI agents (Kiro CLI, Claude Code) persistent memory across sessions, turning them from stateless tools into proactive thinking partners that know how you work, what you're working on, and what you've learned.

The core problem it solves: AI agents forget everything between sessions. Session 100 is no better than session 1. This system makes the agent accumulate institutional knowledge over time — your decisions, ideas, research, syntheses, and open questions — so it can reason across your entire knowledge base and help you think.

But storage and retrieval is just the foundation. The real purpose is to create a proactive collaborator — an agent that surfaces connections, patterns, and ideas you can't see because no human can hold 74,000+ memories in their head simultaneously. The agent digests and synthesizes at a scale you can't, finding hidden structure in your thinking: contradictions between decisions, recurring patterns across unrelated projects, implicit principles you follow but haven't named. It doesn't wait to be asked — it tells you what it found.

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
│  │  priorities  │  │  notes       │  │  related_to   │  │
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

Six migrations applied in order (`migrations/000` through `005`).

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
    metadata JSONB DEFAULT '{}', -- flexible extracted metadata (depth_score, people, topics, action_items, files)
    status TEXT DEFAULT 'active', -- active, explored, archived, superseded, user_rejected
    confidence FLOAT DEFAULT 1.0,
    parent_id UUID REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    search_vector TSVECTOR,      -- auto-populated by weighted trigger: title+questions=A, content=B (migration 005)
    access_count INTEGER DEFAULT 0, -- retrieval reinforcement (testing effect)
    -- V2 columns (migration 002)
    mem_class TEXT,               -- semantic, episodic, procedural (Tulving 1972)
    project TEXT,                 -- project scope for encoding specificity (Tulving & Thomson 1973)
    last_accessed_at TIMESTAMPTZ  -- last retrieval time for spaced retrieval (Roediger & Karpicke 2006)
);

CREATE TABLE memory_relationships (
    source_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    target_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,  -- supports, contradicts, extends, inspires, blocks, requires, derived_from, related_to, superseded_by
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    expired_at TIMESTAMPTZ,      -- temporal awareness (migration 003)
    PRIMARY KEY (source_id, target_id, relation_type)
);

-- Dream cycle tables (migration 003)
CREATE TABLE dream_cycle_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type TEXT NOT NULL,       -- scheduled, post_learn, session_start, user_triggered
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    explorer_output JSONB,
    explorer_feedback_injected TEXT,
    candidates_generated INTEGER,
    candidates_accepted INTEGER,
    candidates_deferred INTEGER,
    candidates_rejected INTEGER,
    digest TEXT
);

CREATE TABLE dream_cycle_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES dream_cycle_runs(id),
    candidate_json JSONB,
    operation TEXT,               -- CREATE, UPDATE, SUPERSEDE
    target_memory_id UUID,
    schema_operation TEXT,        -- assimilation, accommodation
    evaluator_a_verdict TEXT,     -- Skeptic
    evaluator_a_reasoning TEXT,
    evaluator_b_verdict TEXT,     -- User Advocate
    evaluator_b_reasoning TEXT,
    evaluator_c_verdict TEXT,     -- Epistemologist
    evaluator_c_reasoning TEXT,
    evaluator_d_verdict TEXT,     -- Methodologist (migration 004)
    evaluator_d_reasoning TEXT,
    final_verdict TEXT,           -- ACCEPTED, REJECTED
    created_memory_id UUID REFERENCES memories(id),
    user_rejected_at TIMESTAMPTZ,
    user_rejection_reason TEXT,
    deferred_twice_rejected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON memories USING gin (search_vector);
CREATE INDEX ON memories (type, status);
CREATE INDEX ON memories USING gin (tags);
CREATE INDEX ON memories USING gin (metadata);
CREATE INDEX ON memories (created_at DESC);
CREATE INDEX ON memories (source_type);
CREATE INDEX ON memories (mem_class);
CREATE INDEX ON memories (project);
CREATE INDEX ON memories (last_accessed_at DESC);
```

### Search Architecture

Queries use hybrid retrieval with cognitive science-grounded reranking. Implementation lives in `src/search.py` (extracted from `db.py`).

1. **BM25 full-text search** — PostgreSQL `tsvector/tsquery` with GIN index. Catches exact keyword matches.
2. **Vector cosine search** — pgvector HNSW index. Catches semantic similarity.
3. **Reciprocal Rank Fusion (RRF)** — Merges both result sets: `score = 1/(k+rank_vec) + 1/(k+rank_bm25)`, k=60.
4. **Utility reranking** — V2 formula (see below).
5. **Retrieval reinforcement** — `access_count` incremented on every retrieval. `last_accessed_at` updated for spaced retrieval.
6. **Temporal context** — Top search result's temporal neighbors (±24h) appended to response.

#### V2 Reranking Formula

```
rerank_score = 0.30 * rrf_score
             + 0.18 * token_overlap
             + 0.18 * title_overlap
             + 0.12 * recency              (exponential decay, τ=60 days)
             + 0.08 * length_score
             + 0.05 * depth_score           (numeric 0.0–1.0 from src/depth.py)
             + type_boost                   (0.06 for idea/synthesis/insight/decision)
             + mem_class_boost              (0.04 semantic, 0.02 procedural, 0.00 episodic)
             + reinforcement                (0.03 * log1p(access_count) * spacing_bonus)
             + project_penalty              (-0.15 if cross-project)
```

`spacing_bonus = min(1.0, days_since_last_access / 7.0)` — dampens same-session re-retrievals, rewards spaced retrieval (Roediger & Karpicke 2006).

See `docs/DESIGN-DECISIONS.md` for the cognitive science rationale behind these choices.

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
| `related_to` | Auto-discovered semantic or temporal neighbor (created at ingest time) |
| `superseded_by` | This memory was replaced by the target (dream cycle SUPERSEDE operation) |

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

## Scheduled Jobs

All jobs use `scripts/job_wrapper.sh` which sends macOS notification on failure.

| Time | Job | Script | Status |
|---|---|---|---|
| Sat 1:00 AM | Bookmark scrape | scrape_bookmarks.py | Working |
| Daily 1:30 AM | YouTube scrape | youtube_scrape.sh | Working |
| Daily 2:00 AM | Backup | backup.sh | Working |
| Daily 2:30 AM | Chat extraction | chat_extract.py | Working |
| Daily 3:00 AM | Staged ingestion | ingest_staged.sh | Working |
| Sun 3:00 AM | Backup verify | verify_backup.sh | Working |
| Sun 4:00 AM | Dream cycle | dream_cycle_scheduled.sh | Not loaded (plist exists but not installed in launchd) |

## Current Status

All core features implemented and operational. 74K+ memories in PostgreSQL. Dream cycle has run once live, producing 4 insights and 2 syntheses. 243 tests passing (property-based, unit, integration). Daily scheduled jobs running (backup, chat extraction, bookmarks). MCP server exposes 8 tools to AI agents.

## Next Steps

1. Load dream cycle launchd job and validate BFT consensus panel with scheduled runs
2. Capture API — multi-channel input (Slack, mobile, browser, email)
3. Memory migration — import from Claude, ChatGPT, Notion
4. Metadata extraction on ingest — auto-extract people, topics, action items

## Project Status

### Infrastructure
- [x] Architecture design
- [x] Schema design (6 migrations: 000-005)
- [x] Disaster recovery runbook
- [x] Repo structure (`src/`, `scripts/`, `docker/`, `scheduling/`, `tests/`, `migrations/`)
- [x] Database migrations system (`migrations/migrate.sh`, numbered SQL files)
- [x] PostgreSQL + pgvector Docker setup
- [x] Encryption key generated and stored locally
- [x] Backup scheduling (7 launchd plists in `scheduling/`, all installed)
- [x] `scripts/backup.sh` — daily encrypted backup to Google Drive + S3
- [x] `scripts/verify_backup.sh` — weekly backup verification (both clouds)
- [x] `scripts/job_wrapper.sh` — macOS notification on job failure

### Core Library (`src/`)
- [x] `src/db.py` — PostgreSQL connection, memory CRUD, semantic search, relationships, dedup helpers
- [x] `src/search.py` — hybrid search (BM25 + vector + RRF), V2 reranking, retrieval reinforcement
- [x] `src/embeddings.py` — Bedrock Titan v2 embedding generation (1024-dim)
- [x] `src/ingest.py` — ingestion pipeline (parse → classify → chunk → embed → store → discover relationships)
- [x] `src/classify.py` — deterministic memory classifier (semantic/episodic/procedural)
- [x] `src/depth.py` — numeric depth scorer (0.0–1.0)
- [x] `src/mcp_server.py` — MCP server (8 tools: create, search, read, update, relate, list, graph, learn)
- [x] `src/models.py` — data models (DreamCycleResult, MemorySlice, CandidateInsight, EvaluatorVerdict)
- [x] `src/agent_invoker.py` — subprocess-based agent invocation with JSON parsing
- [x] `src/parsers/` — chat and content parsers (crawlee, cli_chat, ide_chat)
- [x] `src/prompts/` — agent prompt templates (explorer, thinker, panel with 4 evaluator roles)

### Dream Cycle (`src/dream_cycle/`)
- [x] `orchestrator.py` — four-agent pipeline coordination (Explorer → Thinker → 4-evaluator Consensus Panel)
- [x] `consensus.py` — binary BFT consensus tally (≥3/4 ACCEPTED, ≤2/4 REJECTED)
- [x] `storage.py` — memory storage operations (CREATE, UPDATE, SUPERSEDE) with deduplication
- [x] `digest.py` — markdown digest generation with dissent annotations
- [x] `feedback.py` — feedback injection from recent rejections and accepted dissents
- [x] `src/dream_cycle_db.py` — dream cycle database operations (18 functions)

### Ingestion & Capture
- [x] `scripts/chat_extract.py` — daily Phase 1 chat extraction (5,561 chats backfilled)
- [x] `scripts/ingest_staged.sh` — Phase 2 wrapper (Kiro headless + Opus 4.6-1m)
- [x] `scripts/crawlee_ingest.py` — Crawlee content ingestion
- [x] `scripts/ingest_chats.py` — batch chat ingestion (5,561 chats → 74K+ memories)
- [x] `scripts/scrape_bookmarks.py` — Chrome bookmarks scraper
- [x] `scripts/youtube_scrape.sh` — YouTube playlist/history scrape
- [x] Hybrid chat extraction spec (`docs/HYBRID-CHAT-EXTRACTION.md`)
- [ ] Capture API (multi-channel: Slack, mobile, browser, email)
- [ ] Metadata extraction on ingest (people, topics, action items)

### Testing
- [x] `tests/` — 243 tests (property-based with Hypothesis, unit, integration)
- [x] Test database fixture (isolated `memory_bank_test` DB)
- [x] Embedding mock (deterministic, no Bedrock calls)
- [x] `scripts/golden_queries.py` — Tier 3 metrics (MRR, hit rates)

### V2 — Cognitive Science Enhancements

All Layer 1 retrieval improvements implemented. See `docs/DESIGN-DECISIONS.md` for research grounding.

- [x] Memory classification — auto-classify semantic/episodic/procedural (Tulving 1972), boost semantic in reranking
- [x] Spaced retrieval reinforcement — `last_accessed_at` tracking, spacing bonus modulates reinforcement (Roediger & Karpicke 2006)
- [x] Project scoping — `project` column, cross-project penalty in reranking (Tulving & Thomson 1973)
- [x] Depth scoring — numeric 0.0–1.0 in metadata, reranking factor (Craik & Lockhart 1972)
- [x] Temporal contiguity — surface temporal neighbors of top results (Howard & Kahana 2002)
- [x] Automatic relationship discovery — semantic + temporal neighbors linked at ingest time
- [x] `memory_learn` MCP tool — elaborative interrogation (Dunlosky 2013)
- [x] Dream cycle pipeline — four-agent autonomous learning with 4-evaluator binary BFT consensus (Lamport 1982)
- [x] Dream cycle module decomposition — `src/dream_cycle/` package with 6 focused modules
- [x] Data layer decomposition — `src/search.py` extracted from `src/db.py`
- [x] "Questions this answers" BM25 indexing — weighted tsvector trigger (migration 005): title+questions=Weight A, content=Weight B

### Future Work
- [ ] Memory migration (Claude, ChatGPT, Notion imports)
- [ ] Evaluation framework (retrieval accuracy, consolidation quality, cold vs warm comparison)
