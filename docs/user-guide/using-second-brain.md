---
title: "Using Second Brain"
type: how-to
---

# Using Second Brain

Day-to-day tasks: creating memories, searching, linking, reading synthesis output, and shaping what the system surfaces.

## How memories arrive

Most capture is automated by [scheduled jobs](operations.md) — chat extraction, YouTube transcripts, Quick Desktop sync, and web scrapes run on a schedule and feed the ingestion pipeline. You rarely need to create memories manually.

In-session, an agent (Kiro CLI or Claude Code) creates memories on your behalf via the `memory_create` MCP tool when you ask it to remember something or when `memory_learn` synthesizes external content.

## Write a good memory

The system rewards *depth*. Shallow notes ("X is interesting") score low and rank poorly in future retrieval. The depth scorer and reranker boost memories that explain causation.

### The depth principle

Include:

1. **Causal "because"** — state *why* something matters, not just what it is.
2. **Conditional "when X then Y"** — describe when the insight applies.
3. **A `Questions this answers:` section** — 3–5 natural-language queries someone might ask that this memory should surface for.

### Why retrieval rewards depth

The reranking formula includes a `depth_score` component and a `type_boost` for ideas/insights/decisions. A memory with causal reasoning and explicit questions:

- Scores higher on `token_overlap` when someone asks one of those questions.
- Gets the `depth_score` bonus (computed at creation).
- Accumulates `access_count` (retrieval reinforcement) because it's useful.

### Example

```text
## Decision: Use pgvector HNSW over IVFFlat

We chose HNSW because it maintains recall >0.95 at our scale (~120K vectors)
without periodic re-training. IVFFlat requires rebuilding cluster centroids as
data grows; when the index goes stale, recall drops silently.

When you need sub-10ms latency on >500K vectors, re-evaluate — HNSW memory
usage grows linearly and may exceed available RAM.

Questions this answers:
- Why did we choose HNSW for the vector index?
- When should we reconsider IVFFlat?
- What happens if the vector index isn't retrained?
```

> [!TIP]
> If you create a memory of type `idea`, `insight`, `synthesis`, or `decision` without causal reasoning, the tool returns a depth warning. Heed it — rewrite before moving on.

## Search effectively

Use `memory_search` with filters to narrow results:

| Filter | What it does | Example value |
|--------|-------------|---------------|
| `type` | Memory type | `decision`, `insight` |
| `project` | Scope to a project tag | `second-brain` |
| `source_type` | Capture channel | `kiro_cli_chat`, `article` |
| `since_days` | Only recent memories | `30` (last month) |
| `status` | Exclude superseded | `active` |

### Tips

- Start broad, then add filters. If hits are thin, drop filters or rephrase the query.
- Call `memory_read(id)` to expand a truncated preview (search returns 500 chars max).
- Call `memory_graph(id)` to follow relationships from a hit to related memories.

## Link memories

Use `memory_relate` to create typed edges between memories. The system also auto-discovers relationships during ingestion.

| Relationship | Use when… |
|---|---|
| `supports` | A provides evidence for B |
| `contradicts` | A conflicts with B |
| `extends` | A builds on B |
| `inspires` | A sparked B |
| `blocks` | A is a blocker for B |
| `requires` | A depends on B |
| `derived_from` | A was synthesized from B |
| `related_to` | Semantic/temporal neighbor |
| `superseded_by` | A is replaced by B |

```text
memory_relate(source_id="<id-A>", target_id="<id-B>", relation_type="extends",
              note="Adds the latency caveat missing from the original")
```

## Read the dream-cycle digest

The *dream cycle* — the nightly autonomous synthesis pipeline — writes a digest after each run:

```bash
ls logs/dream-cycle-digest-*.md | tail -5
cat logs/dream-cycle-digest-2026-06-15.md
```

Each digest lists accepted insights, contradictions detected, and rejected candidates with evaluator reasoning.

## Use Express — the `brief` CLI

*Express* is the delivery layer that pushes synthesis to you. The `brief` CLI is the pull surface.

### Read your briefing

```bash
.venv/bin/python scripts/brief.py              # LLM-edited headlines
.venv/bin/python scripts/brief.py --no-llm     # fast deterministic ranking
.venv/bin/python scripts/brief.py --window-days 30  # wider window (default 14)
```

### Shape what it surfaces

Feedback is a gradient — you can boost, down-weight, or hard-hide items:

```bash
.venv/bin/python scripts/brief.py --useful <target>   # boost
.venv/bin/python scripts/brief.py --less   <target>   # soft down-weight
.venv/bin/python scripts/brief.py --mute   <target>   # hard hide
.venv/bin/python scripts/brief.py --unmute <target>   # clear a preference
.venv/bin/python scripts/brief.py --prefs             # list preferences
```

`<target>` is one of:

- An item id — the `#abcd1234` shown next to each briefing item
- A kind — `insight` | `contradiction` | `resurface` | `digest` | `question`
- A topic or project name

### In-session: `memory_brief`

Call `memory_brief` at the start of a work session. The agent receives recent insights, contradictions, resurfaced high-value memories, and open questions — connections you'd otherwise have to search for.

See [reference.md](reference.md) for full argument lists and return shapes.

## Related

- [Reference](reference.md) — complete tool, CLI, and type tables
- [Operations](operations.md) — scheduled jobs, backup, monitoring
- [Getting started](getting-started.md) — initial setup
