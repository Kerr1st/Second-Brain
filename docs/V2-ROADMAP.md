# Second Brain V2 Roadmap — Cognitive Science-Grounded Enhancements

> **Status:** Items 1–7 implemented. See `.kiro/specs/retrieval-quality/` for the
> implementation spec and `docs/V2-TASKS.md` for the task-level breakdown.

Based on research from Tulving (1972), Squire (1992), Roediger & Karpicke (2006),
Howard & Kahana (2002), Craik & Lockhart (1972), Tulving & Thomson (1973),
Dunlosky et al. (2013), Stickgold (2005), Walker (2009).

Inspired by the kiro-memory project (`~/KiromemoryMCP/mainline/`).

## 1. Memory Classification ✅ (Tulving 1972, Squire 1992)

**Research:** Human long-term memory has three systems — semantic (principles,
knowledge), episodic (events, what happened when), and procedural (how-to steps).
Semantic memories are more broadly useful for retrieval than episodic ones.

**Implementation:** Auto-classify memories into semantic/episodic/procedural based
on content signals (kiro-memory already has regex patterns for this). Add a
`mem_class` column alongside our existing `type` field. Boost semantic memories
during reranking. Our 10-type system (idea, synthesis, research, etc.) maps onto
this: idea/synthesis/insight/decision → semantic, source → episodic, etc.

**Why it matters:** Right now all memory types rank equally. A principle about
"always use idempotent migrations" should outrank a raw session log that happens
to mention migrations.

## 2. Spaced Retrieval Reinforcement ✅ (Roediger & Karpicke 2006)

**Research:** Equally spaced retrieval produces superior long-term retention vs.
massed retrieval. The first few retrievals matter most (diminishing returns).
A memory retrieved once a week for 10 weeks is stronger than one retrieved
10 times in one day.

**Implementation:** Add `last_accessed_at TIMESTAMPTZ` column. Change reinforcement
formula from `0.03 * log(1 + access_count)` to factor in spacing:
`reinforcement = 0.03 * log(1 + access_count) * spacing_bonus`
where `spacing_bonus = min(1.0, days_since_last_access / 7)`.
This means a re-retrieval within the same day adds almost no boost, but a
retrieval a week later adds full boost.

**Why it matters:** Prevents "popularity bias" where a memory retrieved many
times in one session dominates rankings forever.

## 3. Temporal Contiguity ✅ (Howard & Kahana 2002)

**Research:** Memories encoded close in time are associated. Retrieving memory A
makes memories created around the same time as A more accessible. The temporal
context model shows this is a fundamental property of human memory.

**Implementation:** When returning search results, also check for temporal
neighbors — memories created within ±24 hours of the top result. Surface these
as "related context" alongside the main results. Could be a simple SQL query:
`WHERE created_at BETWEEN (top_result.created_at - interval '1 day') AND ...`

**Why it matters:** If you were working on a problem across multiple chats in
one day, retrieving one of those chats should surface the others — they share
temporal context even if their content is different.

## 4. Encoding Specificity / Project Scoping ✅ (Tulving & Thomson 1973)

**Research:** Memory retrieval is most effective when the retrieval context matches
the encoding context. A decision made in Project A may be irrelevant or harmful
in Project B.

**Implementation:** Add `project TEXT` column to memories. Set it during ingestion
based on workspace path (for chats) or explicit tagging. Apply a cross-project
penalty in reranking (kiro-memory uses 0.15). Label cross-project results with
a warning: "From a different project — use as reference only."

**Why it matters:** As the Second Brain grows across multiple projects, search
results from unrelated projects will pollute results. Project scoping prevents
a React convention from overriding a Python decision.

## 5. Depth Scoring ✅ (Craik & Lockhart 1972)

**Research:** Information processed at deeper levels (semantic, elaborative) is
retained and retrieved far more effectively. Shallow encoding (bullet lists)
produces weak memory traces. Deep encoding (causal explanations with examples)
produces strong ones.

