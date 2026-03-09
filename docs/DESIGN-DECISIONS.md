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
