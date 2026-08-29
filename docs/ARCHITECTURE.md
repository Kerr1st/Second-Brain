# Second Brain — Architecture

> Last updated: 2026-08-29

## Overview

A personal knowledge system that gives AI agents persistent memory across sessions. PostgreSQL + pgvector stores memories and their typed relationships. An MCP server exposes this to Kiro CLI, Claude Code, and other agents. A dream cycle pipeline autonomously surfaces connections and insights. (An entity knowledge graph was imported from Quick Desktop but is currently **dormant** — unused by retrieval/synthesis; see below.)

## Component Documentation

The [Architecture Component Index](components/index.md) is the canonical
navigation layer for component ownership, contracts, runtime flows, code entry
points, tests, operations, and related decisions. This document remains the
breadth-first system view; component pages provide the maintained depth.

## System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  Capture Channels                                           │
│  Codex Tasks · Kiro CLI · Kiro IDE chats · Quick Desktop    │
│  (docs, chats, feed events) · YouTube · Web articles        │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Ingestion Pipeline                                         │
│  Parse → Classify → Chunk → Embed (Bedrock Titan 1024d)     │
│  → Store → Discover relationships                           │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  PostgreSQL 17 + pgvector (native)                          │
│                                                             │
│  ┌───────────┐ ┌────────────────┐ ┌──────────────────────┐  │
│  │ memories   │ │ relationships  │ │ knowledge graph      │  │
│  │ ~121K rows │ │ source→target  │ │ (imported from QD,   │  │
│  │            │ │ typed edges    │ │ DORMANT — unused in  │  │
│  │            │ │                │ │ retrieval/synthesis) │  │
│  └───────────┘ └────────────────┘ └──────────────────────┘  │
│                                                             │
│  ┌───────────────────┐ ┌──────────────────────────────────┐ │
│  │ dream_cycle_runs  │ │ dream_cycle_candidates           │ │
│  │ run metadata      │ │ 4-evaluator BFT consensus        │ │
│  └───────────────────┘ └──────────────────────────────────┘ │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  MCP Server (src/mcp_server.py)                             │
│  11 tools: create, search, context, outcome, read, update, │
│            relate, list, graph, learn, brief                │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Agents: Kiro CLI · Claude Code · Dream Cycle Pipeline      │
│  LLM: Kiro CLI (Opus 4.8), backend-selectable              │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

1. **Capture** — Content arrives from multiple channels (Kiro CLI/IDE chats, Quick Desktop, YouTube transcripts, web scrapes)
2. **Ingest** — Content is parsed, classified (semantic/episodic/procedural), chunked, embedded, and stored with auto-discovered relationships
3. **Retrieve** — Hybrid search (BM25 + vector + RRF) with cognitive science-grounded reranking
4. **Synthesize** — Dream cycle pipeline (Explorer → Thinker → 4-evaluator Consensus Panel) autonomously surfaces connections and insights
5. **Deliver (Express)** — surfaces synthesized output to the user: on-demand `brief`, a gated Gmail push chained after the noon dream cycle, and the `memory_brief` MCP tool; shaped by delivery preferences (`express_feedback`, migration 010)
6. **Backup** — Daily encrypted pg_dump + JSON exports to Google Drive (+ local + git). **S3 was de-scoped 2026-06-01** (brittle overnight SSO).

### Agentic-assistant capture standard

All agentic-assistant integrations follow one accepted source model: a stable Agent Task contains
ordered Agent Turns, and each turn preserves the user's prompt plus the agent's visible outcome.
Complete turns are grouped into semantic Topic Segments using distinct purpose, coherence, and
independent search or distillation value. All qualifying segments remain searchable, including
segments that yield no immediate decision, insight, or Correction Episode.

Before eligibility, each Source Connector classifies Task Ownership from native evidence as
`user-owned`, `delegated`, or `unknown`. Only User-Owned Tasks continue. Delegated Tasks and
Unknown-Ownership Tasks are skipped and reported, with delegation evidence taking precedence over
conflicting user markers. Codex is the reference implementation; each later connector must
document and fixture-test its own evidence before activation. Shared runtime code is extracted only
after a second connector proves the seam. See
[ADR 0008](adr/0008-classify-task-ownership-before-capture.md).

