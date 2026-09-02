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

Even with Opus 4.8's 1M token window, processing one chat per session ensures:
- Each chat gets full LLM attention (no degradation from accumulated context)
- Failures are isolated — one bad chat doesn't block others
- The wrapper script can retry individual failures
- Cost is predictable per chat

## Why Kiro headless for Phase 2 (not a Python script calling Bedrock directly)

Kiro CLI in `--no-interactive` mode IS the LLM integration layer. It handles auth, model selection, tool use, and MCP server connections. Writing our own LLM orchestration in Python would duplicate all of that. The tradeoff is Kiro credit cost vs development time — Kiro headless wins.

## Why a pluggable model-backend resolver (and per-machine profiles)

The dream cycle was hard-wired to Kiro CLI. To run the brain on a machine without Kiro (the always-on Mac Mini) — and to leave the door open for Fable-5-on-the-Thinker and evaluator-panel diversity — the orchestrator now resolves an `Invoker` per role from a named profile in `config/backends.toml`, selected by `SECOND_BRAIN_PROFILE`. Constraints baked into the design: it is **default-preserving** (the `laptop` profile reproduces today's all-Kiro/Opus behavior exactly, so nothing changes until a profile is edited); a tool-less Direct-API backend (Bedrock) **cannot** run the agentic Explorer, enforced by an eager guard at construction; and the MVP is **one model per instance**, with per-role model selection and panel diversity deliberately deferred. See `docs/MODEL-BACKENDS.md`.

## Why Crawlee outputs markdown files (not database writes)

Clean separation of concerns. Crawlee is a content acquisition tool — it shouldn't know about PostgreSQL, embeddings, or the Second Brain schema. It outputs markdown to a directory. The ingestion pipeline reads from that directory. If either side changes, the other doesn't break.

> **Update (2026-06-08):** Still true for **web/article** capture. **YouTube moved in-repo** to a self-contained `yt-dlp` connector (`src/capture/youtube.py`) that no longer goes through Crawlee — see `CAPTURE-COMPONENTS.md`. The connector emits the same markdown-with-metadata-header contract, so the separation principle is preserved.

## Why the Open Brain comparison matters

Nate B Jones' "Open Brain" system (YouTube: "You Don't Need SaaS") independently converged on the same core stack (PostgreSQL + pgvector + MCP). Comparing architectures revealed gaps in ours:
- **Multi-channel capture** — we only had terminal input, he had Slack. Led to the planned Capture API.
- **Metadata extraction** — he auto-extracts people, topics, action items. Led to the `metadata JSONB` column.
- **Memory migration** — he has prompts to pull from Claude/ChatGPT memory. Added to our roadmap.
- **Weekly synthesis** — periodic review that surfaces patterns. Added to our roadmap.

Our system is deeper (relationship graph, typed memories, encrypted offsite + local backup). His is more accessible (low-friction capture). We're adding his accessibility to our depth.

## Why `metadata JSONB` instead of dedicated columns

We don't know yet what metadata will be most valuable. JSONB lets us store anything (people, topics, action items, sentiment, urgency) and query it without schema migrations. Six months from now, new metadata fields are just new keys — no ALTER TABLE needed.

## Why hybrid retrieval (BM25 + vector + RRF reranking)

Added 2026-03-09, inspired by the kiro-memory project (`~/KiromemoryMCP/mainline/`).

Pure vector search (pgvector cosine distance) misses exact keyword matches. If you search for "gridfinity baseplate" and a memory contains those exact words but the embedding doesn't rank it highly, you won't find it. This mirrors the dual-process model of human memory (Tulving, 1972): we recall things both by surface cues (a specific word) and by meaning (the gist). BM25 handles the first; vector embeddings handle the second.

Implementation: PostgreSQL's built-in `tsvector/tsquery` for BM25 (no external dependencies), fused with pgvector cosine search via Reciprocal Rank Fusion (RRF). A utility reranking layer then scores results by recency (power-law decay, `(1 + days/S)^-0.8`, where stability S grows with access count), memory type boost (ideas/insights rank higher than raw sources), token overlap, content length, and retrieval reinforcement.

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

The reranking base weights in `src/search.py` are `0.30·rrf_score + 0.18·token_overlap + 0.18·title_overlap + 0.10·context_overlap + 0.12·recency + 0.08·length + 0.05·depth_score`, which sum to ~1.01. V2 added `depth_score` (0.05) as the newest factor and lightly rebalanced the others from the V1 set (which summed to ~1.00); the change sharpens ranking without materially inflating the overall score. The boosts/penalties (type, mem_class, reinforcement, project) are additive on top. The specific weight choices were informed by the cognitive science research grounding each factor (see "Research Grounding for V2 Reranking Signals" below).

## Research Grounding for V2 Reranking Signals

Each V2 reranking signal is grounded in cognitive science research. Consolidated from the original V2 roadmap (Tulving 1972, Squire 1992, Roediger & Karpicke 2006, Howard & Kahana 2002, Craik & Lockhart 1972, Tulving & Thomson 1973, Dunlosky et al. 2013, Stickgold 2005, Walker 2009).

| Signal | Research | Why it matters |
|---|---|---|
| `mem_class` boost (semantic > procedural > episodic) | Tulving 1972, Squire 1992 — human long-term memory has three systems with different retrieval utility | A principle about idempotent migrations should outrank a raw session log that mentions migrations |
| Spaced retrieval (`spacing_bonus`) | Roediger & Karpicke 2006 (testing effect), Bjork 1975 (spacing effect) | Prevents popularity bias where a memory retrieved many times in one session dominates forever |
| Temporal contiguity (related context) | Howard & Kahana 2002 (temporal context model) | Memories encoded close in time are associated; retrieving one should surface temporal neighbors |
| Project scoping (`project` penalty) | Tulving & Thomson 1973 (encoding specificity) | Retrieval is most effective when retrieval context matches encoding context; cross-project results pollute |
| `depth_score` | Craik & Lockhart 1972 (levels of processing) | Deep encoding (causal explanations with examples) produces dramatically more retrievable memory traces |
| `memory_learn` tool | Dunlosky 2013 (elaborative interrogation) | Generating explanations for WHY creates richer retrieval paths; connecting to existing knowledge is one of the most effective learning strategies |
| Dream cycle consolidation | Stickgold 2005, Walker 2009 (sleep consolidation) | During sleep, the hippocampus replays episodic experiences and extracts hidden rules; agents with "learned context" need less reasoning at query time |
| Question-aware search (tsvector Weight A) | Dunlosky 2013 (elaborative interrogation) | "Questions this answers:" creates multiple retrieval paths to the same knowledge, matching how people actually search |

## Why three-speed enrichment (not uniform LLM enrichment)

Every serious memory product (Mem0, Cognee, Zep) runs LLM enrichment at ingest time. Our system has three speeds because volume and value differ:

- **Speed 1: Interactive `memory_create`** — Low volume (5-10 per session), high value. Gets full LLM enrichment: classification, depth scoring, contradiction check, relationship discovery.
- **Speed 2: Batch ingestion** — High volume (hundreds per batch), cost-prohibitive for LLM per item. Gets deterministic enrichment only: classify, depth score, project tag.
- **Speed 3: Dream cycle** — Weekly deep LLM enrichment re-examines everything, including batch-ingested memories that only got deterministic enrichment. The "slow path" that catches what the fast paths missed.

## Why memory lifecycle states (active → superseded → consolidated → archived)

Memories have lifecycle states with moderate (not heavy) demotion for superseded memories. Bjork's storage strength vs. retrieval strength (1992) shows that memories with high storage strength but low retrieval strength are the most valuable candidates for re-engagement. Heavy demotion would make them invisible to the Explorer's desirable difficulty strategy.

CLS theory (McClelland 1995/2016) says the hippocampus retains episodes even after neocortical integration, for future replay. Consolidated memories remain searchable but don't compete with their distilled semantic successors. Zep invalidates but never deletes. User rejection is stored as meta-cognitive information (Bartlett's schema theory), not deletion.

