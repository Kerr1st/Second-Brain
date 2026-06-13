# Fable 5 → Thinker Integration Plan (Dream Cycle)

> **Status: DRAFT.** Date: 2026-06-12 (rev. 2026-06-13). Phase 0a instrumentation shipped
> (commit `b4c3a7e`); the dream cycle runs healthy on the **Opus + MCP** path — the Jun 8–12
> outage was a launchd `cwd` bug, now fixed (see `docs/DREAM-CYCLE-MCP-DECOUPLING.md`).
> Apply Claude Fable 5 to the Second Brain **surgically and cost-consciously**, targeting
> only the dream-cycle **Thinker** agent. Fable work is gated on **B2** (the Thinker context
> packet), built only when pursuing Fable.

## TL;DR

- Put Fable 5 on the **Thinker only** (insight generation). Keep the **Explorer** and the
  **four evaluators** on the current kiro-cli / Opus 4.8 path.
- Estimated *added* cost **~$50–80/month** (daily batch, ≤5 Thinker calls/run) vs **~$380/month**
  if Fable 5 ran everywhere (the four evaluators would be ~80% of that).
- Ship behind a flag (default **OFF**), prove a quality lift with an A/B, then enable with a
  per-run cost cap + kill switch.

## Design decisions (locked 2026-06-13)

- **Orchestration stays deterministic Python** — no agentic LLM controller. (An agentic Fable
  orchestrator would reintroduce multi-turn/tool fragility and multiply cost.)
- **The 4-evaluator independent BFT consensus (≥3/4) is the quality gate, always.** No synthesis
  model — Fable included — evaluates its own output as the sole judge. Self-grading invites
  self-consistency bias; independent critics are how we ensure only the best insights reach the
  store.
