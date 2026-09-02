---
title: "How Memory Works"
type: explanation
---

# How Memory Works

This page explains how Second Brain stores and retrieves your memories — and why each design choice exists. You will understand the retrieval pipeline from embedding to final ranked result, grounded in the cognitive science research that shaped it.

> [!NOTE]
> This page covers the **why** of storage and retrieval internals. For the database column/index schema, see [Database Schema](database-schema.md). For hands-on search usage, see [Using Second Brain](using-second-brain.md) and [Reference](reference.md).

## From text to meaning: embeddings

When you store a memory, Second Brain converts its text into a 1,024-dimensional vector using local
Ollama BGE-M3. The vector positions semantically similar texts close together in one named active
space. Preserved Titan vectors occupy a separate legacy column and are never compared with BGE-M3.

This matters because keyword matching fails when you phrase a question differently from how you originally wrote the answer. The query "how do I prevent duplicate records?" should surface a memory titled "idempotent migration strategy" even though no words overlap. Vectors make this possible: both texts occupy nearby coordinates because they encode similar *meaning*.

The 1024-dimensional space is stored in PostgreSQL via pgvector, indexed with HNSW (Hierarchical Navigable Small World) for sub-linear approximate nearest-neighbor search. At personal scale (~121K memories), this delivers single-digit-millisecond vector lookups without a separate vector database.

## Why hybrid retrieval beats either approach alone

Pure vector search captures meaning but misses exact keywords. If a memory contains "gridfinity baseplate" and you search those exact words, the embedding may not rank it first because the model weighs the broader semantic neighbourhood. Conversely, pure keyword search (BM25) finds exact terms but misses paraphrases entirely.

This mirrors Tulving's (1972) dual-process model of human memory: you recall things both by *surface cues* (a specific word, a name) and by *meaning* (the gist, the concept). BM25 handles the first; vector embeddings handle the second. Using both channels in parallel — the way your own memory does — produces dramatically better recall than either alone.

### Implementation

Second Brain runs two searches in parallel against the same PostgreSQL database:

1. **BM25 full-text** — PostgreSQL's native `tsvector/tsquery` with a GIN index. Fast, exact, zero external dependencies.
2. **Vector cosine similarity** — pgvector's HNSW index over active 1,024-dimension BGE-M3 embeddings.

Each search returns a ranked list of candidates. The two lists are fused into a single ranking using *Reciprocal Rank Fusion*.

### Reciprocal Rank Fusion (k=60)

RRF combines ranked lists without requiring comparable scores. For each candidate memory:

```
rrf_score = 1/(k + rank_vector) + 1/(k + rank_bm25)
```

The constant k=60 dampens the influence of top-ranked results so that a memory appearing in both lists (even at moderate ranks) outscores one that appears only in a single list at rank 1. This is the key insight: agreement between retrieval channels is a stronger signal than dominance in one channel.

A memory absent from one list receives a penalty rank (prefetch_limit + 1), ensuring it can still appear in results — but won't outrank a memory that both channels agree on.

## Cognitive-science reranking

After RRF fusion, a *utility reranker* rescores every candidate using signals grounded in memory research. The formula:

```
rerank_score = 0.30 × rrf_score
             + 0.18 × token_overlap
             + 0.18 × title_overlap
             + 0.10 × context_overlap
             + 0.12 × recency
             + 0.08 × length_score
             + 0.05 × depth_score
             + type_boost
             + mem_class_boost
             + reinforcement
             + project_penalty
             + superseded_penalty
             + staleness_penalty
```

### Signal breakdown

