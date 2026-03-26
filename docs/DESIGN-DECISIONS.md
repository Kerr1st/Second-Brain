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

## Why retrieval reinforcement (access_count + spaced retrieval)

Every time a memory is returned by `memory_search`, its `access_count` is incremented and `last_accessed_at` is updated. The reranking formula applies `0.03 * log(1 + access_count) * spacing_bonus` — a logarithmic boost modulated by how long ago the memory was last accessed.

`spacing_bonus = min(1.0, days_since_last_access / 7.0)` means a re-retrieval within the same day adds almost no boost, but a retrieval a week later adds full boost. This prevents "popularity bias" where a memory retrieved many times in one session dominates rankings forever.

This implements the testing effect (Roediger & Karpicke, 2006): retrieving information strengthens the memory trace more than re-reading it. The spacing bonus implements the spacing effect (Bjork, 1975): equally spaced retrieval produces superior long-term retention vs. massed retrieval.

## Why depth enforcement on memory_create

When the agent writes a knowledge-type memory (idea, synthesis, insight, decision), the MCP server checks for causal depth signals ("because", "when X then Y", "the fix was") and warns if the content is shallow. Also checks for a "Questions this answers:" section.

Inspired by kiro-memory's three-question framework (WHAT / WHAT HAPPENS when violated / WHY) and grounded in Craik & Lockhart's (1972) levels-of-processing research: information processed at deeper, more elaborative levels is retained and retrieved far more effectively. A bullet-point list of rules is shallow. A memory that explains WHY with a concrete failure example is deep and dramatically more retrievable.

## Why 4 evaluators with binary BFT consensus (not 3 with DEFERRED)

The original dream cycle used 3 evaluators with a three-state model: 3/3 ACCEPT → ACCEPTED, 2/3 → DEFERRED, else REJECTED. DEFERRED candidates re-entered the next cycle with the dissenting objection as context.

This was upgraded to 4 evaluators with binary consensus based on Lamport, Shostak, and Pease (1982): tolerating f faulty (hallucinating) nodes requires 3f+1 total nodes. With f=1, the panel needs 4 evaluators. The PBFT quorum (Castro & Liskov 1999) of 2f+1 = 3 out of 4 is the acceptance threshold.

The DEFERRED state was removed because with 4 evaluators, the "one dissenter" case (3/4) now meets the BFT quorum and is ACCEPTED directly. The 2/4 case is ambiguous under BFT assumptions and is REJECTED. Dissenting reasoning on 3/4 accepts is preserved via digest annotations and feedback injection.

## Why search.py was extracted from db.py

`db.py` had grown to 342 lines mixing three concerns: data access (CRUD), retrieval algorithms (hybrid search with RRF fusion), and business logic (reranking with scoring formulas). The extraction into `src/search.py` follows a three-layer architecture: Presentation (`mcp_server.py`), Service (`search.py`), Data Access (`db.py`). This makes the reranking formula independently testable and keeps `db.py` focused on pure CRUD operations.

## Why V2 reranking weights were rebalanced

The V1 formula weights summed to 1.00 (0.35 + 0.20 + 0.20 + 0.15 + 0.10). V2 added `depth_score` as a new 0.05 factor, requiring the base weights to be reduced slightly (sum from 1.00 to 0.86) to keep the total in a similar range. The boosts/penalties (type, mem_class, reinforcement, project) are additive on top. The specific weight choices were informed by the cognitive science research grounding each factor — see `docs/V2-ROADMAP.md`.
