# Fable 5 → Thinker Integration Plan (Dream Cycle)

> **Status: DRAFT.** Date: 2026-06-12 (rev. 2026-06-13). Phase 0a instrumentation shipped
> (commit `b4c3a7e`); the dream cycle runs healthy on the **Opus + MCP** path — the Jun 8–12
> outage was a launchd `cwd` bug, now fixed (see `docs/DREAM-CYCLE-MCP-DECOUPLING.md`).
> Cost + rollout rewritten 2026-06-13 from the **verified run** (`43ab9653`): measured cost,
> a transcript-mined packet spec, and a **1a/1b split**.
> Apply Claude Fable 5 to the Second Brain **surgically and cost-consciously**, targeting
> only the dream-cycle **Thinker** agent. Fable work is gated on **B2** (the Thinker context
> packet), built only when pursuing Fable.
>
> **Embedding update (2026-08-29):** ADR 0012 replaced the active Titan embedding path with local
> Ollama BGE-M3. References below to reusing `src/embeddings.py` as a Bedrock pattern are historical;
> a future direct-Bedrock reasoning Adapter must use the backend Module, not the active embedding
> Interface.

## TL;DR

- Put Fable 5 on the **Thinker only** (insight generation). Keep the **Explorer** and the
  **four evaluators** on the current kiro-cli / Opus 4.8 path.
- **Measured** added cost (verified run, char/4 estimates): **~$20/month** thinker-only
  (~$32 at a full 5 slices) vs **~$68/month** everywhere — far below the original $50–380 guess.
  Caveat: these count *visible* output only; Fable's thinking-token output is unmeasured and is
  the real driver (the 1b calibration call resolves it).
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

Today the dream cycle is effectively **$0 in metered Bedrock**: `backends/kiro.py` (`KiroInvoker`) invokes
`kiro-cli chat --model claude-opus-4.8`, so every Explorer / Thinker / evaluator call runs
through the **Kiro service**, not this account's Bedrock. Kiro's model catalog does **not**
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
| **Evaluators ×4** (Skeptic, Advocate, Epistemologist, Methodologist) | Score a candidate against a role checklist → ACCEPT/REJECT | Checklist judgment, protected by 4-way BFT consensus (≥3/4) | 4 × candidates (32 this run) | none | Keep cheapest viable — **never Fable 5** |

**Rationale:**

- The **Thinker** is the single stage where model reasoning depth becomes output quality — it
  is the "memory that thinks" payoff. It is also the lowest-volume LLM stage (≤5 calls/run).
  This is exactly Fable 5's strength (sustained, multi-stage, self-verifying reasoning).
- The **Explorer** is strategy-driven curation; the prompt scripts the work heavily and the
  real cognitive lift is deferred to the Thinker. A cheaper model does this well. Revisit only
  if Phase-1 shows slice quality (not insight reasoning) is the bottleneck.
- The **evaluators** are the call-volume hog (32 this run; 4 × candidates) and their **4-way BFT consensus
  deliberately tolerates a weaker per-judge model** — individual errors wash out at ≥3/4.
  Putting Fable 5 here is the worst cost/value trade in the system.

Non-LLM stages confirmed: `classify.py` and `depth.py` are **pure-deterministic** functions
(no model calls) — ingest-time work is already free and stays that way.

## Cost model (measured — verified run `43ab9653`, 2026-06-13)

Per-call **char/4 token estimates** from the instrumented run (3 Thinker slices, 8 candidates,
32 evaluator calls). Fable 5 = $10/$50 per 1M in/out.

| Stage | calls | est in | est out | Fable $/run |
|---|---|---|---|---|
| **Thinker** | 3 | 5,701 | 11,883 | **$0.65** |
| Evaluators ×4 | 32 | 49,523 | 19,131 | $1.45 |
| Explorer | 1 | 1,436 | 3,013 | $0.16 |

- **Thinker-only on Fable: ~$0.65/run → ~$20/month** (~$32 at a full 5 slices) — **3–5× below**
  the original $50–80 guess.
- **Fable everywhere: ~$2.26/run → ~$68/month** (not $380) — evaluators are still the volume, but
  the token totals are far smaller than assumed.

**The caveat that matters:** these are **char/4 estimates of *visible* output only** — the
kiro/Opus path exposes no `usage`, and **no thinking tokens at all**. Fable on direct Bedrock at
`effort=high` bills thinking as output at $50/1M, and that volume is **completely unmeasured** —
plausibly 2–10× the visible output. So **$20/mo is a floor**; the real driver is thinking-token
output, which only the **1b calibration call** can reveal. The dream cycle is a once-daily batch,
so worst-case spend stays bounded regardless.

