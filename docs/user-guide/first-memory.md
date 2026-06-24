---
title: "Your First Memory"
type: tutorial
---

# Your First Memory

Create, search, read, update, and relate a memory — the full lifecycle — using the MCP tools through your connected agent.

## Prerequisites

> [!NOTE]
> Before you begin, ensure you have:
> - Completed [Getting started](getting-started.md) (PostgreSQL running, MCP server connected)
> - An agent (Kiro CLI or Claude Code) connected to the Second Brain MCP server — see [Connect an AI agent](connect-ai-agent.md)
> - A valid AWS SSO session (`aws sso login --profile default`)

## 1. Create a memory

Ask your agent:

> "Create a memory of type `decision` titled 'Chose pgvector HNSW over IVFFlat' with this content:
>
> We chose HNSW because it maintains recall above 0.95 at our current scale without periodic retraining. IVFFlat requires rebuilding cluster centroids as data grows; when centroids go stale, recall drops silently.
>
> When the index exceeds 500K vectors, re-evaluate — HNSW memory usage grows linearly.
>
> Questions this answers:
> - Why did we pick HNSW for the vector index?
> - When should we reconsider IVFFlat?"

Expected result — the agent confirms creation and returns the memory ID:

```text
Created memory abc12345-... (type: decision, depth_score: 0.72)
```

> [!TIP]
> A good *memory* includes causal depth ("because…", "when X then Y") and a "Questions this answers:" section. Without these, `memory_create` returns a depth warning prompting you to rewrite.

## 2. Search for the memory

Ask your agent:

> "Search for memories about 'HNSW vs IVFFlat'."

The agent calls `memory_search` and returns ranked results:

```text
Results (1 match):
1. [decision] Chose pgvector HNSW over IVFFlat
   Score: 0.89 | Created: 2026-06-16
   Preview: We chose HNSW because it maintains recall above 0.95...
```

The *hybrid semantic + keyword search* with reranking found your memory by meaning, not just exact keywords.

## 3. Read the full memory

Search results show a truncated preview. To see everything, ask your agent:

> "Read memory `<id>` in full."

The agent calls `memory_read` and returns the complete record:

```text
ID: abc12345-...
Type: decision
Title: Chose pgvector HNSW over IVFFlat
Content: We chose HNSW because it maintains recall above 0.95...
Status: active
Depth score: 0.72
Created: 2026-06-16T07:50:00Z
Tags: []
```

## 4. Update the memory

You realize you should tag it. Ask your agent:

> "Update memory `<id>` — add tags `pgvector` and `infrastructure`."

The agent calls `memory_update` with only the changed field:

```text
Updated memory abc12345-... (tags: [pgvector, infrastructure])
```

> [!NOTE]
> `memory_update` accepts only the fields you want to change. Unmentioned fields stay as they are.

## 5. Relate it to another memory

First, create a second memory to relate to:

> "Create a memory of type `research` titled 'pgvector index benchmarks' with content:
>
> At 120K vectors (1024-dim), HNSW query p99 is 4ms. IVFFlat with fresh centroids achieves 2ms but degrades to 12ms after 30 days without retraining.
>
> Questions this answers:
> - What is the p99 latency for our vector index?
> - How fast does IVFFlat degrade without retraining?"

Now relate them:

> "Relate memory `<first-id>` to memory `<second-id>` with relationship type `supports` and note 'Benchmark data that justified the HNSW decision'."

The agent calls `memory_relate`:

```text
Created relationship: <first-id> --supports--> <second-id>
Note: Benchmark data that justified the HNSW decision
```

A *relationship* is a typed, directed edge between two memories. The `supports` type means the second memory provides evidence for the first. See [Reference](reference.md) for all relationship types.

## What you learned

In this tutorial you:

1. **Created** a memory with causal depth and a "Questions this answers:" section.
2. **Searched** using natural language — the hybrid reranker matched by meaning.
3. **Read** the full content and metadata of a memory.
4. **Updated** a memory's tags without touching other fields.
5. **Related** two memories with a typed, directed edge.

These five operations — `memory_create`, `memory_search`, `memory_read`, `memory_update`, `memory_relate` — form the core interaction loop. The remaining tools (`memory_list`, `memory_graph`, `memory_learn`, `memory_brief`) build on this foundation.

## Next steps

- [Using Second Brain](using-second-brain.md) — daily workflows, the depth principle, and Express briefings
- [Reference](reference.md) — full tool arguments, memory types, and relationship types
- [Glossary](glossary.md) — definitions of project terms
