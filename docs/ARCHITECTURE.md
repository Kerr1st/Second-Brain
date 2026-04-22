# Second Brain — Architecture

> Last updated: 2026-04-22

## Overview

A personal knowledge system that gives AI agents persistent memory across sessions. PostgreSQL + pgvector stores memories, relationships, and a knowledge graph. An MCP server exposes this to Kiro CLI, Claude Code, and other agents. A dream cycle pipeline autonomously surfaces connections and insights.

## System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  Capture Channels                                           │
│  Kiro CLI · Kiro IDE · Quick Desktop (Slack/Outlook/Cal)    │
│  Crawlee (web/YouTube) · Capture API · Browser · Mobile     │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Ingestion Pipeline                                         │
│  Parse → Classify → Chunk → Embed (Bedrock Titan 1024d)     │
│  → Store → Discover relationships                           │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  PostgreSQL 17 + pgvector (Docker)                          │
│                                                             │
│  ┌───────────┐ ┌────────────────┐ ┌──────────────────────┐  │
│  │ memories   │ │ relationships  │ │ knowledge graph      │  │
│  │ 74K+ rows  │ │ source→target  │ │ entities (2.1K)      │  │
│  │            │ │ typed edges    │ │ entity_edges (4.6K)  │  │
│  │            │ │                │ │ memory_entities      │  │
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
│  8 tools: create, search, read, update, relate,            │
│           list, graph, learn                                │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Agents: Kiro CLI · Claude Code · Dream Cycle Pipeline      │
│  LLM: Amazon Bedrock (Claude for reasoning)                 │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

1. **Capture** — Content arrives from multiple channels (AI chats, Quick Desktop, Crawlee scrapes, manual input, Capture API)
2. **Ingest** — Content is parsed, classified (semantic/episodic/procedural), chunked, embedded, and stored with auto-discovered relationships
3. **Retrieve** — Hybrid search (BM25 + vector + RRF) with cognitive science-grounded reranking
4. **Synthesize** — Dream cycle pipeline (Explorer → Thinker → 4-evaluator Consensus Panel) autonomously surfaces connections and insights
5. **Backup** — Daily encrypted pg_dump + JSON exports to Google Drive and S3

## Database Schema

9 migrations applied in order (`migrations/000` through `008`).

### Core Tables

```sql
-- memories: The primary knowledge store
CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,              -- idea, synthesis, research, insight, question, decision, priority, project, connection, source
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    embedding vector(1024),
    tags TEXT[] DEFAULT '{}',
    source_url TEXT,
    source_type TEXT,                -- youtube, article, pdf, course, kiro_cli_chat, kiro_ide_chat, quick_desktop, notes, manual
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
    encoding_context TEXT,           -- cognitive context at creation time (migration 006)
    schema_type TEXT                 -- memory schema type (migration 007)
);

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

### Knowledge Graph Tables (migration 008)

Imported from Quick Desktop's knowledge graph. Entities represent real-world objects (people, tools, projects); edges represent factual relationships between them. The bridge table links entities to memories.

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
             + 0.12 * recency              (exponential decay, τ=60 days)
             + 0.08 * length_score
             + 0.05 * depth_score
             + type_boost                   (0.06 for idea/synthesis/insight/decision)
             + mem_class_boost              (0.04 semantic, 0.02 procedural, 0.00 episodic)
             + reinforcement                (0.03 * log1p(access_count) * spacing_bonus)
             + project_penalty              (-0.15 if cross-project)
```

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
| Knowledge store | PostgreSQL 17 + pgvector (Docker) |
| Agent interface | MCP server (`src/mcp_server.py`) |
| LLM | Amazon Bedrock (Claude) |
| Embeddings | Amazon Bedrock (Titan v2, 1024-dim) |
| Scheduling | macOS launchd |
| Backup transport | rclone (Google Drive + S3) |
| Backup encryption | GPG (AES-256) |
| Content acquisition | Crawlee (Node.js, `~/Work/Tools/Crawlee/`) |
| QD sync | Quick Desktop (`~/.quickwork/knowledge_v1.db`) |

## Dream Cycle Pipeline

Four-agent autonomous learning system (`src/dream_cycle/`):

1. **Explorer** — Assembles "memory slices" using 10 strategies (temporal juxtaposition, cross-project collision, orphan archaeology, etc.)
2. **Thinker** — Analyzes slices, proposes candidate insights (CREATE, UPDATE, or SUPERSEDE operations)
3. **Consensus Panel** — 4 evaluators (Skeptic, User Advocate, Epistemologist, Methodologist) vote independently. Binary BFT: ≥3/4 = ACCEPTED
4. **Storage** — Accepted candidates become memories with `tags: ["dream-cycle"]` and `metadata: {"dream_cycle": true}`

Digests written to `logs/dream-cycle-digest-*.md`.

## Source Code Layout

```
src/
  mcp_server.py          MCP server (8 tools)
  db.py                  PostgreSQL connection, memory CRUD, relationships
  search.py              Hybrid search, reranking, retrieval reinforcement
  embeddings.py          Bedrock Titan embedding generation
  ingest.py              Ingestion pipeline
  classify.py            Memory classifier (semantic/episodic/procedural)
  depth.py               Depth scorer (0.0–1.0)
  capture_api.py         HTTP capture endpoint
  models.py              Data models
  agent_invoker.py       Subprocess-based agent invocation
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