- **Fable is confined to synthesis (the Thinker), and only B2 is needed for it.** The broader
  Explorer decoupling (**B1**) is **deferred** — the Jun outage was a launchd `cwd` bug (fixed),
  not intractable MCP fragility, so the agentic Explorer stays on the now-reliable MCP path
  (it's the path producing the cross-project/contradiction "crown jewels"). A *consolidated*
  Fable synthesis core remains an **optional** A/B arm, still gated by the independent panel.
  See `docs/DREAM-CYCLE-MCP-DECOUPLING.md`.

## The reframe that drives the cost plan

Today the dream cycle is effectively **$0 in metered Bedrock**: `agent_invoker.py` invokes
`kiro-cli chat --model claude-opus-4.8`, so every Explorer / Thinker / evaluator call runs
through the **Amazon Q service**, not this account's Bedrock. Kiro's model catalog does **not**
include `claude-fable-5` (verified — catalog is `auto, claude-opus-4.8/4.7/4.6/4.5,
claude-sonnet-4.6/4.5/4, claude-haiku-4.5, deepseek-3.2, minimax-*, glm-5, qwen3-coder-next`).

So adopting Fable 5 means **two** changes at once:

1. A **new direct-Bedrock invocation path** (kiro-cli cannot carry Fable 5), and
2. **New metered spend** at **$10 / $50** per 1M input/output tokens (~2× Opus 4.8's $5/$25).

Everything below follows from that: introduce Fable 5 only where reasoning quality most
changes the output, and where call volume is lowest.

## Agent profile review — why the Thinker is the right (and only) target

| Agent | What it does | Reasoning type | Calls/run | Tools | Verdict |
|---|---|---|---|---|---|
| **Explorer** | Assembles 0–5 "memory slices" via 11 scaffolded strategies + UCB1 diversity pressure | Curation/retrieval — heavily scripted; defers the thinking | 1 | MCP | Keep on Opus (Phase-2 candidate) |
| **Thinker** | Examines each slice deeply and **generates candidate insights** (unnamed principles, contradictions, CLS interleaving, distillation) — explicitly "NOT a summary" | **Generative synthesis** — deep, non-obvious, multi-hop | ≤5 | MCP | **Fable 5 ✓** |
| **Evaluators ×4** (Skeptic, Advocate, Epistemologist, Methodologist) | Score a candidate against a role checklist → ACCEPT/REJECT | Checklist judgment, protected by 4-way BFT consensus (≥3/4) | 4 × candidates (~48) | none | Keep cheapest viable — **never Fable 5** |

**Rationale:**

- The **Thinker** is the single stage where model reasoning depth becomes output quality — it
  is the "memory that thinks" payoff. It is also the lowest-volume LLM stage (≤5 calls/run).
  This is exactly Fable 5's strength (sustained, multi-stage, self-verifying reasoning).
- The **Explorer** is strategy-driven curation; the prompt scripts the work heavily and the
  real cognitive lift is deferred to the Thinker. A cheaper model does this well. Revisit only
  if Phase-1 shows slice quality (not insight reasoning) is the bottleneck.
- The **evaluators** are the call-volume hog (~48/run) and their **4-way BFT consensus
  deliberately tolerates a weaker per-judge model** — individual errors wash out at ≥3/4.
  Putting Fable 5 here is the worst cost/value trade in the system.

Non-LLM stages confirmed: `classify.py` and `depth.py` are **pure-deterministic** functions
(no model calls) — ingest-time work is already free and stays that way.

## Cost model (rough — replace with measured numbers in Phase 0)

Assumptions: 1 run/day, Thinker at `effort="high"`, ~12K input / ~8K output tokens per call,
≤5 slices. Fable 5 = $10/$50 per 1M in/out.

| Strategy | ~$/run | ~$/month | Note |
|---|---|---|---|
| **Thinker-only on Fable 5 (≤5 slices)** | ~$2.6 | **~$50–80** | The recommendation |
| Thinker-only, 3-slice cap | ~$1.6 | ~$48 | With slice cap |
| Fable 5 *everywhere* (incl. 48 evaluators) | ~$12.6 | ~$380 | ~80% is evaluators — avoided |

The dream cycle is a **once-daily batch** (not per-query, not per-ingest), so worst-case spend
is inherently bounded and controllable.

## Architecture changes (minimal, 3)

1. **Add a direct-Bedrock Fable 5 backend.** Reuse the boto3 pattern already in
   `src/embeddings.py` (this account is on Bedrock / us-west-2 today). Use
   `bedrock-runtime` `InvokeModel` with inference-profile ID **`global.anthropic.claude-fable-5`**
   (bare IDs are unsupported; CRIS/inference-profile only). Bake in the Fable 5 constraints
   (see below) and capture `usage` for per-call cost logging.

2. **Feed the Thinker a pre-assembled context packet → single-shot call.** Today the Thinker
   uses MCP tools (`effort="max"`); reproducing a tool loop around Fable 5 is avoidable. Have
   the orchestrator pre-fetch what the Thinker needs and pass it inline so the call is a single
   completion (bounded + meterable). The packet must cover the Thinker's tool-using modes:
   - slice memory **content** (not just IDs/titles),
   - **semantic neighbors** for CLS interleaving (mode #8),
   - **recent rejected candidates** for meta-cognitive reflection (mode #7).
   - *Alternative:* keep a **bounded** tool loop (cap tool calls) if pre-fetching proves to
     narrow the Thinker's live search too much. Decide in Phase 1.
   - **Scope (B2):** this packet is the *only* decoupling Fable needs. The Explorer stays
     agentic on the (cwd-fixed) MCP path; full Explorer decoupling (B1) is deferred. The cycle
     is healthy on Opus+MCP today, so build B2 only when pursuing Fable.

3. **Route only the Thinker to the Fable backend, behind a flag.** `AgentInvoker(model=...)`
   is already per-instance configurable. In `DreamCycleOrchestrator.__init__`, add a second
   invoker (e.g. `self.thinker_invoker`) used solely in `invoke_thinker`; Explorer and
   evaluators keep using the existing kiro-cli/Opus `self.invoker` unchanged. Gate the new
   invoker on a setting (default OFF).

## Guardrails & cost controls

- `effort="high"`, **not** `"max"` — Fable 5 at high reportedly exceeds prior models' xhigh;
  `max` balloons thinking-token output (billed as output at $50/1M).
- Hard `max_tokens` ceiling per Thinker call; cap slices to **≤3** when Fable is enabled.
- **Per-run cost budget** computed from Bedrock `usage`, with **auto-downgrade to Opus** if a
  run exceeds it.
- **Kill switch**: a settings flag (default OFF) that reverts the Thinker to kiro-cli/Opus
  instantly.
- **Refusal handling**: on `stop_reason: "refusal"`, fall back to Opus for that candidate
  (unlikely on the user's own memories, but cheap insurance).
- **Per-call token/cost logging** to `logs/` so the estimates above become measured numbers.

## Phased rollout

- **Phase 0 — instrument + baseline.** Add token/cost logging; record the current run's
  accept-rate and cross-project-insight count as the quality baseline. *(No-risk first step.)*
- **Phase 1 — build behind flag (OFF).** Fable backend + the **B2** Thinker context packet;
  then **A/B the Thinker** on the **same** slices across **several cycles** (not n=1):
  - **Arm 0 — Opus + tools** (today's agentic Thinker): the baseline.
  - **Arm 1 — Opus + packet** (tool-less, pre-fetched): **control** — isolates the
    access-pattern change (incl. the loss of CLS mode-#8 live neighbor search) so any delta is
    attributable to the *packet*, not the model.
  - **Arm 2 — Fable + packet**: isolates the *model* (vs Arm 1) — the real question.
  - **Arm 3 (optional) — consolidated Fable synthesis** (Explorer+Thinker collapsed).
  Compare **cross-project / contradiction insight rate** (not just accept count — that's the
  crown-jewel signal) and measured cost. The independent panel gates **all** arms.
- **Decision gate:** does Fable (Arm 2 vs Arm 1) measurably lift the crown-jewel rate enough to
  justify the ~$50–80/month? Only then →
- **Phase 2 — enable by default** with caps; optionally test Explorer-on-Fable.

## Open decision — data sharing (user's call)

The Thinker would send memory **content** to Fable 5 under `provider_data_share` (30-day
retention, Anthropic abuse review; the account opt-in is already set in us-east-1 + us-west-2).
This brain holds personal/work memories. If some are sensitive, either keep synthesis on
**Opus 4.8** (no data sharing) and reserve Fable 5 for non-sensitive runs, or accept the
sharing. **Not decided here.**

## Fable 5 technical constraints (bake into the backend)

- Model ID: `global.anthropic.claude-fable-5` (us-west-2, via inference profile) **or**
  `anthropic.claude-fable-5` on the Mantle Messages API (us-east-1). Bare ID on `InvokeModel`
  is **not** supported.
- **Adaptive thinking only.** Do **not** send `temperature` / `top_p` / `top_k` or
  extended-thinking budgets → they return HTTP 400. Control depth with `output_config.effort`.
- `content[0]` may be a **thinking block** — iterate content blocks by type; don't assume
  `content[0].text`.
- Handle `stop_reason: "refusal"` as a normal response path.
- Requires the account `provider_data_share` opt-in (already set).

## Out of scope / unchanged

- Explorer, the four evaluators, Express, embeddings (Titan), `classify`/`depth` — all stay as
  they are.
- This plan does **not** change capture, retrieval, storage, or the kiro-cli path for any stage
  other than the Thinker.
- Verified empirically before writing this: Fable 5 serves this account via both
  `bedrock-runtime` (us-west-2, `global.` profile) and the Mantle Messages API (us-east-1).
