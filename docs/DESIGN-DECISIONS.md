# Design Decisions

Key architectural decisions and the reasoning behind them. Read this to understand WHY the system is built this way.

## Why PostgreSQL + pgvector (not a separate vector DB)

One database handles SQL queries, semantic search, JSONB metadata, and relationship graphs. No need to sync between a relational DB and a vector store. At personal scale (~50K memories), pgvector performance is more than sufficient.

## Why the hybrid chat extraction approach

Chat ingestion has two distinct phases with different cost profiles:
- **Phase 1 (stripping + filtering)** is deterministic — regex, string ops, size checks. No LLM needed. Runs free.
- **Phase 2 (chunking + metadata extraction)** requires understanding topic shifts and extracting structured data. LLM adds real value here.

Splitting them with a staging directory means Phase 1 runs unattended every night for free, and Phase 2 costs are controllable — you can process 50 chats or 5,000, on your schedule.

## Why one chat per Kiro session (Phase 2)

Even with Opus 4.6-1m's 1M token window, processing one chat per session ensures:
- Each chat gets full LLM attention (no degradation from accumulated context)
- Failures are isolated — one bad chat doesn't block others
- The wrapper script can retry individual failures
- Cost is predictable per chat

## Why Kiro headless for Phase 2 (not a Python script calling Bedrock directly)

Kiro CLI in `--no-interactive` mode IS the LLM integration layer. It handles auth, model selection, tool use, and MCP server connections. Writing our own LLM orchestration in Python would duplicate all of that. The tradeoff is Kiro credit cost vs development time — Kiro headless wins.

## Why Crawlee outputs markdown files (not database writes)

Clean separation of concerns. Crawlee is a content acquisition tool — it shouldn't know about PostgreSQL, embeddings, or the Second Brain schema. It outputs markdown to a directory. The ingestion pipeline reads from that directory. If either side changes, the other doesn't break.

## Why the Open Brain comparison matters

Nate B Jones' "Open Brain" system (YouTube: "You Don't Need SaaS") independently converged on the same core stack (PostgreSQL + pgvector + MCP). Comparing architectures revealed gaps in ours:
- **Multi-channel capture** — we only had terminal input, he had Slack. Led to the planned Capture API.
- **Metadata extraction** — he auto-extracts people, topics, action items. Led to the `metadata JSONB` column.
- **Memory migration** — he has prompts to pull from Claude/ChatGPT memory. Added to our roadmap.
- **Weekly synthesis** — periodic review that surfaces patterns. Added to our roadmap.

Our system is deeper (relationship graph, typed memories, dual-site backup). His is more accessible (low-friction capture). We're adding his accessibility to our depth.

## Why `metadata JSONB` instead of dedicated columns

We don't know yet what metadata will be most valuable. JSONB lets us store anything (people, topics, action items, sentiment, urgency) and query it without schema migrations. Six months from now, new metadata fields are just new keys — no ALTER TABLE needed.

## Why hybrid retrieval (BM25 + vector + RRF reranking)

Added 2026-03-09, inspired by the kiro-memory project (`~/KiromemoryMCP/mainline/`).

Pure vector search (pgvector cosine distance) misses exact keyword matches. If you search for "gridfinity baseplate" and a memory contains those exact words but the embedding doesn't rank it highly, you won't find it. This mirrors the dual-process model of human memory (Tulving, 1972): we recall things both by surface cues (a specific word) and by meaning (the gist). BM25 handles the first; vector embeddings handle the second.

Implementation: PostgreSQL's built-in `tsvector/tsquery` for BM25 (no external dependencies), fused with pgvector cosine search via Reciprocal Rank Fusion (RRF). A utility reranking layer then scores results by recency (exponential decay, τ=60 days), memory type boost (ideas/insights rank higher than raw sources), token overlap, content length, and retrieval reinforcement.

## Why retrieval reinforcement (access_count)

Every time a memory is returned by `memory_search`, its `access_count` is incremented. The reranking formula applies `0.03 * log(1 + access_count)` — a logarithmic boost so the first few retrievals matter most (diminishing returns).

This implements the testing effect (Roediger & Karpicke, 2006): retrieving information strengthens the memory trace more than re-reading it. Memories that keep proving useful naturally float to the top. Unused memories decay in relative ranking without being deleted.

## Why depth enforcement on memory_create

When the agent writes a knowledge-type memory (idea, synthesis, insight, decision), the MCP server checks for causal depth signals ("because", "when X then Y", "the fix was") and warns if the content is shallow. Also checks for a "Questions this answers:" section.

Inspired by kiro-memory's three-question framework (WHAT / WHAT HAPPENS when violated / WHY) and grounded in Craik & Lockhart's (1972) levels-of-processing research: information processed at deeper, more elaborative levels is retained and retrieved far more effectively. A bullet-point list of rules is shallow. A memory that explains WHY with a concrete failure example is deep and dramatically more retrievable.