## Why the dream cycle uses a four-agent pipeline

The dream cycle implements Stickgold (2005) and Walker (2009): background processing during idle time that replays episodic experiences, extracts hidden rules, and finds connections between unrelated memories. Letta's sleep-time compute research (2025) operationalized this for AI.

The pipeline is Explorer → Thinker → 4-evaluator BFT Consensus Panel. Each agent runs as a separate `kiro-cli chat --no-interactive` invocation for context isolation and failure isolation. See `.kiro/specs/dream-cycle/` for implementation details and `.kiro/specs/byzantine-consensus-panel/` for the consensus design.

## Product landscape convergence

Every serious memory system has converged on five operations:

| Operation | Cognee | Mem0 | Zep | Letta | Google | Ours |
|---|---|---|---|---|---|---|
| Ingest | add+cognify | Extraction | Episode ingest | Message proc | IngestAgent | ingest_content |
| Enrich | cognify | Entity extract | Entity+rel | Core self-edit | IngestAgent | Three-speed |
| Consolidate | memify | ADD/UPDATE/DELETE | Contradiction detect | Archival promote | ConsolidateAgent | Dream cycle |
| Retrieve | search | Relevance | Temporal+semantic | Recall+archival | QueryAgent | hybrid_search+rerank |
| Reinforce | Usage-weighted | Implicit | Temporal recency | Self-editing | Consolidated | access_count+spacing |