Exact Provenance is the direct chain from a derived memory to its supporting Agent Turns, Topic
Segment, Captured Task, and native Agent Task identity. Processing diagnostics such as model
versions, hashes, timing, and usage are not part of provenance. See the canonical
[capture definition](CAPTURE-COMPONENTS.md#exact-provenance).

Native project, workspace, and repository context remain source provenance; they never become the
semantic `memories.project` value automatically. A semantic project is assigned only when the task
content clearly establishes the subject. See
[ADR 0004](adr/0004-separate-source-provenance-from-semantic-project.md).
Codex Build 1 preserves the provenance fields but deliberately defers content-based semantic
project classification, so its records currently leave `project` unset.

Captured Task history grows monotonically. Once stored, an Agent Turn is immutable; a refresh
appends only unseen complete turns. Changed, missing, or reordered known turns are Source Drift and
are ignored without recording additional capture state. When new turns append, semantic processing receives
the prior tail segment as context plus those new turns. See the canonical
[capture policy](CAPTURE-COMPONENTS.md#monotonic-capture-policy) and
[ADR 0003](adr/0003-accumulate-captured-task-history-monotonically.md).

For each eligible Agent Task revision, source capture commits first. The same scheduled run then
uses one Task Semantic Pass to return Topic Segments and zero or more supported decisions or
insights for each segment, plus a Correction Episode when a user specifically corrects a prior
visible agent outcome. A Correction Episode is stored as episodic, user-attributed evidence. It is
searchable and available to the Dream Cycle, but is not a Steering Rule and is excluded from
automatic steering context and proactive Express delivery. Topic Segmentation and Task
Distillation remain distinct concepts but are not separate model calls, and segments do not
require summaries. The Dream Cycle remains
separate from capture and, on its own schedule, performs evaluator-gated, higher-order synthesis
across memories, including contradiction discovery; Codex v1 has no immediate contradiction
planner. Express delivers selected results. Source-specific connectors may differ in access and
eligibility rules, but not in this downstream model. See
[ADR 0002](adr/0002-standardize-agent-task-capture.md) and
[ADR 0006](adr/0006-combine-task-segmentation-and-distillation.md). The deferred
Correction Episode promotion path is specified in
[ADR 0007](adr/0007-capture-correction-episodes-before-steering.md).

The combined semantic result stores atomically. Failure leaves the source capture intact, stores no
partial semantic output, and leaves one Semantic Processing Cursor at the last successfully
processed Agent Turn. The next capture invocation retries the whole unprocessed tail; once hourly
scheduling is approved, that is normally the next hourly run. There are no stage-specific
semantic retries.

Integration names also define identity namespaces. Amazon Quick is a web source and Quick Desktop
is a separate desktop source; although both call their units sessions, their storage, connectors,
and captured identities remain separate.

## Database Schema

The migration runner applies schema migrations `001` through `013` in order; migration `000`
bootstraps the version-tracking table.

### Core Tables

```sql
-- memories: The primary knowledge store
CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,              -- idea, synthesis, research, insight, question, decision, correction_episode, priority, project, connection, source
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    embedding vector(1024),
    tags TEXT[] DEFAULT '{}',
    source_url TEXT,
    source_type TEXT,                -- youtube, article, kiro_cli_chat, kiro_ide_chat, distilled_chat, quick_desktop_doc, quick_desktop_chat, quick_desktop_feed, ... (see DB for full set)
    metadata JSONB DEFAULT '{}',
    status TEXT DEFAULT 'active',    -- active, explored, archived, superseded, user_rejected
    confidence FLOAT DEFAULT 1.0,
    parent_id UUID REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    search_vector TSVECTOR,          -- auto-populated trigger: title+questions=A, content=B
    access_count INTEGER DEFAULT 0,
    mem_class TEXT,                   -- semantic, episodic, procedural
    project TEXT,
    last_accessed_at TIMESTAMPTZ,
    encoding_context TEXT            -- cognitive context at creation time (migration 006)
);
-- NOTE: migration 007 added partial INDEXES for schema-type memories
-- (type='schema' + derived_from edges) — NOT a column. There is no schema_type
-- column, and the schema feature is currently inert (0 type='schema' rows).

-- memory_relationships: Typed edges between memories
CREATE TABLE memory_relationships (
    source_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    target_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,      -- supports, contradicts, extends, inspires, blocks, requires, derived_from, related_to, superseded_by
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    expired_at TIMESTAMPTZ,
    PRIMARY KEY (source_id, target_id, relation_type)
);
```

### Knowledge Graph Tables (migration 008) — DORMANT

**Status: imported from Quick Desktop but unused by retrieval, synthesis, or Express (0 `src/` references).** Retained for possible future use; see `AGENTIC-RETRIEVAL-PLAN.md` (the KG is ~99.5% disconnected from memories, so it is deferred). Entities represent real-world objects (people, tools, projects); edges represent factual relationships between them; the bridge table links entities to memories.

```sql
CREATE TABLE entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category TEXT NOT NULL,          -- Person, Product, CreativeWork, DefinedTerm, Organization, etc.
    name TEXT NOT NULL,
    summary TEXT,
    properties JSONB DEFAULT '{}',
    source_type TEXT,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(category, name)
);

CREATE TABLE entity_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity UUID REFERENCES entities(id) ON DELETE CASCADE,
    to_entity UUID REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,          -- mentions, about, relatedTo, isPartOf, worksFor, etc.
    weight REAL DEFAULT 1.0,
    properties JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(from_entity, to_entity, relation)
);

CREATE TABLE memory_entities (
    memory_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    entity_id UUID REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT DEFAULT 'mentions',
    PRIMARY KEY (memory_id, entity_id, relation)
);
```

### Dream Cycle Tables (migration 003-004)

```sql
CREATE TABLE dream_cycle_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type TEXT NOT NULL,          -- scheduled, post_learn, session_start, user_triggered
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
    operation TEXT,                   -- CREATE, UPDATE, SUPERSEDE
    target_memory_id UUID,
    schema_operation TEXT,            -- assimilation, accommodation
    evaluator_a_verdict TEXT,         -- Skeptic
    evaluator_a_reasoning TEXT,
    evaluator_b_verdict TEXT,         -- User Advocate
    evaluator_b_reasoning TEXT,
    evaluator_c_verdict TEXT,         -- Epistemologist
    evaluator_c_reasoning TEXT,
    evaluator_d_verdict TEXT,         -- Methodologist
    evaluator_d_reasoning TEXT,
    final_verdict TEXT,               -- ACCEPTED, REJECTED
    created_memory_id UUID REFERENCES memories(id),
    user_rejected_at TIMESTAMPTZ,
    user_rejection_reason TEXT,
    deferred_twice_rejected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

### Indexes

```sql
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

## Search Architecture

Hybrid retrieval with cognitive science-grounded reranking. Implementation: `src/search.py`.

1. **BM25 full-text search** — PostgreSQL `tsvector/tsquery` with GIN index
2. **Vector cosine search** — pgvector HNSW index (1024-dim Titan embeddings)
3. **Reciprocal Rank Fusion** — `score = 1/(k+rank_vec) + 1/(k+rank_bm25)`, k=60
4. **Utility reranking** — weighted scoring (see below)
5. **Retrieval reinforcement** — `access_count` incremented on retrieval, `last_accessed_at` updated
6. **Temporal context** — top result's temporal neighbors (±24h) appended

### Reranking Formula

```
rerank_score = 0.30 * rrf_score
             + 0.18 * token_overlap
             + 0.18 * title_overlap
             + 0.10 * context_overlap       (encoding context match)
             + 0.12 * recency              (power-law decay with stability)
             + 0.08 * length_score
             + 0.05 * depth_score
             + type_boost                   (0.06 for idea/synthesis/insight/decision)
             + mem_class_boost              (0.04 semantic, 0.02 procedural, 0.00 episodic)
             + reinforcement                (0.03 * log1p(access_count) * spacing_bonus)
             + project_penalty              (-0.15 if cross-project)
             + superseded_penalty           (-0.20 if status=superseded)
             + staleness_penalty            (-0.05 if unretrieved >90 days)
```

> **Authoritative source:** `src/rerank_weights.py` is the single source of truth for these weight values; both the production scorer (`src/search.py`) and the evaluation scorer derive from it.

See `docs/DESIGN-DECISIONS.md` for the cognitive science rationale.

## Memory Types

| Type | Purpose |
|---|---|
| `research` | Raw information gathered |
| `synthesis` | Your analysis of information |
| `idea` | Hypotheses, project concepts |
| `connection` | Links between concepts |
| `priority` | What to work on and why |
| `question` | Open threads to explore |
| `insight` | Aha moments, realizations |
| `decision` | Choices made and rationale |
| `correction_episode` | User-attributed evidence that a prior visible agent outcome was misaligned and what the user expected instead |
| `project` | Active project status |
| `source` | Ingested external content |

## Relationship Types

| Type | Meaning |
|---|---|
| `supports` | Provides evidence for the target |
| `contradicts` | Conflicts with the target |
| `extends` | Builds on the target |
| `inspires` | Sparked the target idea |
| `blocks` | Blocker for the target |
| `requires` | Depends on the target |
| `derived_from` | Synthesized from the target source |
| `related_to` | Auto-discovered semantic/temporal neighbor |
| `superseded_by` | Replaced by the target (dream cycle) |

## Technology Stack

| Component | Technology |
|---|---|
| Knowledge store | PostgreSQL 17 + pgvector (native Homebrew, localhost:5432; Docker container retained as rollback pending Phase 5 decommission) |
| Agent interface | MCP server (`src/mcp_server.py`) |
| LLM (reasoning) | Kiro CLI (Claude Opus 4.8) under the current Kiro plan; pluggable per-machine via `config/backends.toml` (see MODEL-BACKENDS.md) |
| Embeddings | Amazon Bedrock (Titan v2, 1024-dim) |
| Scheduling | macOS launchd |
| Backup transport | rclone (Google Drive) + local + git — S3 de-scoped 2026-06-01 |
| Backup encryption | GPG (AES-256) |
| YouTube capture | yt-dlp + curl_cffi (in-repo, `src/capture/youtube.py`) |
| Web/article acquisition | Crawlee (Node.js, `~/Work/Tools/Crawlee/`) |
| QD sync | Quick Desktop (`knowledge_v1.db`, `sessions.db`, `eventlog/`, `slack_cache/`) |

## Dream Cycle Pipeline

Four-agent autonomous learning system (`src/dream_cycle/`):

1. **Explorer** — Assembles "memory slices" using 11 strategies (temporal juxtaposition, cross-project collision, orphan archaeology, etc.)
2. **Thinker** — Analyzes slices, proposes candidate insights (CREATE, UPDATE, or SUPERSEDE operations)
3. **Consensus Panel** — 4 evaluators (Skeptic, User Advocate, Epistemologist, Methodologist) vote independently. Binary BFT: ≥3/4 = ACCEPTED
4. **Storage** — Accepted candidates become memories with `tags: ["dream-cycle"]` and `metadata: {"dream_cycle": true}`

Digests written to `logs/dream-cycle-digest-*.md`.

## Express (Delivery Layer)

The Partnership rung — surfaces synthesized output to the user instead of waiting to be queried.
Implementation: `src/express.py`, `scripts/brief.py`, `scripts/express_push.py`.

- **compose_briefing** gathers from existing synthesis: recent dream-cycle insights (cross-project detected), active `contradicts` edges, resurfaced high-value/forgotten memories, the weekly digest, open questions. **edit_briefing** ranks and writes headlines (LLM editor, deterministic fallback).
- **Surfaces:** on-demand `brief` (CLI); a gated Gmail push (`should_push` sends only on a new cross-project synthesis or contradiction, chained after the noon dream cycle); the `memory_brief` MCP tool (in-session).
- **Feedback (delivery preferences):** `brief --useful/--less/--mute/--unmute` targeting item/kind/topic, stored in `express_feedback` (migration 010); applied as hard filters + soft re-rank. Delivery-only (does not yet feed synthesis).

## Source Code Layout

```
src/
  mcp_server.py          MCP server (11 tools)
  express.py             Express delivery: briefing compose/edit/render, feedback, Gmail push
  db.py                  PostgreSQL connection, memory CRUD, relationships
  search.py              Hybrid search, reranking, retrieval reinforcement
  embeddings.py          Bedrock Titan embedding generation
  ingest.py              Ingestion pipeline
  classify.py            Memory classifier (semantic/episodic/procedural)
  depth.py               Depth scorer (0.0–1.0)
  capture_api.py         HTTP capture endpoint (DEPRECATED — 0 captures, slated for removal)
  capture/               Self-contained capture sources (youtube.py); see CAPTURE-COMPONENTS.md
  models.py              Data models
  agent_invoker.py       Backward-compat shim → re-exports backends.kiro.KiroInvoker
  backends/              Pluggable model-backend layer (see MODEL-BACKENDS.md)
    base.py              Invoker Protocol, BackendCapabilities, capability table
    kiro.py              KiroInvoker — Kiro CLI (the default adapter)
    resolver.py          Resolves an Invoker per role from config/backends.toml
  parsers/               Chat and content parsers
  prompts/               Agent prompt templates
  dream_cycle/
    orchestrator.py      Pipeline coordination
    consensus.py         BFT consensus tally
    storage.py           Memory persistence (CREATE/UPDATE/SUPERSEDE)
    digest.py            Markdown digest generation
    feedback.py          Feedback injection from rejections
  dream_cycle_db.py      Dream cycle DB operations
```