**Implementation:** Upgrade depth enforcement from binary warn/don't-warn to a
numeric `depth_score` (0.0–1.0) stored in metadata. Score based on: causal
signals (because, when...then), concrete examples, "Questions this answers"
section, connection to existing knowledge. Use depth_score as a reranking
factor — deeper memories rank higher.

**Why it matters:** Not all memories are created equal. A shallow "use PostgreSQL"
note should rank below a deep explanation of WHY PostgreSQL was chosen, what
the tradeoffs were, and what happens if you switch.

## 6. memory_learn Tool ✅ (Dunlosky 2013 — Elaborative Interrogation)

**Research:** Rated moderate-to-high utility in Dunlosky's meta-analysis of 10
learning techniques. The mechanism: generating explanations for WHY a fact is
true creates richer retrieval paths. Connecting new knowledge to existing
knowledge is one of the most effective learning strategies.

**Implementation:** New MCP tool `memory_learn` (two-step process):
Step 1: Agent provides new content + key topics. Tool searches existing memories
for related knowledge and returns both.
Step 2: Agent synthesizes connections and writes atomic memories via memory_create,
each explicitly connecting to existing knowledge ("this extends our principle
about X because...").

**Why it matters:** Without this, the agent just stores information. With it,
the agent integrates information — building a web of connected knowledge that
produces richer retrieval paths.

## 7. Consolidation Pipeline ✅ → Dream Cycle (Stickgold 2005, Walker 2009)

> **Note:** This item was superseded by the Dream Cycle pipeline — a four-agent
> autonomous learning system (Explorer → Thinker → 4-evaluator BFT Consensus Panel)
> that runs weekly. See `docs/DREAM-CYCLE-DESIGN.md` and `.kiro/specs/dream-cycle/`.

**Research:** During sleep, the hippocampus replays episodic experiences and
gradually transfers abstracted knowledge to neocortical long-term storage.
Raw episodes are compressed into durable, generalizable knowledge.

**Implementation:** Scheduled job (weekly) that:
1. Clusters episodic memories (chat sessions) by topic similarity
2. Generates consolidation templates with source excerpts
3. Runs through Kiro headless to fill in deep learnings (principles,
   causal models, heuristics, failure patterns)
4. Stores consolidated semantic memories with links back to source episodes

kiro-memory's `consolidate_knowledge.py` is a good reference implementation.

**Why it matters:** 5,561 chat sessions are raw episodes. Without consolidation,
the agent has to re-derive patterns from raw transcripts every time. With
consolidation, patterns are pre-extracted into searchable semantic memories.

## 8. "Questions This Answers" Convention (Partially Done)

**Research:** Elaborative interrogation works because it creates multiple retrieval
paths to the same knowledge. If a memory only has a title and content, it can
only be found by matching those. Adding 3-5 natural-language questions creates
additional retrieval paths.

**Implementation:** Convention (not enforced in schema): every knowledge-type
memory should include a "Questions this answers:" section with 3-5 queries
phrased the way a person would actually ask. Depth enforcement already warns
if this is missing. Could also index these questions separately for BM25 boost.

**Why it matters:** Abstract queries like "how should I structure a PRFAQ" need
to match memories that don't contain those exact words but answer that question.

## Priority Order (Original) — All Implemented

1. ✅ **memory_learn tool** — highest impact, enables knowledge integration
2. ✅ **Memory classification + semantic boost** — biggest retrieval quality win
3. ✅ **Consolidation pipeline** → Dream Cycle — transforms raw data into usable knowledge
4. ✅ **Spaced retrieval** — prevents popularity bias
5. ✅ **Project scoping** — becomes critical as data grows
6. ✅ **Depth scoring** — refinement of existing depth enforcement
7. ✅ **Temporal contiguity** — context enrichment in search results
8. 🔲 **Questions convention** — convention enforced via depth scoring, dedicated BM25 index not yet built
