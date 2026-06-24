# Quick Desktop Integration — Design Decisions & Learnings

**Date:** 2026-04-21
**Branch:** `quick_desktop_integration`

> **⚠️ STATUS (2026-06-08):** This documents the original QD integration, including the **entity
> knowledge graph**. The KG (`entities`/`entity_edges`/`memory_entities`) was imported but is
> **currently DORMANT — unused by retrieval, synthesis, or Express** (0 `src/` references; ~99.5%
> disconnected from memories). See `AGENTIC-RETRIEVAL-PLAN.md` (KG deferred) and `ARCHITECTURE.md`.
> The QD *memory / doc / chat / feed* sync still runs; the **KG sections below are historical**.
> Entity counts cited here predate later imports and conflict with other docs — treat as
> illustrative, not current.

## Context

Quick Desktop (QD) is an AI desktop assistant that monitors Slack, Outlook, and calendar, extracting knowledge into a local SQLite database (`~/.quickwork/knowledge_storage/knowledge_v1.db`). After one week of use, it had accumulated:

- **815 long-term memories** — facts and procedures with Bayesian confidence scoring
- **2,105 knowledge graph entities** — people, projects, organizations, products, decisions
- **4,559 edges** — typed relationships (worksFor, dependsOn, isPartOf, attended, etc.)
- **5,938 session directories** — conversations and scheduled task outputs

The goal: integrate QD's knowledge into the Second Brain as a capture channel, following the "Storage Converges, Capture Diverges" principle identified in the March 19 dream cycle.

## Key Design Decisions

### 1. One-time backfill + incremental sync (not just one-time)

QD runs `scheduled-memory-extraction` every few minutes and `scheduled-kg-enrichment` every 2 hours. New knowledge is constantly being created. A one-time import would diverge within days.

**Decision:** Build both a `--full` backfill mode and an incremental sync mode that tracks the last processed ID in `~/.quickwork/.second_brain_sync_state.json`. Hourly LaunchAgent runs the incremental sync.

### 2. Import memories directly via `create_memory()`, not through the ingestion pipeline

QD's memories are already distilled knowledge (e.g., "Gyan Singh is Kerr's manager at AWS CSS"). They don't need parsing, chunking, or the chat-extraction pipeline. They map directly to Second Brain memory types.

**Category mapping:**
| QD Category | Second Brain Type | Rationale |
|---|---|---|
| people (583) | source | Reference data about colleagues/contacts |
| terminology (122) | research | Domain knowledge definitions |
| source (53) | source | Source references |
| tool-strategy (21) | insight | Operational patterns |
| preference (11) | insight | User preferences as design constraints |
| anti-pattern (10) | insight | Things to avoid |
| profile (5) | source | User profile data |
| procedure (10) | insight | How-to knowledge |

### 3. Why we added a knowledge graph to Second Brain

This was the most debated decision. QD runs two complementary knowledge stores:

**Memory Store** — Flat facts with Bayesian confidence. "Gyan Singh is Kerr's manager." Retrieved by similarity search. Good for: "What do I know about X?"

**Knowledge Graph** — Structured relationships. Gyan Singh → `worksFor` → CSS COE → `hasPart` → Playbook Update Project. Retrieved by traversal. Good for: "Who's involved in X?" and "What depends on X?"

These serve fundamentally different retrieval patterns:

| Query Type | Memory Store | Knowledge Graph |
|---|---|---|
| "What do I know about Gyan?" | ✅ Returns the fact | ✅ Returns entity + all edges |
| "Who attended the Big Rocks meeting?" | ❌ Only if someone wrote that fact | ✅ Traverses `attended` edges |
| "What projects depend on the firewall fix?" | ❌ Keyword match at best | ✅ Traverses `dependsOn` edges |
| "What was I thinking about CLS theory?" | ✅ Semantic search finds it | ❌ Not structured enough |

**The dream cycle connection:** The Explorer agent's job is to find non-obvious connections across the memory space. With only vector search, it can find textually similar memories. With the graph, it can find memories about the *same entities* even when the text is completely different. Two memories might share zero keywords but both mention the same person, project, and organization — the graph reveals that.