## Dream cycle research grounding

| Design Element | Research Basis |
|---|---|
| Background processing during idle time | Stickgold 2005, Walker 2009 |
| Novel recombination of memories | Stickgold 2005 (hippocampal replay) |
| Multi-agent consensus reduces hallucination | Yao et al. 2025 v2 (Roundtable Policy) |
| Binary BFT consensus (≥3/4) over unanimity | Lamport 1982, Castro & Liskov 1999 |
| Binary verdicts over weighted voting | Prasad & Nguyen 2025 (LLM confidence miscalibration) |
| Reflection as first-class operation | Park 2023 (Generative Agents), Shinn 2023 (Reflexion) |
| Two-system learning (fast + slow) | McClelland et al. 1995/2016 (CLS theory) |
| Desirable difficulty surfacing | Bjork 1992 (storage vs retrieval strength) |
| No confidence-based digest ordering | Prasad & Nguyen 2025 (72.9% avg confidence vs 50% rational baseline) |

## References

[1] Tulving 1972 — Episodic and Semantic Memory
[2] Squire 1992 — Memory and the Hippocampus
[3] Craik & Lockhart 1972 — Levels of Processing Framework
[4] Roediger & Karpicke 2006 — Test-Enhanced Learning
[5] Howard & Kahana 2002 — Temporal Context Model
[6] Tulving & Thomson 1973 — Encoding Specificity Principle
[7] Dunlosky et al. 2013 — Improving Students' Learning with Effective Learning Techniques
[8] Bjork 1975/1992 — Spacing Effect / Storage vs Retrieval Strength
[9] Stickgold 2005 — Sleep-dependent memory consolidation
[10] Walker 2009 — Sleep-Dependent Memory Processing
[11] McClelland et al. 1995/2016 — Complementary Learning Systems
[12] Bartlett 1932 — Schema Theory / Reconstructive Memory
[13] Shinn et al. 2023 — Reflexion: Verbal Reinforcement Learning
[14] Park et al. 2023 — Generative Agents: Interactive Simulacra of Human Behavior
[15] Lamport, Shostak, and Pease 1982 — The Byzantine Generals Problem
[16] Castro and Liskov 1999 — Practical Byzantine Fault Tolerance
[17] Yao et al. 2025 v2 — Confidence-Weighted-Consensus Aggregation
[18] Prasad & Nguyen 2025 — When Two LLMs Debate, Both Think They'll Win
[19] Letta 2025 — Sleep-time Compute
[20] Mem0 — arxiv 2504.19413
[21] Zep/Graphiti — arxiv 2501.13956
[22] Cognee — memify pipeline (topoteretes.com)

## Why power-law decay instead of exponential (recency signal)

Added 2026-03-28. Replaced `exp(-days/60)` with `(1 + days/S)^(-b)`.

The original exponential decay (τ=60 days) was a reasonable approximation but doesn't match how human memory actually decays. Ebbinghaus (1885) and Murre & Dros (2015, replication) showed that forgetting follows a power law, not an exponential. The key difference: exponential decay drops too fast initially and too slow later. Power-law decay has a long tail — old memories don't vanish, they just become harder to retrieve.

The stability parameter S is derived from `access_count`: `S = 30 + 10 * log1p(access_count)`. This connects the spacing effect (Bjork 1975) directly to the forgetting curve — memories that have been retrieved more often decay more slowly because each retrieval strengthens the trace. A memory retrieved 10 times has S ≈ 54 days; a never-retrieved memory has S = 30 days.