## Architecture changes (minimal, 3)

1. **Add a direct-Bedrock Fable 5 backend.** Implement it in the model-backend Module. Use
   `bedrock-runtime` `InvokeModel` with inference-profile ID **`global.anthropic.claude-fable-5`**
   (bare IDs are unsupported; CRIS/inference-profile only). Bake in the Fable 5 constraints
   (see below) and capture `usage` for per-call cost logging.

2. **Feed the Thinker a pre-assembled context packet → single-shot call.** Today the Thinker
   uses MCP tools at `effort="max"`. **Transcript mining of the verified run (3 slices) is
   decisive:** tool use is **30 `memory_read` + 1 `memory_search` (of 31 calls)**, every read
   param a bare slice UUID, and **read counts never exceed member counts** (11/11, 7/13, 12/12).
   So the Thinker almost entirely **hydrates the slice's own member memories** — IDs the
   orchestrator already holds via `slice.memory_ids` — which is **deterministic and
   pre-fetchable**. *(Honest limit: member IDs aren't persisted yet, so for the 7/13 slice we
   can't confirm all 7 reads were members vs a neighbor — if one was, content-only's blind spot
   is slightly larger than the lone search. 1a's slice-composition persistence + the ablation
   settle it empirically either way.)* The packet, in layers:
   - **Content (the core — covers the hydration):** pre-hydrate the full text of
     `slice.memory_ids` — eager instead of lazy; the same material the Thinker reads anyway.
     Input inflates ~1.9K → ~8–26K est tokens, but it's cheap on input pricing.
   - **Neighbors (pre-registered — *not* "maybe"):** the lone `memory_search` was a cross-domain
     *neighbor reach* — the crown-jewel move (query: *"…unnamed cross-domain heuristic"*) — and
     it landed on **1 of 3 slices**, concentrated on neighbor-seeking strategies (#8 CLS, #11
     elaborative). Expect to add a semantic-neighbor layer; prove the need by ablation in 1a.
   - **Recent rejected candidates** for meta-cognitive reflection (mode #7) — a small, static add.
   - **Scope (B2):** this packet is the *only* decoupling Fable needs. The Explorer stays
     agentic on the (cwd-fixed) MCP path; B1 is deferred. Build B2 only when pursuing Fable.

3. **Route only the Thinker to the Fable backend, behind a flag.**
   > **Superseded (2026-06-16) by the Model Backends refactor.** The single `self.invoker`
   > is gone; the orchestrator now resolves an Invoker per role (`self.resolver` /
   > `_invoker_for(role)`), with effort from the active `config/backends.toml` profile.
   > Fable-on-the-Thinker becomes a **per-role profile entry** (a `(backend, model, effort)`
   > for the `thinker` row), not a hand-added `self.thinker_invoker`. The *intent* below
   > stands (route only the Thinker, gated, keep Explorer/evaluators on Opus); the mechanism
   > is obsolete.

## Guardrails & cost controls

- `effort="high"`, **not** `"max"` — Fable 5 at high reportedly exceeds prior models' xhigh;
  `max` balloons thinking-token output (billed as output at $50/1M).
- **Pin effort per A/B arm.** Today's Thinker runs at `effort="max"` (the metrics confirm it),
  so effort is a hidden third variable. Hold Arm 0 and Arm 1 at the **same** effort (clean
  packet test); run Arm 2 (Fable) at `high`. Record effort on every call (already done).
- **Timeout + latency.** Thinker calls already reach ~208s against the 300s cap; Fable thinking
  will be slower → **raise the Fable timeout** and treat **latency as a first-class A/B metric**,
  not just cost/quality.
- Hard `max_tokens` ceiling per Thinker call; cap slices to **≤3** when Fable is enabled.
- **Per-run cost budget** computed from Bedrock `usage`, with **auto-downgrade to Opus** if a
  run exceeds it.
- **Kill switch**: a settings flag (default OFF) that reverts the Thinker to kiro-cli/Opus
  instantly.
- **Refusal handling**: on `stop_reason: "refusal"`, fall back to Opus for that candidate
  (unlikely on the user's own memories, but cheap insurance).

## Phased rollout — 1a (free Opus) → 1b (Fable), gated

**Phase 0 — instrument + baseline. DONE** (`b4c3a7e`): per-call metrics in `logs/llm_metrics`.
The verified run gave the measured cost above and the transcript evidence for the packet spec.

**Phase 1a — packet de-risk on free Opus** (no Bedrock, no metered spend, no data-sharing). This
answers the riskiest question — *does a tool-less packet preserve crown-jewel quality?* — before
any Fable build. If the packet tanks quality, Fable can't save it, and we've spent nothing.

1. **Slice-composition persistence (prerequisite — corrects a gap).** `explorer_output` currently
   stores only a *summary* (`{name, strategy, memory_count}`), and the Explorer is
   non-deterministic — so historical slices are **not faithfully replayable**. First task: persist
   the full slice composition (member IDs + strategy + hypothesis) so a slice can be rebuilt and
   run *identically* across arms.
2. **Build a stratified corpus, then replay** (not "replay stored slices" — we don't have them).
   Run the Explorer in batch to accumulate ~30–50 slices, **over-weighting the crown-jewel-prone
   strategies** (`contradiction_hunting`, `cross_project_collision`, #11 elaborative neighbor-
   seeker) — they are both the rarest and the *only* stratum that can move the gate, so a random
   sample under-powers exactly what matters. **Reweight to the natural strategy mix**
   (`get_strategy_usage`) for the headline rate.
3. **Packet-builder:** start **content-only** (hydrate `slice.memory_ids`) — the minimal
   hypothesis the data supports.
4. **Arm 0 (Opus+tools) vs Arm 1 (Opus+content-only packet)** on the *identical* slices, **effort
   pinned equal**, **evaluated stratified by strategy** — so 2/3 clean-hydration slices can't mask
   a jewel loss on a neighbor slice (the crown-jewel-rate gate would otherwise hide it).
5. **Ablation ladder if Arm 1 underperforms on neighbor slices:** content-only → **content+neighbor**.
   If +neighbor recovers it → the dial is the *reach* (keep the neighbor layer). If it does **not**
   recover → the cause is **dilution** (force-feeding all members vs the model's chosen subset —
   the run showed 7/13 used) → the dial is **leaner hydration**, not more. This disentangles the
   two causes of a slice-2-type regression instead of reflexively blaming the missing neighbor.

**Phase 1b — Fable, only if the packet holds.** Now the expensive build + metered spend sit behind
a proven prerequisite.

1. **Calibration smoke-test (literal first step):** one scripted Fable call on a *real* packet —
   confirm inference-profile access, validate the no-temperature / thinking-block / refusal
   handling, and **measure actual thinking-token output** so the monthly cost stops being a guess.
   Bump the Thinker timeout for Fable.
2. **Direct-Bedrock Fable backend** (architecture #1) with `usage` capture — replaces the char/4
   estimates with real token counts.
3. **Arm 2 (Fable+packet, `effort=high`) vs Arm 1**, same slices, same stratified + weighted eval.
4. **Data-sharing decision gates 1b** (memory content → Fable; see below), with an optional
   "sensitive memories stay on Opus" carve-out.

**Judge protocol (decide before running).** Crown jewels are rare (1–2/run), so sparse rate
differences would need weeks — instead judge **blind pairwise per slice** (every slice is a data
point, far more power):

- **You are primary** on the replay sample.
- A **neutral-family LLM scaler** — a model in *neither* arm, randomized A/B order per pair —
  provides throughput, **validated against your 15% spot-checks**; if it doesn't track your taste,
  fall back to you-as-sole-judge.
- Plus an **absolute crown-jewel flag** per accepted insight (cross-project / contradiction), and
  the panel's existing accept-rate as a free coarse guardrail (Fable shouldn't accept *fewer*).

**Decision gate:** does Arm 2 beat Arm 1 on the weighted crown-jewel rate + pairwise preference,
enough to justify the measured spend (including the now-known thinking-token cost)? Only then →

**Phase 2 — enable by default** with caps + kill switch. **Arm 3 (consolidated Fable) and
Explorer-on-Fable are explicitly OUT of the initial A/B** — they reintroduce deferred-B1 risk and
muddy the Arm 1↔Arm 2 model comparison; revisit only if Arm 2 wins and we want to push further.

## Open decision — data sharing (user's call)

**This gates Phase 1b only — Phase 1a runs entirely on Opus and triggers none of it.** The Thinker
would send memory **content** to Fable 5 under `provider_data_share` (30-day retention, Anthropic
abuse review; the account opt-in is already set in us-east-1 + us-west-2). This brain holds
personal/work memories. If some are sensitive, either keep synthesis on **Opus 4.8** (no data
sharing) and reserve Fable 5 for non-sensitive runs (a tag-filtered carve-out on which slices are
Fable-eligible), or accept the sharing. **Not decided here.**

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