**Decision:** Add three tables to PostgreSQL:
- `entities` — graph nodes (2,110 imported)
- `entity_edges` — typed directed relationships (4,596 imported)
- `memory_entities` — bridge linking memories to entities they reference (1,700 links)

### 4. Why not a separate graph database?

Considered Neo4j, Neptune, and Apache AGE. Rejected for now because:

- At 2,110 nodes and 4,596 edges, PostgreSQL handles graph queries in **0.4ms** per traversal
- Projected growth: ~110K entities/year at current rate. PostgreSQL handles millions.
- A separate database means syncing two systems — exactly the complexity we questioned about QD's architecture
- The schema maps 1:1 to any graph DB if we ever need to migrate (Apache AGE is a `CREATE EXTENSION` away)

### 5. Why QD uses two separate stores (memories vs. entities)

Initially puzzling — why not one table? The answer is different **write patterns**:

- **Memories** are mutable and probabilistic. They have Bayesian confidence (alpha/beta), get reinforced or weakened over time, track retrieval success/failure. This is a learning system.
- **Entities** are factual and structural. "Acme is an Organization" isn't probabilistic. Entities have edges, not confidence scores.

For Second Brain, we kept the separation: memories remain the primary store for behavioral knowledge, entities are additive structural knowledge. The `memory_entities` bridge table connects them.

### 6. What we chose NOT to import

- **Sessions/conversations** (319 MB) — Already have 74K chat memories, don't need raw conversations
- **Memory traces** (1,141 dirs) — Retrieval logs, not knowledge
- **Events/Meetings** (171 entities) — Ephemeral calendar data
- **Actions** (94 entities) — Task tracking, not persistent knowledge
- **Channel entities** (184) — Slack channel references, low value

## Architecture

```
Quick Desktop (always running)
├── Slack monitor (every 15 min)
├── Outlook monitor (every 15 min)
├── Memory extraction (every ~3 min)
├── KG enrichment (every ~2 hours)
└── knowledge_v1.db
     ├── memories (815 facts/procedures)
     └── entities + edges (2,105 + 4,559)
            │
            ▼ (hourly sync via LaunchAgent)
Second Brain PostgreSQL
├── memories table (74K + 862 from QD)
├── entities table (2,110 from QD)      ← NEW
├── entity_edges table (4,596 from QD)  ← NEW
└── memory_entities bridge (1,700)      ← NEW
```

## Files Added

| File | Purpose |
|---|---|
| `migrations/008_knowledge_graph.sql` | Schema for entities, entity_edges, memory_entities |
| `scripts/migrate/migrate_quick_desktop.py` | Backfill + incremental sync script |
| `scripts/jobs/qd_sync.sh` | Shell wrapper for LaunchAgent |
| `scheduling/com.second-brain.qd-sync.plist` | Hourly LaunchAgent |
| `tests/test_qd_migration.py` | 12 tests covering mapping, filtering, sync state |
| `docs/QUICK-DESKTOP-INTEGRATION.md` | This document |

## Performance Validation

| Operation | Result |
|---|---|
| 1-hop graph traversal | 0.4ms |
| 2-hop recursive traversal | <2ms |
| Bridge query (memories via graph) | <5ms |
| Full backfill (862 memories + 2,110 entities + 4,596 edges) | ~4 seconds |
| Incremental sync (0 new items) | <1 second |
| Existing test suite | 321 passed, 1 pre-existing SSO failure |

## Future Work

1. **Dream cycle enhancement** — Teach the Explorer to use graph traversal alongside vector search. New strategies: "find memories about entities connected to X within N hops."
2. **Entity extraction on ingest** — When new memories are created (from any source), extract entities and populate the bridge table automatically.
3. **Apache AGE** — If graph queries ever become slow at scale, add the PostgreSQL graph extension for native Cypher queries. No migration needed.
4. **Bidirectional sync** — Currently one-way (QD → Second Brain). Could push Second Brain insights back to QD's KG.
