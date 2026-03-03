# Second Brain — Agent Memory System

## Purpose

This system is a personal knowledge management and ideation platform powered by AI. It gives AI agents (Kiro CLI, Claude Code) persistent memory across sessions, turning them from stateless tools into thinking partners that know how you work, what you're working on, and what you've learned.

The core problem it solves: AI agents forget everything between sessions. Session 100 is no better than session 1. This system makes the agent accumulate institutional knowledge over time — your decisions, ideas, research, syntheses, and open questions — so it can reason across your entire knowledge base and help you think.

## Workflow

1. You gather information (YouTube, articles, PDFs, courses, conversations)
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
│  │  insights    │  │  notes       │  │  extends      │  │
│  │  questions   │  │  courses     │  │  inspires     │  │
│  │  priorities  │  │              │  │  derived_from │  │
│  └─────────────┘  └──────────────┘  └───────────────┘  │
├─────────────────────────────────────────────────────────┤
│  Ingestion Pipeline                                     │
│  MCP tools for frictionless content capture:            │
│  YouTube → transcript → chunk → embed → store           │
│  Articles → scrape → chunk → embed → store              │
│  PDFs → extract → chunk → embed → store                 │
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

## Database Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,           -- idea, synthesis, research, insight, question, decision, priority, project, source
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,                 -- agent-generated summary for long content
    embedding vector(1536),
    tags TEXT[] DEFAULT '{}',
    source_url TEXT,              -- origin: YouTube URL, article URL, etc.
    source_type TEXT,             -- youtube, article, pdf, conversation, manual
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

## Backup & Disaster Recovery

Full recovery is possible from either Google Drive or AWS alone. See `DISASTER-RECOVERY.md` for the complete runbook.

| Schedule | What | Where |
|---|---|---|
| Daily 2AM | Encrypted pg_dump + JSON export | Google Drive + S3 |
| Daily 2AM | Config files, skills, scripts | Google Drive + S3 |
| Weekly Sun 3AM | Verification (decrypt + validate both sites) | Local log |

Encryption key stored in: local file + Google Secret Manager + AWS Secrets Manager.

## Project Status

- [x] Architecture design
- [x] Schema design
- [x] Disaster recovery runbook
- [ ] PostgreSQL + pgvector Docker setup
- [ ] Database schema creation
- [ ] MCP server (memory read/write)
- [ ] Ingestion pipeline (YouTube, articles, PDFs)
- [ ] Embedding generation (Bedrock Titan/Cohere)
- [ ] Backup scripts (rclone + GPG)
- [ ] Backup scheduling (launchd)
- [ ] Weekly verification scheduling
- [ ] Encryption key setup (Google + AWS)
- [ ] Integration testing