## Why active forgetting (staleness penalty)

Added 2026-03-28. Anderson & Neely (1996): interference theory.

Memories with `access_count = 0` and `created_at` older than 90 days receive a mild penalty that scales with age: `-0.05 * min(1.0, (days_unretrieved - 90) / 180)`. Maximum penalty is -0.05 at 270+ days.

The reasoning: if a memory was never useful enough to retrieve in 90 days, it's likely noise. The penalty is deliberately mild — it doesn't delete anything, just deprioritizes. This implements the "use it or lose it" principle from interference theory: unretrieved memories should fade in relevance, not persist at full strength forever.

## Why encoding context (contextual reinstatement)

Added 2026-03-28. Godden & Baddeley (1975): context-dependent memory.

The `encoding_context` column captures what the user was working on when a memory was created — "debugging auth flow", "reading about CLS theory", "planning dream cycle architecture". At search time, token overlap between the query and the encoding context is a reranking signal (weight 0.10).

This is one of the most replicated findings in cognitive psychology: retrieval is dramatically better when the retrieval context matches the encoding context. Divers who learned word lists underwater recalled them better underwater than on land, and vice versa. The same principle applies to knowledge retrieval — if you were debugging auth when you learned something, searching while debugging auth should boost that memory.

## Why supersession penalty (retroactive interference)

Added 2026-03-28. Anderson & Neely (1996): retroactive interference.

Memories with `status = 'superseded'` receive a -0.20 reranking penalty. The `superseded_by` relationship and status field existed before this change, but nothing in the retrieval path used them. Now superseded memories are actively deprioritized in favor of their replacements.

## Why UCB1 strategy diversity in the dream cycle

Added 2026-03-28. Auer, Cesa-Bianchi & Fischer (2002): multi-armed bandits.

The Explorer agent has 11 named strategies for assembling memory slices. Without diversity pressure, it tends to converge on the same 3-4 strategies that produce the most obvious results. UCB1 (Upper Confidence Bound) adds an exploration bonus to underused strategies: `bonus = C * sqrt(ln(N) / n_i)` where N is total strategy uses and n_i is uses of strategy i.

This is injected into the Explorer prompt as a "strategy diversity pressure" section showing usage counts and exploration bonuses. Strategies never used get a ★ UNEXPLORED label. The Explorer is instructed to prioritize high-bonus strategies.

## Why elaborative re-interrogation (strategy #11)

Added 2026-03-28. Pressley, McDaniel, Turnure, Wood & Ahmad (1987).

The original dream cycle only created new insights. Strategy #11 (ELABORATIVE RE-INTERROGATION) finds high-value existing memories that haven't been updated in 30+ days and assembles them with their newer semantic neighbors. The Thinker's job for these slices is to UPDATE existing memories with new context, not create new ones.

The research shows that repeated elaborative interrogation at increasing intervals produces far stronger encoding than one-shot elaboration. The `memory_learn` tool does one-shot elaboration; this strategy adds the "repeated at intervals" component.

## Why relationship decay (expired_at)

Added 2026-03-28. The `expired_at` column on `memory_relationships` existed but was never set. Now, after each dream cycle run, relationships where neither endpoint has been accessed in 90+ days are marked expired. This prevents stale relationships from cluttering the knowledge graph. A "contradicts" relationship from 6 months ago between two memories nobody looks at is noise, not signal.

## Why schema-type memories (two-level retrieval)

> **Status (2026-06-08): INERT.** This describes the *intended* design. In practice the write path
> was never completed — there are **0 `type='schema'` memories** in the DB, so the `schema_context`
> field always returns empty. Migration 007 added supporting partial indexes (NOT a `schema_type`
> column). Rationale retained as design history; the feature is a candidate for completion or removal.

Added 2026-03-28. Bartlett (1932): schema theory; Piaget: assimilation/accommodation.

Schema memories (type='schema') are abstract structures that group related principles, decisions, and insights. Instead of returning a flat list of 10 individual memories, search can now surface "here's the principle, and here are the specific memories that support it."

Schemas are created by the dream cycle when the Thinker identifies 3+ memories sharing an unnamed principle. They use `derived_from` relationships to link to their constituent memories. The search results include a `schema_context` field that surfaces relevant schemas alongside individual results.