| Signal | Weight / Value | Rationale | Research basis |
|--------|---------------|-----------|----------------|
| RRF score | 0.30 | Dual-channel agreement is the strongest relevance signal | Tulving 1972 (dual-process recall) |
| Token overlap | 0.18 | Direct lexical match between query and content | Surface-cue retrieval path |
| Title overlap | 0.18 | Titles are compressed summaries; high overlap = strong match | — |
| Context overlap | 0.10 | Match between query and the *encoding context* at creation time | Godden & Baddeley 1975 (context-dependent memory) |
| Recency | 0.12 | Power-law decay: `(1 + days/S)^(-0.8)` where stability S grows with access count | Ebbinghaus 1885; Murre & Dros 2015 |
| Length score | 0.08 | Longer content has more substance (capped at 80 tokens) | — |
| Depth score | 0.05 | Causal explanations with examples are more retrievable than shallow bullet lists | Craik & Lockhart 1972 (levels of processing) |
| Type boost | +0.06 | Ideas, syntheses, insights, and decisions outrank raw sources | Knowledge hierarchy |
| mem_class boost | +0.04 / +0.02 / +0.00 | Semantic > procedural > episodic retrieval utility | Tulving 1972; Squire 1992 |
| Reinforcement | +0.03 × log₁₊₁(access) × spacing | Previously retrieved memories are stronger traces | Roediger & Karpicke 2006 (testing effect); Bjork 1975 (spacing effect) |
| Project penalty | −0.15 | Cross-project results are deprioritized (not excluded) | Tulving & Thomson 1973 (encoding specificity) |
| Superseded penalty | −0.20 | Replaced memories fade in favour of their successors | Anderson & Neely 1996 (retroactive interference) |
| Staleness penalty | −0.05 | Memories never retrieved in 90+ days fade mildly: `−0.05 × min(1, (days−90)/180)` | Anderson & Neely 1996 (interference theory / "use it or lose it") |

> [!TIP]
> The base weights (0.30 + 0.18 + 0.18 + 0.10 + 0.12 + 0.08 + 0.05 = 1.01) form the core score. Boosts and penalties are additive modifiers on top — they shift results up or down without dominating.

### Why these specific weights?

V2 added the `depth_score` signal (0.05) and lightly rebalanced the other factors, bringing the base weights to a sum of ~1.01 — essentially unchanged from V1's ~1.00, so the new signal sharpens ranking without inflating the overall score. Each weight reflects how strongly the corresponding cognitive mechanism predicts retrieval success in practice. RRF dominates because dual-channel agreement is the single best relevance indicator; token and title overlap together (0.36) represent the keyword path of dual-process recall; context overlap, recency, and depth together (0.27) represent the elaborative/contextual path.

## Retrieval reinforcement and the spacing effect

Every time `memory_search` returns a memory, two things happen:

1. `access_count` is incremented.
2. `last_accessed_at` is updated to now.

On subsequent searches, the reinforcement signal applies:

```
reinforcement = 0.03 × log₁₊₁(access_count) × spacing_bonus
```

The **logarithmic** scaling (log₁₊₁) prevents runaway popularity — retrieving a memory 100 times produces only twice the boost of 10 times. The **spacing bonus** modulates this:

```
spacing_bonus = min(1.0, days_since_last_access / 7.0)
```

A re-retrieval the same day adds almost zero bonus. A retrieval a week later adds full bonus. This directly implements two findings:

- **Testing effect** (Roediger & Karpicke 2006): retrieving information strengthens the memory trace more than re-reading it. Each access makes the memory slightly easier to find next time.
- **Spacing effect** (Bjork 1975): spaced retrieval at increasing intervals produces superior long-term retention compared to massed retrieval in a single session.

Without the spacing bonus, a memory retrieved 20 times in one debugging session would permanently dominate rankings — popularity bias. The spacing modulation prevents this by rewarding *distributed* access over *concentrated* access.

## Temporal-context neighbours

After reranking, Second Brain appends the temporal neighbours of the top result — memories created within ±24 hours of it. This implements Howard & Kahana's (2002) *temporal context model*: memories encoded close in time are associatively linked. Retrieving one should surface its temporal neighbours because they likely share the same working context.

If you were debugging an auth flow on Tuesday afternoon, the memories you created during that session form a temporal cluster. Retrieving any one of them brings the cluster's neighbours along, reconstructing the context without requiring you to remember the exact titles.

## Recency as a forgetting curve

The recency signal uses power-law decay rather than exponential:

