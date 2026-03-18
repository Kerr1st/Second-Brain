# Dream Cycle — Multi-Agent Autonomous Learning Architecture

> **Implementation status:** Complete. The pipeline is implemented in `src/dream_cycle/`
> as a decomposed package (orchestrator, consensus, storage, digest, feedback modules).
> See `.kiro/specs/dream-cycle-decomposition/` for the decomposition spec.

> Design document for the Second Brain's proactive learning capability.
> Grounded in cognitive science (Stickgold 2005, Walker 2009, Dunlosky 2013,
> Roediger & Karpicke 2006, McClelland 1995/2016, Bjork 1992, Bartlett 1932,
> Park 2023, Shinn 2023), Byzantine fault tolerance (Lamport 1982, Castro &
> Liskov 1999 PBFT), and multi-agent consensus research (Luo et al. 2025
> WBFT, Yao et al. 2025 Roundtable Policy v2, Letta sleep-time compute,
> Google Always-On Memory Agent 2026).
>
> Informed by product landscape analysis: Cognee, Mem0, Zep/Graphiti, Letta,
> Google Always-On Memory Agent.

## The Problem

The Second Brain currently learns only when the user explicitly triggers it.
`memory_learn` requires the user to share content and say "learn this."
`memory_search` only fires when the user asks a question. The 74K+ memories
sit inert between interactions — no connections are discovered, no patterns
are extracted, no contradictions are surfaced, no implicit principles are named.

The system stores and retrieves. It doesn't think.

## The Insight

Stickgold (2005) and Walker (2009) showed that sleep isn't passive decay — it's
the brain's most creative period. During slow-wave sleep, the hippocampus
actively replays episodic experiences, extracts hidden rules that weren't
apparent during waking, finds connections between seemingly unrelated memories,
and reorganizes raw experiences into generalizable knowledge.

Letta's sleep-time compute research (2025) operationalized this for AI: agents
that "think" during downtime transform "raw context" into "learned context."
Their key finding: agents with learned context need less reasoning at query time
because they've already thought about the material in advance.

The critical design choice: this thinking should not be deterministic code
deciding what to look at. It should be LLMs thinking freely across the memory
space, with multiple passes from different angles. Each pass is a chance to
notice something the previous pass didn't.

---

## Architectural Decision: Three-Speed Enrichment

