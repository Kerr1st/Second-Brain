---
title: "Dream Cycle Design"
type: explanation
---

# Dream Cycle Design

Why Second Brain has an autonomous synthesis pipeline, and how it works.

## The sleep-consolidation analogy

During sleep your brain doesn't go idle — the hippocampus replays the day's episodic experiences, extracts hidden rules, and integrates them into long-term semantic networks (Stickgold 2005; Walker 2009). You wake up with connections you didn't consciously make. The *dream cycle* is Second Brain's equivalent: a background pipeline that runs during idle time, replays your memories, and surfaces insights you never explicitly asked for.

Letta's "sleep-time compute" research (2025) operationalized this idea for AI systems. Rather than reasoning only at query time, an agent can spend cheap background cycles consolidating knowledge, so that at retrieval time the answer is already distilled. Second Brain applies the same principle — the dream cycle runs on a schedule (via launchd), and the insights it produces are immediately searchable the next time you or an agent queries the MCP server.

> [!NOTE]
> The dream cycle is *not* a summary generator. It finds contradictions between decisions you made months apart, names principles you've been following unconsciously, bridges questions you asked in one project with answers that appeared in another. It produces new memories — typed, embedded, and linked — that participate in retrieval just like anything you wrote manually.

## The four-agent pipeline

The pipeline is **Explorer → Thinker → Consensus Panel → Storage**. Each agent runs as a separate invocation with its own context window, so a failure in one stage doesn't corrupt the others.

```
Explorer             Thinker             Consensus Panel       Storage
───────────────      ───────────────     ───────────────────   ──────────
Assembles memory  →  Analyzes slices  →  4 evaluators vote  →  Accepted
slices (10–20        and proposes        independently on       candidates
memories each)       candidates          each candidate         become real
using 11             (CREATE / UPDATE                           memories
strategies           / SUPERSEDE)
```

### Explorer

The Explorer's job is *curation*, not analysis. It decides which regions of your memory space are worth examining and assembles "memory slices" — curated sets of 10–20 memories that, placed side by side, might reveal something non-obvious. It outputs 0–5 slices per run, each tagged with a hypothesis for the Thinker.

Strategy diversity is encouraged rather than left to chance: the Explorer is instructed to use at least three strategies per cycle and to vary them across runs, and strategies it has underused recently are favored — preventing it from converging on the same two or three comfortable patterns.

### Thinker

The Thinker receives a slice and performs the actual cognitive work. It proposes *candidates* — potential new memories with one of three operations:

| Operation | Meaning |
|-----------|---------|
| **CREATE** | A genuinely new insight, connection, or principle |
| **UPDATE** | An existing memory enriched with new context |
| **SUPERSEDE** | A replacement that obsoletes an older memory |

Each candidate includes structured content (title, body, type, relationships) ready for evaluation.

### Consensus Panel