```
recency = (1 + days_old / stability)^(-0.8)
```

Stability grows with access count: `S = 30 + 10 × log₁₊₁(access_count)`. A memory retrieved 10 times has S ≈ 54 days; a never-retrieved memory has S = 30 days.

Why power-law and not exponential? Ebbinghaus (1885) and Murre & Dros (2015, replication) showed human forgetting follows a power law — exponential decay drops too fast initially and too slow later. The power-law long tail means old memories don't vanish; they become harder to retrieve, just like in human memory. And connecting stability to access count means each retrieval *slows the decay curve itself* — the spacing effect directly reshapes the forgetting function.

## Encoding context and contextual reinstatement

The `encoding_context` field captures what you were working on when a memory was created — "debugging auth flow", "reading about CLS theory", "planning dream cycle architecture". At search time, token overlap between your query and the encoding context contributes 0.10 to the rerank score.

This implements one of the most replicated findings in cognitive psychology. Godden & Baddeley (1975) showed that divers who learned word lists underwater recalled them better underwater than on land. Retrieval is dramatically more effective when the retrieval context matches the encoding context. If you were debugging auth when you learned something, searching while debugging auth should boost that memory — and it does.

## Memory classification: three systems

Every memory carries a *mem_class* — one of `semantic`, `procedural`, or `episodic` — following Tulving's (1972) taxonomy and Squire's (1992) elaboration:

- **Semantic** (+0.04 boost): general knowledge, principles, abstractions. "Idempotent migrations require a unique constraint." Highest retrieval utility for future queries.
- **Procedural** (+0.02 boost): how-to knowledge, processes, recipes. "To rotate backup keys, run `scripts/rotate-key.sh`."
- **Episodic** (+0.00 boost): raw session records, time-stamped events. "On Tuesday I discussed auth with Alice." Valuable as source material, but rarely the best direct answer.

The boost reflects that a principle about idempotent migrations should outrank a raw session log that merely mentions migrations. Episodic memories aren't penalised — they receive the base score — but they don't get the bonus that knowledge-distilled memories earn.

## Depth of processing

The *depth_score* (0.0–1.0) measures how elaboratively a memory was encoded. Craik & Lockhart (1972) demonstrated that information processed at deeper, more causal levels is retained and retrieved far more effectively. A bullet-point list of rules is shallow. A memory explaining *why* with a concrete failure example is deep — and dramatically more retrievable.

Second Brain's MCP server checks knowledge-type memories (idea, synthesis, insight, decision) for causal depth signals ("because", "when X then Y", "the fix was") and a "Questions this answers:" section. The resulting depth score feeds the reranker at weight 0.05 — a tiebreaker that consistently promotes the most elaborative memories to the top.

## Active forgetting: the staleness penalty

Memories with zero retrievals (`access_count = 0`) older than 90 days receive a mild penalty that scales with age:

```
staleness = −0.05 × min(1.0, (days_unretrieved − 90) / 180)
```

Maximum penalty is −0.05 at 270+ days. This implements the "use it or lose it" principle from interference theory (Anderson & Neely 1996): unretrieved memories should fade in relevance. Nothing is deleted — the penalty is mild and reversible the moment you retrieve the memory again.

## Project scoping and the encoding specificity principle

When you search within a project context, memories from *other* projects receive a −0.15 penalty. Tulving & Thomson's (1973) encoding specificity principle shows that retrieval is most effective when retrieval context matches encoding context. Cross-project results aren't excluded — sometimes the most valuable insight comes from an unexpected domain — but they are deprioritised so in-context results appear first.

## Related

- [Architecture](architecture.md) — system-level view of all components
- [Database Schema](database-schema.md) — column definitions, indexes, and migrations
- [Dream Cycle Design](dream-cycle-design.md) — how autonomous synthesis creates new memories
- [Using Second Brain](using-second-brain.md) — hands-on search and retrieval
- [Reference](reference.md) — MCP tool parameters and options
- [Glossary](glossary.md) — canonical terminology