**Status: DECIDED** (promoted from Open Question #9)

Every serious memory product (Mem0, Cognee, Zep) runs LLM enrichment at ingest
time, not just at consolidation time. Mem0's arxiv paper (2504.19413) shows this
produces better factual accuracy on the LOCOMO benchmark. Cognee runs LLM entity
extraction at cognify time. Zep runs LLM contradiction detection at episode
ingestion. Three out of three production systems do LLM enrichment at ingest.

Our system has three enrichment speeds:

### Speed 1: Interactive `memory_create` — LLM Enrichment

Low volume (5-10 per session), high value per item. On every interactive
`memory_create` call:
- Classify (mem_class: semantic/episodic/procedural) — deterministic
- Depth score (0.0-1.0) — deterministic
- Project tag — deterministic
- **Contradiction check** — LLM: search for existing memories on same topic,
  decide ADD/UPDATE/SUPERSEDE/NOOP (Mem0 pattern)
- **Relationship discovery** — LLM: find top-3 similar existing memories,
  propose relationship types (supports/contradicts/extends)

### Speed 2: Batch Ingestion — Deterministic Only

High volume (hundreds per batch), cost-prohibitive for LLM per item. Chat
ingestion, Crawlee content, bookmark scrapes:
- Classify, depth score, project tag — deterministic (V2 Tasks 3-5)
- No LLM calls. These memories get full LLM enrichment in the dream cycle.

### Speed 3: Dream Cycle — Full Deep LLM Enrichment

The weekly dream cycle re-examines everything, including batch-ingested
memories that only got deterministic enrichment. This is the "slow path"
that catches what the fast paths missed.

This maps to V2-TASKS.md: Tasks 3-6 implement the deterministic enrichment
(Speed 1 & 2). The dream cycle implements Speed 3.

---

## Architectural Decision: Memory Lifecycle States

**Status: DECIDED** (promoted from Open Questions #6 and #7)

Every memory has a lifecycle state. Transitions are triggered by the dream
cycle, user action, or the enrichment pipeline.

```
active → superseded → consolidated → archived
  ↑          ↑             ↑            ↑
  │          │             │            └── User-initiated. Excluded from search.
  │          │             └── Distilled into semantic memory. Moderate demotion
  │          │                 in normal search, full visibility to Explorer.
  │          └── Replaced by newer memory. Moderate demotion in normal search,
  │              full visibility to Explorer. Annotated with supersession reason.
  └── Default. Full visibility everywhere.
```

**Why moderate demotion, not heavy, for superseded memories:**
Bjork's storage strength vs. retrieval strength (1992) shows that memories with
high storage strength but low retrieval strength are the most valuable candidates
for re-engagement. A superseded memory that was deeply encoded has high storage
strength. Heavy demotion would make it invisible to the Explorer's desirable
difficulty strategy (#10) — exactly the mechanism designed to find these
high-value re-engagement targets.

**Why "consolidated" not "archived" for distilled sources:**
CLS theory (McClelland 1995/2016) says the hippocampus retains episodes even
after neocortical integration, for future replay. Consolidated memories remain
searchable but don't compete with their distilled semantic successors in normal
results. Cognee's memify prunes stale nodes but preserves graph structure.
Zep invalidates but never deletes.

**On SUPERSEDE mechanics:**
- Create `superseded_by` relationship from old → new memory
- Old memory's existing relationships stay intact (historical context)
- New memory builds its own relationship set
- Old memory status set to "superseded"
- Old memory annotated: "Superseded on [date] by [link] because [reason]"
  (Zep's narrative rewriting pattern)

**On user rejection of dream cycle insights:**
User rejection marks the insight as `status: user_rejected` with the user's
reason stored. This is NOT deletion — it's meta-cognitive information that
feeds back into Panel calibration. Supported by Zep's "never delete, only
invalidate" principle and Bartlett's schema theory (the rejection itself is
information about the user's epistemic standards).

### Schema changes for lifecycle states

```sql
-- Add expired_at to relationships (Zep bi-temporal model, relationships only)
ALTER TABLE memory_relationships ADD COLUMN expired_at TIMESTAMPTZ;

-- New status values: 'active', 'superseded', 'consolidated', 'archived', 'user_rejected'
-- (status column already exists as TEXT, no schema change needed)

-- Note: valid_at/invalid_at on memories deferred. Known limitation: stale-but-active
-- memories look identical to current-and-active between dream cycle runs. The dream
-- cycle's "stale synthesis check" strategy is the active mitigation.
```

---

## Byzantine Consensus Panel — Design Rationale

### The Problem

The Dream Cycle's original consensus panel used 3 evaluators (Skeptic, User
Advocate, Epistemologist) with a 3/3 unanimity threshold. The architecture was
named "Four-Agent Byzantine Consensus Pipeline" and referenced Lamport's 1982
Byzantine Generals Problem, but the implementation did not satisfy Byzantine
fault tolerance. The core issue: LLM evaluators can hallucinate — producing
verdicts that look valid but are grounded in fabricated reasoning. A
hallucinating evaluator is functionally equivalent to a Byzantine fault.

### Research Foundation

Three papers ground this design:

**Lamport, Shostak, and Pease (1982)** — "The Byzantine Generals Problem"
(ACM TOPLAS). The foundational result: tolerating f faulty nodes requires 3f+1
total nodes. With oral (unsigned) messages, consensus is impossible if ≥1/3 of
participants are faulty. The original 3-evaluator panel could tolerate f=0
faults (since 3f+1=1 for f=0, and 3f+1=4 for f=1). A single hallucinating
evaluator could corrupt the consensus.

**Castro and Liskov (1999)** — "Practical Byzantine Fault Tolerance" (OSDI).
Formalized the BFT quorum at 2f+1 out of 3f+1 nodes. The key property: any
two quorums of size 2f+1 overlap in at least f+1 nodes, guaranteeing at least
one honest node in the intersection. With f=1, the BFT quorum is 3 out of 4.
This is the mathematically proven safe threshold for acceptance.

**Shinn et al. (2023)** — "Reflexion: Language Agents with Verbal
Self-Reflection." Demonstrated that feeding specific verbal feedback from
failed attempts back to an agent produces significant improvement (80% → 91%
on HumanEval). The dream cycle's original DEFERRED mechanism was inspired by
this — passing the dissenter's objection to the Thinker for revision. Our
analysis concluded that while the Reflexion pattern is valuable, it requires
a trustworthy trigger signal, and the 2/4 threshold under BFT is not
trustworthy (see "Why DEFERRED Was Removed" below).

### Design Evolution

The design went through three stages during the specification process:

**Stage 1: Naive BFT (4 evaluators, 3/4 supermajority)**

Initial proposal: add a 4th evaluator (Methodologist), change threshold to
3/4 = ACCEPTED, 2/4 = DEFERRED, ≤1/4 = REJECTED. This satisfied the Lamport
3f+1 bound but set DEFERRED at 2/4 (50% agreement) — lower than the old 2/3
(67%). Analysis revealed this was problematic.

**Stage 2: BFT-correct thresholds (4/4 ACCEPTED, 3/4 DEFERRED)**

Revised proposal: 4/4 = ACCEPTED, 3/4 = DEFERRED, ≤2/4 = REJECTED. The
rationale for rejecting 2/4 as DEFERRED:

With 4 evaluators and f=1, there are 3 loyal evaluators and 1 potential
traitor. A 2/4 ACCEPT result means either:
- (a) 2 loyal + 0 traitor accepted, 1 loyal + 1 traitor rejected — genuine
  2-1 split among loyal evaluators
- (b) 1 loyal + 1 traitor accepted, 2 loyal + 0 traitor rejected — only 1
  genuine supporter

Cases (a) and (b) are indistinguishable. BFT requires designing for the worst
case. In case (b), deferring wastes a full cycle on a candidate with only 1
real supporter. Therefore 2/4 = REJECTED.

This stage preserved DEFERRED at 3/4 (one dissenter) with the two-strike rule.
But it introduced a new problem: requiring 4/4 for ACCEPTED meant the system
tolerated 0 faults for acceptance — the same as the old 3/3 model, just with
an extra evaluator call. This undermined the reason for adding the 4th
evaluator.

**Stage 3: Pure BFT binary consensus (final design)**

The PBFT quorum of 2f+1 = 3 out of 3f+1 = 4 is the mathematically proven
safe threshold. Setting ACCEPTED at 3/4 (not 4/4) means the system actually
uses the fault tolerance it was designed for. This eliminated the "narrow miss"
state entirely, leading to the removal of DEFERRED.

### Why DEFERRED Was Removed

The DEFERRED state served two purposes in the original design:
1. Give narrow misses a second chance via Thinker revision with the dissenter's
   objection (Reflexion pattern)
2. The two-strike rule catches persistent disagreements

Both purposes depend on a trustworthy "close but not quite" signal. With the
new BFT model:
- 3/4 ACCEPT is the BFT quorum → ACCEPTED (no "close but not quite" — it
  passed)
- 2/4 ACCEPT is ambiguous under BFT → REJECTED (can't trust it was close)

There is no threshold between ACCEPTED and REJECTED where DEFERRED would have
a trustworthy trigger. The Reflexion pattern is valuable, but it needs a
reliable signal that the candidate was genuinely close to good. Under BFT
worst-case analysis, 2/4 doesn't provide that signal.

### Final Architecture: Binary BFT Consensus

| Accepts | Result | Rationale |
|---------|--------|-----------|
| ≥3/4 | ACCEPTED | BFT quorum met (2f+1 = 3 out of 3f+1 = 4) |
| ≤2/4 | REJECTED | Insufficient consensus under worst-case BFT |

No DEFERRED state. Binary consensus.

### The Four Evaluators

Each evaluator assesses candidates from an orthogonal perspective:

| Role | Perspective | Key Criteria |
|------|------------|-------------|
| Skeptic (A) | Factual grounding | Accuracy, non-obviousness, logical validity, actionability |
| User Advocate (B) | User value | Relevance, timing, signal-to-noise, depth |
| Epistemologist (C) | Knowledge quality | Evidence sufficiency, falsifiability, novelty, durability, retrievability |
| Methodologist (D) | Reasoning rigor | Internal consistency, source independence, reasoning structure, reproducibility |

The Methodologist was chosen as the 4th evaluator because it covers a gap the
other three don't: whether the reasoning process itself is sound. The Skeptic
checks if claims are factually grounded, the Epistemologist checks if the
knowledge claim is valid, but neither checks whether the cited sources are
actually independent or whether the reasoning chain is traceable. The
Methodologist catches the case where the Thinker cites a memory and its chunk
as two independent sources, or where the conclusion requires unstated
assumptions not present in the evidence.

### Signal Preservation Without Re-evaluation

Removing DEFERRED eliminates the re-evaluation machinery but preserves the
learning signal through two lightweight mechanisms:

1. **Digest dissent annotation:** When an insight is accepted with 3/4 (one
   dissenter), the digest shows: "Accepted (3/4) — [Role] dissented:
   [reasoning]". This surfaces the concern for human review without blocking
   acceptance.

2. **Feedback injection of accepted dissents:** A new section in the Explorer's
   feedback injection — "Dissenting concerns on accepted insights" — surfaces
   patterns from accepted-but-not-unanimous candidates. If the Methodologist
   keeps flagging source independence issues on accepted insights, that signal
   flows into the Explorer's next run, influencing what memory slices it
   assembles. This is a closed feedback loop that drives systemic improvement
   without per-candidate re-evaluation.

This approach is actually more valuable than DEFERRED's per-candidate revision.
DEFERRED said "re-evaluate this specific candidate." The feedback injection
approach says "learn from this pattern across all candidates." The latter
addresses systemic issues, not just individual cases.

### What Was Removed

| Component | Location | Status |
|-----------|----------|--------|
| tally_consensus DEFERRED branch | consensus.py | Removed — binary ACCEPTED/REJECTED |
| _is_second_deferral() | orchestrator.py | Removed — no deferred candidates |
| Deferred candidate handling in invoke_thinker() | orchestrator.py | Removed |
| get_deferred_candidates() | dream_cycle_db.py | Deprecated — no longer called |
| mark_deferred_twice_rejected() | dream_cycle_db.py | Deprecated — no longer called |
| Two-strike rule | orchestrator.py | Removed — no deferred state |
| "Deferred evidence gathering" Explorer strategy | prompts/explorer.py | Removed |
| candidates_deferred tracking | DreamCycleResult, dream_cycle_runs | Always 0 — field kept for schema compatibility |

**Why not weighted voting (WBFT model):**
The WBFT paper (Luo et al. 2025) addresses adversarial environments with
compromised LLM nodes. Our evaluators are the same model with different prompts
— no adversarial threat model applies. More critically, "When Two LLMs Debate,
Both Think They'll Win" (Prasad & Nguyen, 2025) demonstrates that LLM confidence
scores are systematically miscalibrated: 72.9% average initial confidence vs.
50% rational baseline, with confidence *escalating* during interaction. In 61.7%
of debates, both sides simultaneously claimed ≥75% probability of victory.
Weighted voting requires calibrated confidence scores. LLMs don't produce them.

The binary BFT model sidesteps calibration entirely. It asks "accept or
reject?" — binary verdicts with reasoning, not numerical confidence.

**Why not debate protocol between evaluators:**
Independent evaluation for all execution modes. The debate literature shows
quality improvements but risks convergence — all evaluators anchoring on the
first one's reasoning. With independent evaluation, we get genuinely diverse
perspectives.

---

## Architecture: Four-Agent Byzantine Consensus Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│                     DREAM CYCLE                             │
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌─────────────────────┐   │
│  │ EXPLORER │───▶│ THINKER  │───▶│   CONSENSUS PANEL   │   │
│  │ (Agent 1)│    │ (Agent 2)│    │   (BFT: 3f+1 = 4)  │   │
│  │          │    │          │    │                     │   │
│  │ Decides  │    │ Finds    │    │  Evaluator A ──┐    │   │
│  │ what to  │    │ patterns,│    │  Evaluator B ──┼─▶? │   │
│  │ look at  │    │ insights │    │  Evaluator C ──┤    │   │
│  └──────────┘    └──────────┘    │  Evaluator D ──┘    │   │
│       │                          │                     │   │
│       │                          │  ≥3/4 = ACCEPT      │   │
│       │                          │  ≤2/4 = REJECT      │   │
│       │                          └─────────┬───────────┘   │
│       │                                    │               │
│       │  ┌─────────────────┐       ┌───────▼───────┐      │
│       └──│ FEEDBACK LOOP   │       │   ACCEPTED    │      │
│          │ (rejections +   │       │   INSIGHTS    │      │
│          │  accepted       │       │               │      │
│          │  dissents from  │       │ → memory_create│      │
│          │  last 3 cycles) │       │ → memory_relate│      │
│          └─────────────────┘       │ → user digest  │      │
│                                    └───────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### Why Four Agents, Not One

A single agent doing reflection produces insights with no quality gate —
hallucinated connections, shallow observations, noise alongside signal. The
Roundtable Policy (Yao et al. 2025 v2) demonstrated that structured consensus
among diverse LLM agents "significantly enhances reasoning and reduces
hallucinations that single models are prone to."

### Why the Explorer Is Separate

If the Thinker decides what to look at, it gravitates toward obvious connections.
The Explorer creates novel combinations — analogous to how the hippocampus
during sleep replays experiences in combinations that didn't occur during waking
(Stickgold 2005). The Explorer's random sampling strategy (#6) is specifically
the antidote to search bias — all other strategies use the same retrieval
infrastructure and share the same blind spots. Random sampling is the only
strategy immune to retrieval bias.

---

## Agent 1: The Explorer

### Role

Autonomously decide what regions of the memory space to examine. Assemble
"memory slices" — curated sets of 10-20 memories that might yield interesting
connections when a Thinker examines them together.

### Available Tools

- `memory_search` — semantic + keyword search
- `memory_list` — filtered listing (by type, source_type, status)
- `memory_read` — full content of a specific memory
- `memory_graph` — a memory and all its relationships
- Direct SQL read access (aggregate queries, random sampling, temporal windows)

### Feedback Injection (Mechanized Learning Loop)

Before each run, the Explorer receives a "Lessons from recent cycles" section
injected from the `dream_cycle_candidates` table. This queries the last 3
cycles' rejections, grouped by evaluator and rejection reason, with the actual
evaluator reasoning quoted — not just statistical counts.

This is grounded in Reflexion (Shinn 2023): specific verbal self-reflection
("the second for loop is unnecessary") drives improvement, not statistical
summaries ("4/7 rejected"). The specificity of the feedback is what produces
the 80% → 91% improvement Reflexion demonstrated.

Example injection:
```
## Lessons from recent cycles (last 3 runs)

Cycle 2026-03-09: 2 accepted, 3 rejected
- Skeptic rejected 2 of 3 candidates for INSUFFICIENT EVIDENCE: "The claimed pattern of
  'preferring self-hosted solutions' is based on only 2 memories — one from
  the Second Brain project and one from a chat about Docker. Two instances
  from related contexts is not a pattern."
- User Advocate rejected 1/3 for LOW RELEVANCE: "This insight about bookmark
  organization patterns relates to a workflow the user hasn't touched in 4
  months. Not worth a digest slot."

Adjust your strategies: prioritize slices with 3+ independent memories from
different time periods. Weight recent activity (last 30 days) higher.
```

### Prompt

```
You are the Explorer agent in a personal knowledge system containing {memory_count}
memories spanning {date_range}. Your job is to assemble interesting "memory slices"
— curated sets of 10-20 memories that, when examined together, might reveal hidden
patterns, contradictions, implicit principles, or surprising connections.

You are NOT looking for obvious connections. You are looking for the non-obvious:
memories from different time periods that echo each other, decisions that contradict
each other across projects, recurring problems that suggest an unnamed principle,
questions that were asked months ago and now have answers elsewhere in the system.

{feedback_injection}

## Your exploration strategies (use at least 3 per cycle, vary across cycles):

1. TEMPORAL JUXTAPOSITION — Pull memories from the same calendar week but months
   apart. What was the user thinking about in March vs September? Do themes recur?

2. CROSS-PROJECT COLLISION — Find decisions or principles from Project A and
   juxtapose them with recent work in Project B. Do they align or conflict?

3. ORPHAN ARCHAEOLOGY — Find memories with zero relationships and low access_count.
   These are forgotten knowledge. Search for what they might connect to.

4. QUESTION-ANSWER BRIDGING — Find memories of type "question" that are still
   active. Search the broader memory space for memories that might answer them,
   even partially, even from unrelated domains.

5. CONTRADICTION HUNTING — Search for decisions or principles that use opposing
   language about the same topic. "We should always X" vs "We stopped doing X
   because..."

6. PATTERN EMERGENCE — Sample 20 random memories from the last 90 days. What
   themes appear more than twice? Are those themes named as principles anywhere?
   If not, there may be an implicit principle worth surfacing. (Note: this random
   sampling is deliberately immune to search bias — it's the only strategy that
   can find poorly-keyworded or poorly-embedded memories.)

7. DEPTH GRADIENT — Find the shallowest high-access memories (frequently retrieved
   but lacking causal depth). These are candidates for deepening.

8. STALE SYNTHESIS CHECK (CLS-informed) — Find semantic memories (synthesis,
   insight, decision) and search for NEW episodic memories that arrived AFTER
   the semantic memory was created. Don't just check if the synthesis is "stale"
   — check if new evidence has arrived that should be interleaved with it. This
   is the CLS replay mechanism (McClelland 1995/2016): existing knowledge being
   re-examined in light of new experiences.

9. RETRIEVAL FAILURE ANALYSIS — Find memories returned by memory_search with very
   low rerank scores, or memories accessed once and never again. These may be
   poorly written or indicate a gap where a better memory should exist.

10. DESIRABLE DIFFICULTY SURFACING — Find memories with high depth_score (≥ 0.7)
    but no access in the last 30+ days. These are deeply encoded knowledge the
    user has partially forgotten. Cross-reference against recent activity (last
    7 days). If relevant to current work, assemble into a slice. (Bjork 1992:
    high storage strength + low retrieval strength = strongest reinforcement
    on re-retrieval.)

When assembling slices, pay attention to temporal validity of knowledge. A memory
that was later superseded is different from one that's still active. Check for
superseded/consolidated status. Superseded memories are valuable for contradiction
hunting and stale synthesis strategies — they're signals of knowledge evolution.

## Output format

For each memory slice, provide:
- A name for the slice (e.g., "Cross-project database decisions")
- The strategy used
- The memory IDs and titles included
- A 1-2 sentence hypothesis about what the Thinker might find

Assemble 3-5 memory slices per cycle. Prioritize diversity of strategy.
If no strategies yield interesting slices, return an empty list and explain why.
It is better to surface nothing than to force weak slices.
```

### Design rationale

Strategies are inspired by:
- Temporal juxtaposition → Howard & Kahana 2002 (temporal context model)
- Cross-project collision → Tulving & Thomson 1973 (encoding specificity)
- Orphan archaeology → Roediger & Karpicke 2006 (retrieval strengthens memory)
- Question-answer bridging → Dunlosky 2013 (elaborative interrogation)
- Pattern emergence → Walker 2009 (extraction of hidden rules during sleep)
- Stale synthesis → McClelland 1995/2016 (CLS interleaving)
- Retrieval failure → Shinn 2023 (Reflexion meta-cognitive feedback)
- Desirable difficulty → Bjork 1992 (storage vs retrieval strength)

---

## Agent 2: The Thinker

### Role

Receive memory slices from the Explorer and do the deep creative work of
finding connections, patterns, contradictions, and implicit principles. Generate
candidate insights — each one a fully-formed memory ready for consensus review.
Can also propose UPDATE/SUPERSEDE operations on existing memories.

### Available Tools

- `memory_search` — to pull in additional context if needed
- `memory_read` — to read full content of memories in the slice
- `memory_graph` — to see existing relationships

### Prompt

```
You are the Thinker agent in a personal knowledge system. The Explorer has
assembled memory slices — curated sets of memories that might contain hidden
connections. Your job is to examine each slice deeply and generate candidate
insights.

## What counts as an insight worth surfacing

An insight is NOT a summary. It is NOT restating what the memories say.
An insight is something the user doesn't already know — a connection they
haven't made, a pattern they haven't named, a contradiction they haven't
noticed, a principle that's implicit in their behavior but never stated.

Specifically, look for:

1. UNNAMED PRINCIPLES — The user does something consistently across multiple
   memories but has never articulated it as a rule. Name it for them.

2. CONTRADICTIONS — Two memories assert incompatible things. This is a signal
   that thinking has evolved, or that context matters in a way not articulated.

3. RESOLVED QUESTIONS — A question was asked months ago and the answer now
   exists in the memory space, possibly from a completely different context.

4. EMERGING PATTERNS — A theme appears across 3+ memories from different
   time periods or projects.

5. KNOWLEDGE GAPS — The memory space has depth in area A and area B, but
   the connection between them is unexplored.

6. STALE KNOWLEDGE — A synthesis or decision was made based on information
   that has since been superseded by newer memories.

7. META-COGNITIVE REFLECTION (Shinn 2023) — Examine the dream cycle's own
   history. Look at rejected candidates from previous cycles. What patterns
   appear in the rejections? Are there systematic blind spots? Also examine
   retrieval quality: when searches return poor results, what does that tell
   us about gaps in the memory space? Meta-cognitive insights are stored with
   tag "meta-cognitive" and feed back into the Explorer's strategy selection.

8. CLS INTERLEAVING (McClelland 1995/2016) — When examining a slice, actively
   search for existing semantic memories (synthesis, insight, decision) that
   cover the same topic. Don't skip them — re-examine them. Does new episodic
   evidence confirm the existing principle? Extend it? Reveal an edge case?
   Contradict it? If confirmed, strengthen it with new evidence citations.
   If contradicted, flag explicitly — this is one of the highest-value insights.
   The goal is incremental evolution, not replacement.

9. DISTILLATION (Memvid) — When examining a cluster of episodic memories about
   the same topic, ask: can these 5-10 raw episodes be distilled into a single,
   well-written semantic memory? Distillation extracts the principle and discards
   the narrative. "We tried three caching approaches and the third worked" is a
   summary. "Cache invalidation at the application layer is more reliable than
   TTL-based expiry when data consistency matters more than latency" is a
   distillation. Distilled memories link back to sources via derived_from.

## Output format for each candidate insight

{
  "title": "Concise, specific title",
  "type": "insight | connection | question | synthesis",
  "operation": "CREATE | UPDATE | SUPERSEDE",
  "target_memory_id": "uuid (for UPDATE/SUPERSEDE only)",
  "supersedes_reason": "Why the old memory is being replaced (SUPERSEDE only)",
  "schema_operation": "assimilation | accommodation",
  "schema_note": "If assimilation: which existing principle does this reinforce?
    If accommodation: what specific aspect needs to change, and why?",
  "confidence": "high | medium | low",
  "confidence_reasoning": "Why this confidence level — what would change it?",
  "content": "Full insight text following the depth framework:
    WHAT: The insight itself
    EVIDENCE: Which memories support this (IDs and brief quotes)
    WHY IT MATTERS: What the user should do with this knowledge
    Questions this answers: 3-5 natural-language queries",
  "source_memories": ["uuid1", "uuid2", ...],
  "relationships": [
    {"target_id": "uuid", "relation_type": "extends|contradicts|supports", "note": "..."}
  ],
  "strategy_that_found_it": "e.g., cross-project collision"
}

Generate 1-3 candidate insights per memory slice. Quality over quantity.
If a slice doesn't yield anything non-obvious, say so — don't force insights.

The confidence field is informational for the digest — it does NOT gate whether
the Panel evaluates the candidate. The Panel is the single quality gate. If you
generate a candidate, it goes to the Panel regardless of confidence level. Only
return "this slice yielded nothing" if there is genuinely nothing worth proposing.
```

### Design rationale

The Thinker prompt is deliberately open-ended. This is where the LLM's creative
capability matters most. The depth framework (WHAT / EVIDENCE / WHY IT MATTERS)
is grounded in Craik & Lockhart 1972. The schema_operation field (Bartlett 1932)
ensures every connection is explicitly classified as assimilation or accommodation.
Accommodation insights get a priority boost in the consensus panel — schema
changes represent genuine learning.

The UPDATE/SUPERSEDE capability (from Mem0's ADD/UPDATE/DELETE/NOOP pattern)
means the Thinker can propose modifications to existing knowledge, not just
create new knowledge. This is what turns the dream cycle from an insight
generator into a knowledge evolution engine.

---

## Agents 3a, 3b, 3c, 3d: The Consensus Panel

### Role

Four independent evaluators each assess every candidate insight. An insight
is accepted if 3 or more agree (≥3/4 BFT quorum). Evaluators never see each
other's reasoning — fully independent evaluation to avoid convergence.

### Evaluator A: The Skeptic

```
You are evaluating a candidate insight. Your role is THE SKEPTIC — you look
for reasons the insight might be wrong, shallow, or misleading.

## The candidate insight:
{insight_json}

## The source memories it references:
{source_memories_content}

## Evaluate on these criteria:

1. FACTUAL GROUNDING — Does the insight accurately represent what the source
   memories actually say? Or does it misquote, exaggerate, or hallucinate
   connections that aren't in the text?

2. NON-OBVIOUSNESS — Would the user likely already know this? The bar: would
   this make the user stop and think?

3. LOGICAL VALIDITY — Does the reasoning hold? If it claims a contradiction,
   are the positions actually incompatible? If a pattern, is 2 occurrences
   really a pattern or coincidence?

4. ACTIONABILITY — Can the user do something with this insight?

For UPDATE/SUPERSEDE operations, apply additional scrutiny:
5. PRESERVATION — Does the update preserve the valuable parts of the original
   memory? Or does it discard hard-won knowledge for a superficially newer
   observation?
6. EVIDENCE DELTA — Is the new evidence strong enough to justify changing an
   established memory? A single new data point shouldn't override a principle
   derived from 10 data points. (Cognee memify principle: refinement should
   be safe and incremental.)

## Your verdict
ACCEPT or REJECT. No "maybe." Provide reasoning before your verdict.
```

### Evaluator B: The User Advocate

```
You are evaluating a candidate insight. Your role is THE USER ADVOCATE — you
evaluate whether this insight is genuinely valuable to the human who owns
this knowledge base.

## The candidate insight:
{insight_json}

## The source memories it references:
{source_memories_content}

## Context about the user:
Developer/technologist who values research-grounded decisions, understanding
WHY before building WHAT, proactive collaboration, documentation quality,
and strategic thinking across projects.

## Evaluate on these criteria:

1. RELEVANCE — Does this connect to something the user is actively working on?

2. TIMING — Is this the right moment for this insight?

3. SIGNAL-TO-NOISE — If the user receives 3-5 insights per week, is this one
   worth a slot? Would the user be glad they saw it, or think "so what?"

4. DEPTH — Does it explain WHY, not just WHAT? Enough context to evaluate
   without reading all source memories?

## Your verdict
ACCEPT or REJECT. Provide reasoning before your verdict.
```

### Evaluator C: The Epistemologist

```
You are evaluating a candidate insight. Your role is THE EPISTEMOLOGIST — you
evaluate the quality of the knowledge claim itself.

## The candidate insight:
{insight_json}

## The source memories it references:
{source_memories_content}

## Evaluate on these criteria:

1. EVIDENCE SUFFICIENCY — How many independent memories support this? A claim
   based on 1 memory is an observation. Based on 3+ from different periods
   is a pattern.

2. FALSIFIABILITY — Could this be wrong? What would disprove it? Unfalsifiable
   claims are too vague to be useful.

3. NOVELTY — Does this create new knowledge or just reorganize existing?
   Check the schema_operation field: "accommodation" (schema change) is
   inherently higher novelty than "assimilation" (schema confirmation).
   An accommodation insight that updates an existing principle with new
   evidence creates genuine new knowledge. An assimilation insight that
   confirms what's already known is reorganization — lower value unless
   the confirmation itself is surprising.

4. DURABILITY — Will this still be relevant in 6 months?

5. RETRIEVABILITY — Will it be findable when needed? Clear "Questions this
   answers"? Keywords specific enough for BM25, semantics clear for vector?

## Your verdict
ACCEPT or REJECT. Provide reasoning before your verdict.
```

---

## Consensus Protocol

```
For each candidate insight from the Thinker:
  1. Send to all four Evaluators independently (no shared context)
  2. Collect verdicts: ACCEPT or REJECT with reasoning
  3. Count accepts:
     - ≥3/4 ACCEPT → ACCEPTED. Create/update memory, create relationships,
       add to digest. If 3/4 (one dissenter), annotate digest with dissent.
     - ≤2/4 ACCEPT → REJECTED. Log rejection reasons for Explorer feedback.
  (Binary consensus — no DEFERRED state. See "Byzantine Consensus Panel —
  Design Rationale" above for the full BFT analysis.)
```

---

## Execution Modes

### 1. Scheduled Dream Cycle (Weekly)

The primary mode. Full four-agent pipeline.

- Frequency: Weekly (Sunday night, after backup and chat extraction)
- Budget: Configurable max Kiro credits per cycle
- Circuit breaker: If the Explorer returns empty slices, abort early — skip
  Thinker + Panel entirely. Saves budget on cycles where the memory space
  hasn't changed meaningfully.
- Output: Digest of accepted insights + human-readable summary for Monday

### 2. Post-Learn Reflection (Event-Triggered)

After `memory_learn` creates new atomic insights, trigger a scoped dream cycle.

- Cooldown: Batched to session end, not triggered after each `memory_learn`
  call. If the user does 10 memory_learn calls in one session, one reflection
  runs after the session ends. Simpler than distributed locking, avoids race
  conditions with concurrent memory_learn + dream cycle.
- Deduplication: Before creating an accepted insight, check embedding
  similarity against existing memories. If a similar memory exists (from a
  previous dream cycle or from memory_learn itself), skip creation. When
  memory_learn runs, recent dream cycle insights appear in the "Related
  Existing Memories" section — so the agent knows what the system already
  figured out autonomously.
- Explorer scope: "These N new insights were just added. What existing
  memories might they connect to, contradict, or extend?"
- Budget: Smaller (e.g., 8 Kiro credits)

### 3. Session-Start Briefing (On-Demand)

Full pipeline with scoped Explorer. The Panel is NOT skipped — session-start
is the highest-stakes execution mode (first thing the user sees). A bad insight
here erodes trust in the entire dream cycle. The Microsoft Copilot backlash
(2025-2026) demonstrated that unsolicited AI suggestions with insufficient
quality gating create friction and erode user trust rapidly.

- Frequency cap: Skip if no new memories created AND no dream cycle ran since
  last session. Don't brief on an unchanged memory space.
- Explorer scope: Strategies 6 (pattern emergence from last 7 days), 8 (stale
  synthesis check against recent activity), and 10 (desirable difficulty) only.
  Scoped Explorer, not skipped Panel.
- Thinker: 1-2 candidates max
- Panel: Full ≥3/4 BFT consensus
- Output: Brief "here's what I noticed" if anything passes, silence if not.
  Silence is golden — it is better to surface nothing than something mediocre.

### 4. User-Triggered Deep Dive

The user says "reflect on X" and the system runs a focused dream cycle.

- Explorer: Assembles slices specifically around topic X, all 10 strategies
- Thinker + Panel: Full pipeline
- Output: Immediate results, not batched into weekly digest

---

## Digest Format

The weekly digest is how the user experiences the dream cycle's output. For a
system that runs unsupervised and creates memories autonomously, user trust is
everything.

Each digest includes:
- List of accepted insights with the Thinker's confidence indicator
- For each insight: 1-line summary + expandable full content
- Source memories linked (clickable)
- Evaluator reasoning (collapsed/expandable — shows why it passed)
- For UPDATE/SUPERSEDE operations: clearly marked diff showing what changed
  and why, with link to the original memory
- "Reject" action on each insight: marks as `user_rejected` with reason prompt,
  feeds back into Panel calibration. Does NOT delete the memory.
- Run statistics: candidates generated / accepted / rejected
- Explorer strategies used this cycle
- Feedback loop summary: what the Explorer learned from last cycle's rejections
  and dissenting concerns on accepted insights

**v1 delivery: static markdown file.** The dream cycle runs via Kiro headless
as a scheduled job. The digest is written to `logs/dream-cycle-digest-{date}.md`.
The interactive features described above (expand/collapse, reject button) require
a future UI layer. For v1, the user reviews the markdown file and can reject
insights by running a CLI command or MCP tool call (`memory_update {id}
status=user_rejected`). The digest format is designed to be useful as static
markdown while being forward-compatible with an interactive UI.

---

## Deduplication with memory_learn

The dream cycle and `memory_learn` are two paths that can independently discover
the same connections. Without deduplication, you get duplicate or conflicting
insights.

Three mechanisms:
1. **On dream cycle create:** Before storing an accepted insight, search for
   existing memories with embedding similarity > 0.85. If found, skip creation
   and log as "already known." (Note: the 0.85 threshold is a starting point
   that needs empirical tuning against real dream cycle output. Too strict
   and near-duplicates slip through; too loose and legitimate related-but-distinct
   insights get blocked. Calibrate after the first 3-4 weekly cycles.)
2. **On memory_learn:** Surface recent dream cycle insights (last 7 days) in
   the "Related Existing Memories" section, so the agent knows what the system
   already figured out autonomously.
3. **Race prevention:** Post-learn reflection uses a session-end cooldown, not
   per-call triggering. This avoids concurrent memory_learn + dream cycle runs.

---

## Cost Model

Measured in Kiro credits (the actual cost unit), not LLM calls or sessions.

**Execution model:** Each agent runs as a separate `kiro --no-interactive`
invocation — separate process, separate auth, separate context window. This
is required because: (a) the evaluators must not share context with each other
or with the Thinker, (b) each agent has a different system prompt, and (c)
failure isolation — one agent crashing doesn't take down the pipeline. The
orchestrator script invokes them sequentially (Explorer → Thinker → Evaluators
in parallel) and passes outputs between stages via the `dream_cycle_candidates`
table and JSON files.

### Weekly Dream Cycle
- Explorer: 1 Kiro session (may use 5-10 tool calls internally)
- Thinker: 1 session per memory slice (3-5 slices = 3-5 sessions)
- Panel: 1 session per candidate × 4 evaluators (3-10 candidates × 4 = 12-40 sessions)
- Typical: 16-30 Kiro sessions (1 Explorer + 3-5 Thinker + 12-24 Panel).
  The wide range depends on how many slices yield candidates.
- Circuit breaker: Explorer empty slices → abort, 1 session total

### Session-Start Briefing
- Scoped Explorer: 1 session
- Thinker: 1 session (1-2 candidates)
- Panel: 4-8 sessions (1-2 candidates × 4 evaluators)
- Typical: 6-11 Kiro sessions, at most once per 24 hours
- Frequency cap prevents unnecessary runs

### Post-Learn Reflection
- Scoped Explorer: 1 session
- Thinker: 1-2 sessions
- Panel: 4-8 sessions
- Typical: 6-11 Kiro sessions
- Batched to session end, not per memory_learn call

---

## Schema

```sql
CREATE TABLE dream_cycle_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type TEXT NOT NULL,          -- scheduled, post_learn, session_start, user_triggered
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    explorer_output JSONB,           -- memory slices assembled
    explorer_feedback_injected TEXT,  -- the "Lessons from recent cycles" text
    candidates_generated INTEGER,
    candidates_accepted INTEGER,
    candidates_deferred INTEGER,     -- legacy, always 0 (DEFERRED state removed)
    candidates_rejected INTEGER,
    digest TEXT                       -- human-readable summary
);

CREATE TABLE dream_cycle_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES dream_cycle_runs(id),
    candidate_json JSONB,            -- the Thinker's full output
    operation TEXT,                   -- CREATE, UPDATE, SUPERSEDE
    target_memory_id UUID,           -- for UPDATE/SUPERSEDE
    schema_operation TEXT,            -- assimilation, accommodation
    evaluator_a_verdict TEXT,
    evaluator_a_reasoning TEXT,
    evaluator_b_verdict TEXT,
    evaluator_b_reasoning TEXT,
    evaluator_c_verdict TEXT,
    evaluator_c_reasoning TEXT,
    evaluator_d_verdict TEXT,         -- Methodologist (BFT 4th evaluator)
    evaluator_d_reasoning TEXT,
    final_verdict TEXT,               -- ACCEPTED or REJECTED (binary BFT consensus)
    created_memory_id UUID REFERENCES memories(id),
    user_rejected_at TIMESTAMPTZ,    -- if user rejects post-hoc
    user_rejection_reason TEXT,
    deferred_twice_rejected BOOLEAN DEFAULT FALSE,  -- legacy, always FALSE
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Relationship temporal awareness (Zep model)
ALTER TABLE memory_relationships ADD COLUMN expired_at TIMESTAMPTZ;
```

---

## Research Grounding Summary

| Design Element | Research Basis |
|---|---|
| Background processing during idle time | Stickgold 2005, Walker 2009 (sleep consolidation) |
| Novel recombination of memories | Stickgold 2005 (hippocampal replay) |
| Extraction of hidden rules/patterns | Walker 2009 (sleep enhances rule extraction) |
| Multiple retrieval passes strengthen memory | Roediger & Karpicke 2006 (testing effect) |
| Connecting new knowledge to existing | Dunlosky 2013 (elaborative interrogation) |
| Multi-agent consensus reduces hallucination | Yao et al. 2025 v2 (Roundtable Policy) |
| Diverse evaluation perspectives | Yao et al. 2025 v2 (diverse agents > homogeneous) |
| Binary BFT consensus (≥3/4) over unanimity | Lamport 1982, Castro & Liskov 1999 (PBFT quorum 2f+1) |
| Binary verdicts over weighted voting | Prasad & Nguyen 2025 (LLM confidence miscalibration) |
| Temporal juxtaposition strategy | Howard & Kahana 2002 (temporal context model) |
| Cross-project collision strategy | Tulving & Thomson 1973 (encoding specificity) |
| Depth framework for insight quality | Craik & Lockhart 1972 (levels of processing) |
| Proactive surfacing ("Tap on Shoulder") | Nate B Jones Open Brain architecture |
| Sleep-time compute paradigm | Letta 2025 (raw context → learned context) |
| Dream cycle consolidation | Engram (4-stage: score, dedup, extract, prune) |
| Reflection as first-class operation | Park 2023 (Generative Agents), Shinn 2023 (Reflexion) |
| Two-system learning (fast + slow) | McClelland et al. 1995/2016 (CLS theory) |
| Interleaving new with existing | McClelland et al. 2016 (CLS updated) |
| Schema assimilation vs accommodation | Bartlett 1932 (reconstructive memory) |
| Desirable difficulty surfacing | Bjork 1992 (storage vs retrieval strength) |
| Distillation loop prevents memory rot | Memvid architecture, multiple sources |
| Background LLM-driven consolidation | Google Always-On Memory Agent (Saboo 2026) |
| Meta-cognitive self-reflection | Shinn 2023 (Reflexion) |
| ADD/UPDATE/DELETE/NOOP on memories | Mem0 (arxiv 2504.19413) |
| Never delete, only invalidate | Zep/Graphiti (arxiv 2501.13956) |
| Memory refinement as continuous process | Cognee memify pipeline |
| Agent self-editing memory | Letta/MemGPT tiered memory |
| Proactive surfacing erodes trust if low quality | Microsoft Copilot backlash 2025-2026 |
| Random sampling as antidote to search bias | First principles (retrieval bias analysis) |

---

## Product Landscape — Convergence Pattern

Every serious memory system has converged on five operations:

| Operation | Cognee | Mem0 | Zep | Letta | Google | Ours |
|---|---|---|---|---|---|---|
| Ingest | add+cognify | Extraction | Episode ingest | Message proc | IngestAgent | ✅ ingest_content |
| Enrich | cognify 4-6 | Entity extract | Entity+rel | Core self-edit | IngestAgent | ✅ Three-speed (decided) |
| Consolidate | memify | ADD/UPDATE/DELETE | Contradiction detect | Archival promote | ConsolidateAgent | ✅ Dream cycle (this doc) |
| Retrieve | search (14) | Relevance | Temporal+semantic | Recall+archival | QueryAgent | ✅ hybrid_search+rerank |
| Reinforce | Usage-weighted | Implicit | Temporal recency | Self-editing | Consolidated | ✅ access_count+spacing |

---

## Crash Recovery (v2 optimization, deferred)

Weekly cadence + dream_cycle_candidates table providing implicit checkpointing
= the cost of a lost run is one week's delay. For a personal system, "log
warning, run fresh next week" is sufficient for v1. Full resume-from-checkpoint
is a v2 optimization if cadence increases.

---

## Resolved: Why DEFERRED Was Removed

**Status: DECIDED** (supersedes original "Deferred Insight Re-evaluation" decision)

The DEFERRED state was removed when the consensus panel was upgraded from 3
evaluators to 4 with binary BFT consensus. See "Byzantine Consensus Panel —
Design Rationale" above for the full three-stage design evolution.

**Summary:** With 4 evaluators and the BFT quorum at 3/4, there is no
threshold between ACCEPTED and REJECTED where DEFERRED would have a
trustworthy trigger. The Reflexion pattern (Shinn 2023) that DEFERRED enabled
requires a reliable signal that the candidate was genuinely close to good.
Under BFT worst-case analysis, 2/4 doesn't provide that signal — the
hallucinator may be one of the 2 accepts, meaning only 1 loyal evaluator
actually supports the insight.

**What replaced it:** Two lightweight signal preservation mechanisms:
1. Digest dissent annotation for non-unanimous accepts
2. Feedback injection of accepted dissent patterns

These provide the same learning loop without re-evaluation machinery, and
address systemic issues (patterns across candidates) rather than individual
cases.

**Legacy:** The `get_deferred_candidates()` and `mark_deferred_twice_rejected()`
functions are retained in `dream_cycle_db.py` for backward compatibility but
are no longer called by the orchestrator. The `deferred_twice_rejected` column
and `candidates_deferred` field remain in the schema but are always
FALSE/0 respectively.

---

## Resolved: Session-Start Briefing Frequency

**Status: DECIDED** (notification fatigue research; Human Factors 2023)

24-hour minimum gap AND (new memories OR dream cycle ran). Both conditions
must be true.

Run session-start briefing IF:
```
(new_memories_exist OR dream_cycle_ran_since_last_session)
AND (last_briefing_at < now() - interval '24 hours')
```

**Rationale:**
- Carnegie Mellon HCI (2022/2023): unsolicited notifications increase average
  task-resumption time by ~23 seconds and elevate cortisol, even when ignored
- AI-curated alerts have high false-positive rates — delivering no actionable
  value in the majority of instances
- The 24-hour floor aligns with daily work rhythm (one briefing per workday max)
- Prevents the "nagging assistant" pattern that eroded trust in Microsoft
  Copilot's proactive suggestions (2025-2026 backlash)
- If the user starts 3 sessions in one day, they get at most 1 briefing

---

## Resolved: Measuring Dream Cycle Effectiveness

**Status: DECIDED** (Letta Evals; MemoryArena Stanford 2025; TAME arxiv 2602.03224)

Three tiers of metrics, measured at different timescales.

### Tier 1: Process Metrics (per cycle, fully automated)
- Acceptance rate: accepted / (accepted + rejected)
- Acceptance rate trend over time (Explorer learning from feedback)
- Non-unanimous acceptance rate: fraction of accepted insights with 3/4 (not 4/4)
- Explorer strategy diversity (are all 10 strategies being used?)
- Cost efficiency: accepted insights per Kiro credit

### Tier 2: User Engagement Metrics (per digest, semi-automated)
- Digest read rate (proxy: session started within 24h of digest delivery?)
- User rejection rate: fraction of accepted insights rejected post-hoc
- User rejection reason clustering (e.g., "too obvious" = Panel bar too low)
- Insight citation rate: do dream-cycle-created memories get retrieved by
  `memory_search` in subsequent sessions? (measured via access_count)

### Tier 3: Downstream Impact Metrics (monthly, analysis script)
- Retrieval quality: golden query set run monthly, measure rerank scores.
  Golden queries are auto-generated from accepted insights' "Questions this
  answers" fields — the dream cycle creates both the knowledge and the test
  for that knowledge. Self-bootstrapping test suite.
  For each golden query, track:
  - **Rank position:** Did the dream-cycle insight appear at rank 1 vs rank 8?
  - **Co-results:** What else appeared alongside it? Ideal outcome: distilled
    semantic memory at #1, raw episodic sources at #3-5. If raw episodes still
    outrank the distillation, the distilled memory needs better keywords or
    "Questions this answers" section.
  - **Rerank score trend:** Run the same query monthly. Upward trend in top
    result's rerank score = knowledge base is getting better at answering that
    class of question.
  Implementation: a script that runs the golden query set against `memory_search`,
  logs results to a JSON file, and diffs against the previous month. Lightweight,
  longitudinal retrieval quality benchmark.
- Signal-to-noise ratio: fraction of top-10 search results that are semantic
  (distilled) vs. episodic (raw). Should trend toward more semantic over time.
- Knowledge graph density: average relationships per memory. Should increase.
- Consolidation coverage: fraction of episodic memories examined by at least
  one dream cycle. Tracks whether the cycle reaches the full memory space.

Implement Tier 1 first (fully automated, immediate feedback). Tier 2 requires
the digest UX. Tier 3 requires a monthly analysis script.

---

## Resolved: Digest Presentation Order

**Status: DECIDED** (Prasad & Nguyen 2025; arxiv 2401.13835)

No confidence-based ordering. Group insights by strategy type (contradiction,
unnamed principle, distillation, CLS interleaving, etc.) rather than sorting
by confidence.

**Rationale:**
- LLM confidence scores are systematically miscalibrated (Prasad & Nguyen 2025:
  72.9% average initial confidence vs. 50% rational baseline)
- Users overestimate LLM accuracy when given confidence indicators (arxiv
  2401.13835, UC Irvine: longer explanations increased user confidence even
  when accuracy didn't improve)
- Confidence-based ordering would amplify overconfidence bias — users trust
  first items more because they're labeled "high confidence"
- If the Thinker knows high-confidence insights get prominent placement, it
  has an incentive to inflate confidence (same gaming concern as the circuit
  breaker discussion)

The confidence field appears in the digest as informational context ("the system
rated this as medium confidence because the evidence base is thin") but does
not determine presentation order.

---

## References

[1] Craik & Lockhart 1972 — Levels of Processing Framework
[2] Roediger & Karpicke 2006 — Test-Enhanced Learning (https://pubmed.ncbi.nlm.nih.gov/16507066/)
[3] Howard & Kahana 2002 — Temporal Context Model
[4] Dunlosky et al. 2013 — Improving Students' Learning with Effective Learning Techniques
[5] Stickgold 2005 — Sleep-dependent memory consolidation (https://www.nature.com/articles/nature04286)
[6] McClelland et al. 1995/2016 — Complementary Learning Systems (https://pubmed.ncbi.nlm.nih.gov/27315762/)
[7] Bjork 1992 — Storage Strength and Retrieval Strength / New Theory of Disuse
[8] Bartlett 1932 — Schema Theory / Reconstructive Memory
[9] Shinn et al. 2023 — Reflexion: Verbal Reinforcement Learning (https://arxiv.org/abs/2303.11366)
[10] Park et al. 2023 — Generative Agents: Interactive Simulacra of Human Behavior
[11] Walker 2009 — Sleep-Dependent Memory Processing
[12] Tulving & Thomson 1973 — Encoding Specificity Principle
[13] Luo et al. 2025 — WBFT Consensus for Trusted Multi-LLM Networks (https://arxiv.org/abs/2505.05103)
[14] Yao et al. 2025 v2 — Confidence-Weighted-Consensus Aggregation (https://arxiv.org/abs/2509.16839)
[15] Chen et al. 2023 — Round-Table Conference: Consensus among Diverse LLMs (https://arxiv.org/abs/2309.13007)
[16] Letta 2025 — Sleep-time Compute (https://www.letta.com/blog/sleep-time-compute)
[17] Google Always-On Memory Agent (Saboo 2026) — (https://tech1media.com/2026/03/07/google-pm-open-sources-always-on-memory-agent-ditching-...)
[18] Nate B Jones — Open Brain architecture (https://natebjones.com)
[19] Engram — Dream Cycle consolidation (https://openengram.ai) — product reference, not peer-reviewed
[20] Memvid — Persistent Second Brain (https://memvid.com/blog/creating-a-persistent-second-brain-for-autonomous-ai-agents)
[21] Lilian Weng 2023 — LLM Powered Autonomous Agents (https://lilianweng.github.io/posts/2023-06-23-agent/)
[22] Lamport, Shostak, and Pease 1982 — The Byzantine Generals Problem (https://dl.acm.org/doi/10.1145/357172.357176)
[23] Castro and Liskov 1999 — Practical Byzantine Fault Tolerance (https://pmg.csail.mit.edu/papers/osdi99.pdf)
[24] Cognee Architecture — (https://topoteretes.com/blog/fundamentals/how-cognee-builds-ai-memory)
[25] Cognee Memify Pipeline — (https://topoteretes.com/blog/cognee-news/product-update-memify)
[26] Mem0 arxiv paper — (https://arxiv.org/abs/2504.19413)
[27] Mem0 & Mem0-Graph breakdown — (https://memo.d.foundation/research/breakdown/mem0)
[28] Zep/Graphiti arxiv paper — (https://arxiv.org/abs/2501.13956)
[29] Zep bi-temporal approach — (https://blog.getzep.com/beyond-static-knowledge-graphs/)
[30] Letta/MemGPT research — (https://rywalker.com/research/letta)
[31] Letta Sleeptime Agents — (https://forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154)
[32] Prasad & Nguyen 2025 — When Two LLMs Debate, Both Think They'll Win (https://arxiv.org/abs/2505.19184)
[33] Letta Sleeptime Agents Forum — (https://docs.letta.com/guides/agents/architectures/sleeptime/)
[34] Letta Evals — Evaluating Agents that Learn (https://www.letta.com/blog/letta-evals)
[35] MemoryArena — Stanford 2025 (https://arxiv.org/abs/2602.16313)
[36] TAME — Trustworthy Test-Time Evolution of Agent Memory (https://arxiv.org/abs/2602.03224)
[37] Calibration Gap between Model and Human Confidence (https://arxiv.org/abs/2401.13835)
[38] Carnegie Mellon HCI 2022/2023 — Notification recovery latency and attention residue
[39] Ebbinghaus 1885 / Leitner 1972 — Spaced repetition and forgetting curve