Four evaluators independently judge each candidate. They vote ACCEPT or REJECT — nothing else. See [Why four evaluators](#why-four-evaluators-binary-bft) below for the fault-tolerance reasoning. The panel members are:

| Evaluator | Role |
|-----------|------|
| **Skeptic** | Challenges factual accuracy and logical coherence |
| **User Advocate** | Asks whether this is useful to *you* specifically |
| **Epistemologist** | Evaluates the quality of reasoning and evidence |
| **Methodologist** | Checks proper use of operations and relationship hygiene |

A candidate is **ACCEPTED** if and only if ≥ 3 of 4 evaluators vote ACCEPT. Otherwise it is **REJECTED**. Dissenting reasoning on 3/4 accepts is preserved in the digest and fed back to future cycles.

### Storage

Accepted candidates are written to the database as real memories with `tags: ["dream-cycle"]` and `metadata: {"dream_cycle": true}`. CREATE inserts a new row; UPDATE modifies an existing memory in place; SUPERSEDE marks the old memory `status = 'superseded'`, creates a `superseded_by` relationship, and inserts the replacement.

For the column-level details of `dream_cycle_runs` and `dream_cycle_candidates`, see [Database schema](database-schema.md).

## Explorer strategies

The Explorer has 11 named strategies. It uses at least three per cycle, varying across runs.

| # | Strategy | What it looks for |
|---|----------|-------------------|
| 1 | **Temporal Juxtaposition** | Same calendar week months apart — do themes recur? |
| 2 | **Cross-Project Collision** | Decisions from Project A vs. recent work in Project B — alignment or conflict? |
| 3 | **Orphan Archaeology** | Zero-relationship, low-access memories — forgotten knowledge seeking connections |
| 4 | **Question-Answer Bridging** | Active questions matched against answers that may exist elsewhere, even in unrelated domains |
| 5 | **Contradiction Hunting** | Opposing language about the same topic ("always X" vs. "stopped doing X") |
| 6 | **Pattern Emergence** | Random sample of 20 recent memories — are there unnamed recurring themes? (Deliberately immune to search bias.) |
| 7 | **Depth Gradient** | High-access but shallow memories — candidates for deepening |
| 8 | **Stale Synthesis Check** | Semantic memories (synthesis, insight, decision) that have new episodic evidence since they were created — the CLS replay mechanism (McClelland 1995) |
| 9 | **Retrieval Failure Analysis** | Memories with low rerank scores or single access — poorly written, or a gap where a better memory should exist |
| 10 | **Desirable Difficulty Surfacing** | High depth but no access in 30+ days, relevant to current work — Bjork's storage-vs-retrieval strength (1992) predicts strongest reinforcement on re-retrieval |
| 11 | **Elaborative Re-Interrogation** | High-value memories not updated in 30+ days, assembled with newer semantic neighbors — the Thinker should UPDATE, not CREATE (Pressley et al. 1987) |

## Why four evaluators (binary BFT)

A single LLM evaluator can hallucinate approval of a shallow or fabricated insight. Two evaluators can split. Three evaluators with a three-state model (ACCEPT / REJECT / DEFERRED) was the original design — and it had problems. The current design uses **four evaluators with binary verdicts**, grounded in Byzantine fault tolerance.

### The theoretical basis

Lamport, Shostak, and Pease (1982) proved that tolerating *f* faulty (or, in our case, hallucinating) nodes requires **3f + 1** total nodes. Setting f = 1 — one evaluator may produce an unreliable verdict — yields 4 required evaluators.

Castro and Liskov's Practical BFT (1999) establishes the quorum at **2f + 1 = 3**. So a candidate is ACCEPTED if and only if ≥ 3 out of 4 evaluators vote ACCEPT. Two or fewer means REJECTED.

### Why binary verdicts (no DEFERRED)

The original 3-evaluator panel had a DEFERRED state: 2/3 ACCEPT meant the candidate re-entered the next cycle with the dissenter's objection as context. This was removed for two reasons:

1. **With 4 evaluators, the "one dissenter" case (3/4) already meets the BFT quorum** — it is safe to ACCEPT directly. The dissenting reasoning is preserved in annotations and feedback injection.
2. **The 2/4 case is genuinely ambiguous under BFT assumptions** — you cannot distinguish a correct majority from a corrupted one. REJECT is the conservative choice.

Binary verdicts also sidestep a known weakness of LLM evaluators: they are poorly calibrated when asked to express degrees of confidence, but reliably distinguish a clear "yes" from a "no".

## Crash handling

An evaluator failure (timeout, API error, malformed response) is *not* the same as a quality judgment. The pipeline treats them differently:

- **Transient failures** — retried up to a cap (currently 2 retries per evaluator per candidate).
- **Persistent failures** — if an evaluator still fails after retries, the entire run is aborted. No partial verdict is recorded.

The core principle: **an infrastructure failure must never be recorded as a quality REJECT vote.** A 2/3 result caused by one evaluator crashing is not the same as a 2/3 result caused by one evaluator genuinely dissenting. Conflating them would poison the feedback loop and erode trust in the panel's signal.

## Memory lifecycle states

Memories move through lifecycle states that balance accessibility against noise:

```
active → superseded → archived
                ↘ user_rejected (metacognition, not deletion)
```

### States and transitions

| State | Meaning | Reranking effect |
|-------|---------|-----------------|
| `active` | Current, participates fully in retrieval | None |
| `superseded` | Replaced by a newer version (dream cycle SUPERSEDE) | −0.20 penalty |
| `archived` | Manually or automatically retired | Excluded from default search |
| `user_rejected` | You explicitly rejected a dream-cycle candidate | Stored for metacognition |

### Why not just delete?

Three cognitive-science principles shape this design:

1. **Bjork's storage vs. retrieval strength (1992)** — A superseded memory still has high *storage strength* (it's richly encoded). Its *retrieval strength* drops because you stop accessing it. But that combination makes it the most valuable target for re-engagement if the context changes. Heavy demotion (−0.20) deprioritizes it without making it invisible to the Explorer's desirable-difficulty strategy.

2. **Complementary Learning Systems theory (McClelland 1995/2016)** — The hippocampus retains episodic traces even after neocortical (semantic) integration, for future replay. Superseded memories are the episodic originals; their consolidated successors are the semantic distillation. Both remain searchable.

3. **Schema theory and metacognition (Bartlett 1932)** — When you reject a dream-cycle candidate, that rejection is information about your mental model. Storing it (with your reason) as `user_rejected` lets future cycles learn what you don't value. Deleting it discards signal.

## Related

- [Architecture](architecture.md) — system-level view of how the pipeline fits into the whole
- [How memory works](how-memory-works.md) — retrieval, reranking, and the cognitive-science signals
- [Database schema](database-schema.md) — column-level details of `dream_cycle_runs` and `dream_cycle_candidates`
- [Operations](operations.md) — running and monitoring the dream cycle launchd job
- [Glossary](glossary.md) — canonical definitions of *dream cycle*, *Express*, and other terms
