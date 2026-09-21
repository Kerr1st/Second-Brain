# Research: model for the dream-cycle "skim" stage

Branch: `panel-acceptance`. Research date: 2026-09-19. Status: **research complete, no code changed, no model
invoked, no corpus read, zero spend.** Synthesised from three research reports (open-weight landscape, Bedrock-hosted
landscape incl. `/tmp/skim-bedrock-research.md`, integration-fit brief) plus read-only checks of this repo. Anything
not verified against a primary source or live output is marked **[unverified]**.

---

## 0. TL;DR

- **Primary:** Amazon Nova 2 Lite on Bedrock, `us.amazon.nova-2-lite-v1:0`, Flex service tier, extended reasoning off,
  schema-shaped JSON via forced tool use. **~$0.39/week** (~$0.31 for document tokens alone; ~$1.70/month).
- **Fallback (faithfulness-first, same adapter):** Google Gemma 3 12B IT on Bedrock, `google.gemma-3-12b-it`, Flex tier,
  native structured outputs, **~$0.09/week**. Quality fallback if both small models under-extract: Claude Haiku 4.5
  (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ~$2.16/week on-demand, ~$1.08 batch).
- **Reason:** Nova 2 Lite is within measurement noise of the best measured grounded-faithfulness score of any candidate
  the account can actually call (HHEM 5.1% vs Gemma 3 12B's 4.4%; Haiku 4.5 is 9.8%), it is first-party Amazon inside
  the user's own AWS account, has a 1M context, is authorised today with no IAM change, and costs about a fifth of the
  $2/week "ideal" — so the 20–30-document bake-off in §8 (total cost under $10) decides between it and Gemma 3 12B,
  not the price.
- **Kiro path:** admissible under the stated privacy constraint and zero-dollar, but **blocked today** — kiro-cli 2.22
  does not honour `--model` and Auto routing served claude-fable-5.1 (flagged "NOT FOR CUSTOMER DATA, ITAR OR PII") to
  the corpus, `model_honored=False` persists in metrics through 2026-09-19, KiroInvoker has no schema surface, and the
  plan is 55.5% consumed with 12 days left; so Kiro is a **synthetic-document pilot only** (`deepseek-3.2` 0.25×,
  `claude-haiku-4.5` 0.4×, est. 100–460 credits/week for the real workload **[unverified]**) until the §5 verification
  passes, and even then its credit shadow-cost (~$0.04/credit) exceeds Bedrock's dollar cost.

---

## 1. Problem statement and hard constraints

**Problem.** The Explorer (frontier model, Claude Sonnet 5 via Claude Code in profile `controlled`) currently sees the
arrivals since the previous run only as a manifest of counts plus 4 sample ids per channel (`MANIFEST_SAMPLES_PER_SOURCE
= 4`, `MANIFEST_MAX_CHARS = 7000`, `src/dream_cycle/allocation.py:48-49`). A new **skim** stage should read *every*
parent-level memory that arrived since the previous run and emit, per document, structured JSON metadata — people /
entities / projects, topic tags, decisions, open questions, novelty and contradiction flags, one-line summary — plus
cluster assignments, so the Explorer receives a complete ranked inventory. It is data processing, not insight
generation; the frontier models keep the thinking. The project's central quality problem is fabrication, so
**faithfulness of the extracted metadata matters most**, and the output must be reliable JSON.

**Workload (28-day average, per week).** 382 documents, ~931k input tokens of document text, ~115k output tokens.
Channels: articles 239 docs / 810k tokens (max 14k per doc); Slack 35 / 80k (p95 15k); kiro_cli_chat ~1 / 16k (max
25k); quick_desktop_feed 48 / 7k; email 12 / 6k; distilled_chat 32 / 4k; browser_history 2 / 4k; calendar 8 / 1k.
A ~1.2k-token instruction + schema prefix per document adds ~0.46M input tokens/week (so ~1.39M input total).
Needs ≥32k context (the 25k-token chat exports plus prompt). Latency is irrelevant (02:00 LaunchAgent). Runs Mon/Wed/Fri;
the skim may run nightly (~55 docs) or per run (~127 docs).

**Hardware / account.** Laptop: Apple M3 Pro, 18 GB RAM, no local inference runtime installed (verified by grep across
`src`, `docs`, `config`, `scripts`, `scheduling`, `.kiro`). AWS: profile `second-brain-jobs`, account 048588987132,
us-east-1; its only policy is `BedrockInvokeOnly` (InvokeModel + InvokeModelWithResponseStream on foundation-model/* and
inference-profile/*). Budget: per-run cap $9, daily Bedrock budget $10, ~$100/month alert as the signal; the skim should
cost well under $5/week, ideally <$2.

**Hard constraints.**
1. Privacy: the model must run inside the user's AWS account (Bedrock on-demand / batch / Custom Model Import), on the
   laptop, or on Amazon-operated Kiro infrastructure. Third-party model APIs are disqualified. Chinese open weights
   qualify only via Bedrock, Kiro or local.
2. Zero spend and no corpus content reads during research (honoured: all AWS calls were read-only list/describe
   calls; DB access was schema dumps and aggregate counts only).
3. Standing repo decision: "no Kiro on the corpus until `--model` is honoured" (`config/backends.toml:81-90`, commit
   `c5a63ac`, `docs/PLAN-panel-acceptance.md` §3, pinned by `tests/test_resolver.py:58-74`).

---

## 2. Decision criteria and weights

| # | Criterion | Weight | How measured here |
|---|---|---|---|
| 1 | Faithfulness / no fabrication | 30% | Vectara HHEM grounded-summarisation hallucination rate (leaderboard updated 2026-05-11, https://github.com/vectara/hallucination-leaderboard); confirmed by the §8 Astra grounding audit before commitment |
| 2 | Structured-JSON reliability | 15% | Bedrock native structured outputs (JSON Schema / strict tools) or constrained-decoding tool use > prompt-only JSON + validation + retry |
| 3 | Context ≥ 32k | 10% (threshold) | Model card context window; 25k-token docs + prompt must fit without chunking |
| 4 | Weekly cost, on-demand and Flex/batch | 10% | 0.931M in + 0.115M out at list prices (AWS Price List API, us-east-1, 2026-09-19); with-overhead figures in §4 |
| 5 | Availability / access in this account or Kiro plan | 10% | `get-foundation-model-availability` (39/39 AUTHORIZED); `kiro-cli chat --list-models` (2.22.1); lifecycle ("EOL no sooner than") |
| 6 | Throughput / quotas for ~1M tokens per night | 5% | Applied service quotas in the account |
| 7 | Operational simplicity | 10% | IAM/S3 changes, new client code, per-model request quirks, adapter fit |
| 8 | Privacy boundary | 10% | In-account + zero data retention (ZDR) by default; `us.*` not `global.*`; model-provider access; retention exceptions |

Extraction **recall** (does the model find every decision / person) has no public benchmark for this schema; it is
measured only by the bake-off (§8) and is the main reason the primary is a mid-size model rather than the smallest
faithful one. Cost is deliberately weighted low: every viable Bedrock candidate costs $0.02–$1.70/week on-demand, so it
does not discriminate.

---

## 3. Candidate table — everything considered

Weekly cost = document tokens only (0.931M in / 0.115M out), standard tier / Flex-or-batch tier (both 50% off standard
for Nova 2 Lite and third-party open weights; Claude has batch only; Nova Micro/Lite v1 have batch only). HHEM lower is
better. "SO" = native Bedrock structured-outputs flag on the model card. Access: all Bedrock rows returned
AUTHORIZED on 2026-09-19 unless noted.

### 3a. Bedrock (in-account) — viable

| Model (Bedrock id) | Path | Ctx / max out | JSON | HHEM | $/wk std | $/wk Flex or batch | Verdict |
|---|---|---|---|---|---|---|---|
| **Amazon Nova 2 Lite** `us.amazon.nova-2-lite-v1:0` | Bedrock on-demand (Flex) / batch | 1M / 64K | SO "no" on card; schema-valid JSON via forced tool use (constrained decoding per Nova docs) | 5.1% | $0.62 | **$0.31** | **Primary** — first-party, 1M ctx, launched 2025-12-02, 8M TPM |
| **Gemma 3 12B IT** `google.gemma-3-12b-it` | Bedrock on-demand (Flex) / batch | 128K / 8K | SO yes | **4.4%** | $0.12 | **$0.06** | **Fallback / co-candidate** — best measured faithfulness; 12B recall risk; EOL floor 2026-03-12 passed |
| **Claude Haiku 4.5** `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock on-demand / batch (no Flex) | 200K / 64K | SO yes (JSON Schema + strict tools) | 9.8% | $1.66 (global $1.51) | $0.83 batch | Quality fallback — best instruction following, already in `cost.py`; 2× the hallucination rate |
| **DeepSeek V3.2** `deepseek.v3.2` | Bedrock on-demand (Flex) / batch; also Kiro 0.25× | 164K / 8K | SO yes | 6.3% (Exp 5.3%) | $0.79 | $0.39 | Shortlist #4 — the only model on both Bedrock and Kiro (natural A/B); thinking must be off; 92.6% answer rate |
| Qwen3 Next 80B A3B `qwen.qwen3-next-80b-a3b` | Bedrock (Flex) / batch | 256K / 8K | SO yes | 9.3% (thinking variant; instruct unmeasured) | $0.27 | $0.13 | Shortlist #5 — cheap, 256K; EOL floor 2026-09-11 passed |
| Nemotron 3 Nano 30B-A3B `nvidia.nemotron-nano-3-30b` | Bedrock (Flex) / batch | 256K / 8K | SO yes | 9.6% | $0.08 | $0.04 | Cost fallback only (faithfulness mediocre); reasoning-by-default must be disabled |
| GLM 4.7 Flash `zai.glm-4.7-flash` | Bedrock (Flex) / batch | 203K / 4K | SO yes | 9.3% | $0.11 | $0.06 | Cost fallback only; 4K max output is enough for ~300-token records |
| Amazon Nova Micro `amazon.nova-micro-v1:0` | Bedrock on-demand / batch (no Flex) | 128K / 5K | tool-use JSON | 5.5% | $0.05 | $0.02 batch | Budget fallback for trivial channels only; Dec-2024 model, EOL floor Dec 2025 passed |
| Amazon Nova Lite v1 `amazon.nova-lite-v1:0` | Bedrock / batch (no Flex) | 300K / 5K | tool-use JSON | 6.1% | $0.08 | $0.04 | Dominated by Nova 2 Lite |
| Gemma 3 27B IT `google.gemma-3-27b-it` | Bedrock (Flex) / batch | 128K / 8K | SO yes | 7.4% | $0.26 | $0.13 | Worse HHEM than the 12B; no reason to prefer |
| MiniMax M2.5 `minimax.minimax-m2.5` | Bedrock (Flex); batch not on doc list; also Kiro 0.25× | 196K / 8K | SO yes | 9.1% | $0.42 | $0.21 | No advantage over Nova 2 Lite |
| MiniMax M2.1 `minimax.minimax-m2.1` | Bedrock (Flex) / batch; also Kiro 0.15× | 196K / 8K | SO yes | 11.8% | $0.42 | $0.21 | Excluded — faithfulness |
| GLM 4.7 `zai.glm-4.7` | Bedrock (Flex) / batch | 203K / 4K | SO yes | 11.7% | $0.81 | $0.41 | Excluded — worse than Flash, dearer |
| GLM 5 `zai.glm-5` | Bedrock (Flex); batch not on doc list; also Kiro 0.5× | 200K / 128K | SO yes | 10.1% | $1.30 | $0.65 | Excluded — overkill, 10% class |
| Qwen3 Coder 30B A3B / Coder Next | Bedrock (Flex) / batch; Coder Next also Kiro 0.05× | 256K / 16K | SO yes | unmeasured | $0.21 / $0.60 | $0.10 / $0.30 | Excluded — code-tuned, wrong fit for prose extraction |
| Nemotron 3 Super 120B `nvidia.nemotron-super-3-120b` | Bedrock (Flex); batch not on doc list | 256K / 32K | SO yes | unmeasured | $0.21 | $0.11 | Not shortlisted — unmeasured, no advantage |
| Nova Pro / Nova Premier | Bedrock (Flex) / batch | 300K–1M | tool-use JSON | 5.1% / n/a | $1.11 / $3.77 | $0.56 / $1.88 | Dominated by Nova 2 Lite on price |

### 3b. Bedrock — excluded

| Model | Path | Reason |
|---|---|---|
| gpt-oss-20b / 120b `openai.gpt-oss-*` | Bedrock (Flex) / batch | HHEM 14.2% (120b); OpenAI's own card reports 49–53% PersonQA hallucination; reasoning-native. Cheap ($0.05–0.10/wk) but wrong for a fabrication-sensitive stage |
| Qwen3 32B `qwen.qwen3-32b-v1:0` | Bedrock (Flex) / batch | HHEM 5.9% but **32K context** — too tight for the 25k-token chat exports plus prompt |
| Qwen3 235B A22B 2507 | Bedrock | Not in the us-east-1 catalog (us-east-2 / us-west-2 only) |
| Qwen3 VL 235B | Bedrock | Vision model, no SO flag |
| Kimi K2.5 / K2 Thinking `moonshotai.*` | Bedrock (Flex) / batch | HHEM 14.2%; thinking variant output-heavy |
| Kimi K3 `moonshotai.kimi-k3` (GA 2026-09-18) | Bedrock inference profile | Frontier-priced ($4.52–4.97/wk), no batch, wrong tool for triage |
| DeepSeek R1 | Bedrock profile | Reasoning model, HHEM 11.3%, legacy pricing |
| Llama 4 Scout / Maverick, Llama 3.3 70B | Bedrock profile / batch | **No structured-outputs flag** on the cards; 4–8K outputs; Llama 3.3 70B's 4.1% HHEM is tempting but it is a Dec-2024 model with no Flex |
| Ministral 3 3B/8B/14B, Mistral Large 3, Magistral Small | Bedrock (Flex) / batch | HHEM 19–24% (Ministral), 14.5% (Large 3); Magistral is a reasoning model |
| Gemma 4 26B-A4B / 31B `google.gemma-4-*` | Bedrock via `bedrock-mantle` OpenAI-compatible endpoint only | **Reports disagree** (see §3f): documented and priced ($0.13–0.14/$0.40), HHEM 5.2% for 26B-A4B, but **absent from the us-east-1 `list-foundation-models` catalog on 2026-09-19**; would need a new OpenAI-SDK-style client; structured outputs on mantle **[unverified]**. Re-check before the bake-off; if callable it likely displaces Gemma 3 12B |
| Writer Palmyra X5 | Bedrock profile | No discount tiers, 10 RPM quota |
| Claude Sonnet 5 / Opus 5, GPT-6 Astra, GPT-5.6 Sol/Terra/Luna | Bedrock profile | Frontier cost ($3.31/wk and up); GPT-6 has flagged-traffic 30-day retention; these stay in the Explorer / Thinker / panel seats |
| Claude Fable 5 / 5.1 | Bedrock profile / Kiro | **ALL traffic retained up to 30 days with potential AWS human review** (https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html); Kiro labels them "NOT FOR CUSTOMER DATA, ITAR OR PII" |

### 3c. Bedrock Custom Model Import (CMI)

| Model | Path | Reason |
|---|---|---|
| Qwen3-8B / 14B / 32B (Qwen3ForCausalLM), gpt-oss | CMI (us-east-1) | Importable, but $0.05718 per Custom Model Unit-minute + $1.95/model/month → a 1-hour weekly Qwen3-14B run ≈ **$3.4/week**, dearer than serverless; no batch; no Converse for Qwen3; new IAM + S3. Only if a specific dense Qwen3 checkpoint is required |
| Qwen3.5 / 3.6 / Qwen3-Next, Gemma 3/4, DeepSeek V3/V4, Kimi, GLM, MiniMax, Nemotron 3 | CMI | Unsupported architectures (Gated-DeltaNet / Mamba hybrids, etc.) — not importable |

### 3d. Kiro CLI (Amazon-operated infrastructure; credits, not dollars)

Verified with `kiro-cli chat --list-models` on 2026-09-19 (kiro-cli 2.22.1, IAM Identity Center login, plan
"KIRO POWER", 10,000 credits/month, 5,548.60 used = 55.5%, resets 2026-10-01).

| Model id | Multiplier | Ctx | Verdict |
|---|---|---|---|
| `deepseek-3.2` ("experimental preview") | 0.25× | 164K | **Pilot candidate #1** — same weights as Bedrock `deepseek.v3.2` (HHEM 6.3%), so it is the clean Kiro-vs-Bedrock A/B |
| `claude-haiku-4.5` | 0.40× | 200K | **Pilot candidate #2** — best instruction following of the cheap set; HHEM 9.8% |
| `minimax-m2.5` | 0.25× | 196K | Possible; HHEM 9.1%, no advantage over the two above |
| `minimax-m2.1` | 0.15× | 196K | Excluded — HHEM 11.8% |
| `glm-5` | 0.50× | 200K | Excluded — dearer credits, HHEM 10.1% |
| `qwen3-coder-next` (preview) | 0.05× | 256K | Cheapest credits by far but code-tuned and unmeasured; keep as a curiosity in the pilot only |
| `agi-nova-beta-1m` ("[Internal] AGI Nova SWE Beta") | 0.01× | 1M | Excluded — internal beta, data-handling terms unknown **[unverified]** |
| `claude-fable-5.1` / `claude-fable-5` ("[Internal] … NOT FOR CUSTOMER DATA, ITAR OR PII") | 6.0× / 4.4× | — | **Excluded** — PII label; the corpus contains email, calendar, Slack |
| `auto` | 1.0× | — | **Excluded** — this is what routed the corpus to Fable 5.1 on 2026-09-17 |
| `claude-sonnet-5` 1.3×, `claude-opus-5` 2.2×, `gpt-5.6-*` 1.1–4.4×, `claude-opus-4.x`, `claude-sonnet-4.x` | — | — | Excluded — frontier credits for a triage job |

All Kiro rows share the blockers in §5: `--model` not honoured, no schema surface, credits invisible to `SpendTracker`.

### 3e. Local (M3 Pro, 18 GB) and third-party APIs

| Model | Path | Verdict |
|---|---|---|
| Gemma 3 12B IT Q4_K_M (~7 GB) | local, llama.cpp Metal | **Best local option** — HHEM 4.4%, fits with 32K ctx + q8 KV cache; ~3.4 h/week compute; see §6 |
| Qwen3-8B Q4 (~5 GB) / Qwen3-14B Q4 (~8–9 GB) | local | Fit / tight; HHEM 4.8% / 5.4% |
| Qwen3.5-9B Q4 (~5.5 GB) | local | Fits; faithfulness unmeasured and the Qwen3.5 family trends worse (27B 12.1%, 35B-A3B 10.5%) |
| gpt-oss-20b (~12 GB), Gemma 4 26B-A4B (~15 GB), Qwen3(.5)-30/35B-A3B (~17–19 GB) | local | Do **not** fit under the ~11 GB (60%) weight budget |
| Qwen3.5/3.6/3.8, DeepSeek V4 Flash/Pro, Kimi K2.6/K3 weights, GLM 5.1/5.3, MiniMax M2.7, Nemotron 3.5 Lightning | none qualifying | Not on Bedrock, not CMI-importable, too large for the laptop → unreachable under the constraints (only Qwen3.5-9B fits locally) |
| DeepSeek API, Moonshot API, Alibaba DashScope, Zhipu/Z.ai API, MiniMax API, OpenAI API, Google AI Studio | third-party API | **Disqualified** by hard constraint 1 |

### 3f. Where the reports disagree (and what this document takes as true)

| Topic | Open-weight report | Bedrock report | Taken here |
|---|---|---|---|
| Kiro admissibility | "Legitimate zero-dollar path; pilot/fallback" | "Disqualified — traffic transits Amazon's Kiro service, not the user's account" | The task brief explicitly admits Amazon-operated Kiro infrastructure, so the **path** is admissible; the live blocker is that the **served model** cannot be guaranteed today (§5). Integration brief is neutral and correct on mechanics |
| Gemma 4 on Bedrock us-east-1 | Listed (launched 2026-06-10, mantle-only endpoint) | Not in the `list-foundation-models` catalog on 2026-09-19 | Prefer the live catalog: **not callable via Converse today**; mantle route **[unverified]**. Not a first-pass candidate |
| Batch + structured outputs | "Works in batch without additional setup" (structured-output page) | AWS batch overview page says batch does **not** support tool calling / structured output | Contradictory AWS docs → **[unverified]**; sidestep by using the **Flex tier** (same 50% discount, keeps structured outputs, no S3/IAM, no 100-record minimum) |
| Primary recommendation | Gemma 3 12B (faithfulness-first) | Nova 2 Lite (first-party, capability, 1M ctx) | Nova 2 Lite primary, Gemma 3 12B fallback, bake-off decides (§7) |
| Haiku 4.5 weekly cost | $1.51 / $0.75 (global profile) | $1.66 / $0.83 (us. profile) | Both correct; use `us.*` for the privacy boundary → $1.66 / $0.83 |
| MiniMax M2.5, GLM 5, Nemotron Super batch | Price-list SKUs exist | Not on the batch-support page; quotas exist | **[unverified]**; irrelevant once Flex is used |

---

## 4. Shortlist deep-dive

Weekly costs below include the ~1.2k-token prompt prefix per document (1.39M input / 0.115M output per week).

### 4.1 Amazon Nova 2 Lite — `us.amazon.nova-2-lite-v1:0`, Flex tier (~$0.39/week; $0.77 standard)

- Faithfulness: HHEM 5.1% (tied with Nova Pro; better than Haiku 4.5 9.8%, gpt-oss-120b 14.2%). Knowledge cutoff
  Oct 2025. Launched 2025-12-02, so no lifecycle risk in the near term.
- Context 1M / 64K output: no document ever needs chunking; the 25k-token chat exports are trivial.
- JSON: the card's native structured-outputs flag is "no", but the Nova docs state tool use is served by constrained
  decoding, so a **forced** tool choice (`toolChoice: {"tool": {"name": "structured_output"}}`) yields schema-valid JSON
  (https://docs.aws.amazon.com/nova/latest/userguide/concept-chapter-servicename.html, per the Bedrock report;
  **[unverified live]**). The repo's `BedrockInvoker._shape_structured_output` currently uses `toolChoice: {"auto": {}}`
  (`src/backends/bedrock.py:176`, forced choice was avoided because Fable 5.1 rejected it) → needs a per-model switch.
- Reasoning: Nova 2 Lite has extended reasoning; the repo's `_map_effort_to_model_fields` returns `None` for `amazon.*`
  ids (`bedrock.py:278-280`), so nothing is sent. Whether reasoning is off by default on Converse is **[unverified]**
  and must be checked in the bake-off (reasoning roughly doubles hallucination on HHEM for other families).
- Access / quota: `INFERENCE_PROFILE` only (call the `us.` id); AUTHORIZED; 8M TPM cross-region, 2,000 RPM.
- Privacy: first-party Amazon weights, `us.*` keeps traffic in us-east-1/2 and us-west-2, ZDR by default. The most
  defensible posture for Amazon-internal Slack/email content flowing through a personal AWS account.
- Cost accounting: **must** be added to `_PRICE_TABLE` (`src/backends/cost.py:36-48`) — an unknown id is priced at the
  $10/$50 ceiling (`known=False`), which at skim volume books ≈ $15/week and trips any cap on the first night.
- Sources: model card https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html ;
  Bedrock pricing page (Amazon Nova tab) and Price List API, resolved 2026-09-19.

### 4.2 Google Gemma 3 12B IT — `google.gemma-3-12b-it`, Flex tier (~$0.09/week; $0.16 standard)

- Faithfulness: HHEM **4.4%**, the lowest of any structured-output-capable model in the catalog. Non-reasoning model,
  so no thinking tax to disable.
- Context 128K / 8K output — enough. Converse-native, structured outputs "yes", batch and Flex "yes", 100M TPM quota.
- Risks: a 12B dense model may under-extract nuanced decisions / open questions and produce coarser tags (recall is
  what §8 measures); AWS card "EOL no sooner than 2026-03-12" has **passed**, so Legacy status could be announced any
  time (≥6-month legacy period, so not an outage, but a reason to keep a second model configured); Gemma Terms of Use
  (accepted via Bedrock).
- Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-12b-it.html ; Price List API.

### 4.3 Claude Haiku 4.5 — `us.anthropic.claude-haiku-4-5-20251001-v1:0` (~$2.16/week on-demand; ~$1.08 batch)

- Strongest instruction following and tool discipline of the affordable set; native JSON Schema structured outputs and
  strict tools — the most reliable JSON path available. Already priced in `cost.py` (`claude-haiku-4-5`, $1/$5) and
  served by the existing adapter (the 4.x `budget_tokens` mapping for Haiku is not live-verified in this repo).
- Faithfulness: HHEM **9.8%**, roughly 2× Nova 2 Lite — the prompt must forbid inference ("null when absent") and the
  Explorer should treat entity/decision fields as hints.
- No Flex tier for Claude; the discount needs batch (≥100 records/job → per-run or weekly cadence, plus IAM/S3 work)
  or prompt caching ($0.11/M cache reads brings on-demand back under $2/week). `us.` is 1.1× the `global.` price.
- Also reachable on Kiro at 0.4× credits (§5). Use if the bake-off shows both small models missing decisions/people.
- Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html ;
  https://docs.anthropic.com/en/docs/about-claude/pricing .

### 4.4 DeepSeek V3.2 — `deepseek.v3.2`, Flex tier (~$0.54/week; $1.07 standard); also Kiro `deepseek-3.2` 0.25×

- HHEM 6.3% (V3.2-Exp 5.3%); 164K context; native structured outputs; Flex + batch; 100M TPM. MIT licence.
- Risks: must run **non-thinking** (R1-style reasoning triples hallucination to 11.3%); 92.6% answer rate on HHEM
  (more refusals / empty fields); Chinese-origin weights hosted in AWS-owned deployment accounts — satisfies the
  constraint (providers "don't have access … to customer prompts and completions",
  https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html) but the Bedrock report asks for explicit
  user sign-off. The only shortlisted model on both Bedrock and Kiro, hence the natural A/B for §5.
- Source: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-deepseek-deepseek-v3-2.html .

### 4.5 Qwen3-Next-80B-A3B — `qwen.qwen3-next-80b-a3b`, Flex tier (~$0.17/week; $0.33 standard)

- 256K context, structured outputs + batch, Apache 2.0, 100M TPM; the strongest Qwen actually hostable in-account.
- Risks: faithfulness measured only for the *thinking* variant (9.3%); card says "Reasoning: Supported" → disable and
  validate; EOL floor 2026-09-11 has passed. Third choice behind the two above; a bake-off seat only if Nova and Gemma
  both disappoint.
- Source: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-qwen-qwen3-next-80b-a3b.html **[URL form
  unverified]**; Price List API.

---

## 5. Kiro path assessment (per the steering: "models you have access to")

**What is available (live, 2026-09-19).** `kiro-cli chat --list-models` on kiro-cli 2.22.1 exposes 21 models; the
non-frontier, non-internal ones are `claude-haiku-4.5` (0.4×), `deepseek-3.2` (0.25×), `minimax-m2.5` (0.25×),
`minimax-m2.1` (0.15×), `glm-5` (0.5×), `qwen3-coder-next` (0.05×). Plan: KIRO POWER, 10,000 credits/month,
5,548.60 used (55.5%) on day 19, resets 2026-10-01; org-managed, overage $0.04/credit only if enabled
(https://kiro.dev/blog/new-pricing-plans-and-auto/ , https://kiro.dev/docs/billing/related-questions/).

**Privacy boundary.** The task brief admits "Amazon-operated Kiro infrastructure", so the path qualifies. The Bedrock
report's stricter reading ("must be the user's own account") is noted as a disagreement (§3f) but is not the brief's
rule. What does *not* qualify is the model Kiro actually serves when `--model` is ignored: Fable 5.x is labelled by Kiro
"NOT FOR CUSTOMER DATA, ITAR OR PII", and on Bedrock the same models retain all traffic 30 days with potential human
review. `docs/MODEL-BACKENDS.md` "Data boundaries" records that Kiro routes content to the Kiro service, not the user's
AWS account.

**Credits per week for the real workload (382 single-document calls).** Kiro publishes no token→credit formula; credits
are billed "based on the complexity of the prompt" times the model multiplier. Working assumption: a single-turn,
tool-less, 2–25k-token extraction costs 1–3 base credits at 1.0× **[estimate, unverified — measure in the pilot]**.

| Kiro model | Multiplier | Credits/week (382 calls) | Credits/month (×4.33) | Share of 10,000 plan | Shadow $ at $0.04/credit |
|---|---|---|---|---|---|
| `qwen3-coder-next` | 0.05× | 19–57 | 83–248 | 0.8–2.5% | $0.76–2.29 |
| `deepseek-3.2` | 0.25× | 96–287 | 414–1,240 | 4.1–12.4% | $3.82–11.46 |
| `minimax-m2.5` | 0.25× | 96–287 | 414–1,240 | 4.1–12.4% | $3.82–11.46 |
| `claude-haiku-4.5` | 0.40× | 153–458 | 662–1,985 | 6.6–19.9% | $6.11–18.34 |
| `glm-5` | 0.50× | 191–573 | 827–2,481 | 8.3–24.8% | $7.64–22.92 |

Against remaining headroom: 4,451 credits remain for the last 12 days of September; the user's own pace so far is
~292 credits/day (5,548.6 / 19), projecting ~3,500 more before the reset and leaving ~950 credits of slack. A Haiku skim
for the rest of the month (≈1.7 weeks × 153–458) would consume 27–82% of that slack. From October a Haiku skim would
take 7–20% of the monthly plan. **The credits are only "free" if they would otherwise go unused**; at the overage
price the Kiro path costs 10–50× the Bedrock Flex path ($0.09–0.54/week) for the same model classes.

**Throughput.** One `kiro-cli chat --no-interactive` subprocess per document, ~15–20 s overhead each
(`src/dream_cycle/orchestrator.py:92`): 382 × ~17.5 s ≈ **1.9 h/week of pure session overhead** plus inference
(~2–2.5 h/week serial in total; nightly ~55 docs ≈ 20–25 min). Acceptable at 02:00. Packing several documents into one
call would cut overhead but raises cross-document attribution errors — exactly the fabrication mode this stage exists to
avoid — so keep one document per call (or tiny groups with strict per-document ids).

**Blockers, with the evidence that would lift each.**

(a) **`--model` not honoured.** `config/backends.toml:81-90` (2026-09-17): kiro-cli 2.22 answered "failed to set
model: Method not found" and server-side Auto served claude-fable-5.1 to the Explorer and Thinker, which self-reported
"this session is running on claude-fable-5.1, which is not approved for PII" and began self-censoring; commit `c5a63ac`
moved the corpus off Kiro; aggregated `logs/llm_metrics` show `model_honored=False` on every Kiro call dated 2026-09-17,
-18 and -19 (the `distill` stages still run on Kiro requesting `claude-sonnet-5`). Whether 2.22.1 honours `--model` for
the cheap ids was **not tested** (no calls made). Note the metric is inferred from the *absence* of the stderr string
`failed to set model '…'` (`src/backends/agentic_cli.py:237-247`) — it is necessary, not sufficient, evidence.
Evidence that lifts the block, all on **synthetic, non-PII documents**:
  1. `kiro-cli --version` newer than 2.22.1 whose release notes mention the model-selection fix **[to check]**; or the
     stderr message gone on 2.22.1.
  2. ≥10 single-shot calls each with `--model deepseek-3.2` and `--model claude-haiku-4.5`: `model_honored=True` on
     100% of them across three consecutive days.
  3. A **positive** served-model signal, since (2) is only the absence of an error: `/usage` credit delta for the 10
     calls must scale with the multiplier (10 calls at 0.25× ≈ one quarter of 10 identical Auto calls); per-model
     breakdown on the Kiro usage page if it exists **[unverified]**; raw outputs grepped for "fable" / "not approved
     for PII" self-disclosures (the exact signal that exposed the 2026-09-17 routing).
  4. Then relax the `controlled` pins deliberately for the skim role only (`tests/test_resolver.py:58-74`) and record
     the decision in `docs/PLAN-panel-acceptance.md` §6. Until then the standing rule stands.

(b) **No schema / structured-output surface.** `KiroInvoker.invoke` (`src/backends/kiro.py:108-121`) has no `schema`
kwarg (`invoke() got an unexpected keyword argument 'schema'`); capabilities are `structured_output=False,
reports_usage=False, metered=False` (`src/backends/base.py:159-161`). JSON must be prompt-enforced, recovered with the
shared `parse_json_output` backstop (`base.py:258`), validated with `jsonschema` (4.26.0 is in the venv, unpinned), and
retried once with the validation error fed back. Expect a lower first-try validity rate than Bedrock's constrained
decoding (~100% shape compliance); §8 measures it.

(c) **Throughput and credits** — as above: ~2–2.5 h/week serial, 100–460 credits/week for the two pilot models
**[estimate]**, versus ~950 credits of slack this month and the user's interactive use.

(d) **Metering.** `_extract_usage` returns `(None, "estimate")` (`kiro.py:210-215`), so `_record_spend`
(`orchestrator.py:135-137`) skips Kiro calls: Kiro spend is credits, invisible to `SpendTracker`, and the metrics JSONL
carries only char/4 estimates. To account for a Kiro skim: store `credits_est = calls × multiplier × measured
credits-per-call` (from the pilot) per row in `memory_skim`; read `/usage` before and after each job (a non-interactive
way to read usage is **[unverified]**); add a job guard that refuses to run when remaining credits < a reserve the
user sets (e.g. 1,500); and, for comparability with the dollar caps, log a shadow cost at $0.04/credit. `_PRICE_TABLE`
matching (`claude-haiku-4.5` already matches the $1/$5 row) does not help because usage is `None`.

**Verdict.** Kiro is a first-class *pilot* path today and a possible redundancy path later, not the production skim:
even with `--model` proven, it lacks constrained decoding and real usage, and its credit shadow-cost exceeds Bedrock's
dollar cost. Run the §8 Kiro protocol on synthetic documents now (zero dollars, ~20–60 credits); revisit after the
2026-10-01 reset.

---

## 6. Local-inference assessment (Apple M3 Pro, 18 GB, nothing installed)

- Hardware: 14-GPU-core, 150 GB/s part. llama.cpp Apple-Silicon benchmarks (ggml-org/llama.cpp discussion #4167, M3
  Pro rows) give ~270 tok/s prompt processing and ~31 tok/s generation for a 7B Q4_0 (pp512/tg128); scaled: 9B ≈ 210 /
  24, 12B ≈ 155 / 18; long 15–25k contexts degrade prefill further.
- Weekly compute for the workload: 9B ≈ 2.6 h, 12B ≈ 3.4 h → **3–5 h/week (25–45 min/night)**; a 25k-token document
  alone takes ~160 s of prefill on a 12B. Feasible for the 02:00 LaunchAgent only if the laptop is awake.
- Memory: keep weights ≤ ~11 GB (60% rule). Gemma 3 12B Q4_K_M ~7 GB and Qwen3-8B Q4 ~5 GB fit with 32K context (q8
  KV cache); Qwen3-14B Q4 ~8–9 GB is tight; gpt-oss-20b (~12 GB), Gemma 4 26B-A4B (~15 GB) and the 30/35B-A3B MoEs
  (~17–19 GB) do not fit. Postgres and the capture API share the same RAM.
- Runtime: llama.cpp `llama-server` (Metal) or Ollama — both support JSON-Schema constrained decoding (100% parseable
  output). Ollama's MLX backend requires 32 GB, so use llama.cpp Metal. The venv is Python 3.14.3, which constrains
  wheel availability for any Python inference package — verify per package.
- Faithfulness: Gemma 3 12B (4.4%), Qwen3-8B (4.8%), Qwen3-14B (5.4%) are the measured local options; quantisation
  quality is a risk (Q4_K_M ≈ +3% perplexity) and no HHEM figure exists for quantised variants.
- Codebase fit: no local backend exists (`DEFAULT_ADAPTERS` = kiro, claude_code, codex, bedrock,
  `src/backends/resolver.py:35-40`). One would subclass `DirectApiInvoker` (`src/backends/direct_api.py:51`), override
  `_invoke_provider` / `_extract_raw` / `_extract_usage` / `_map_exception` / `_shape_structured_output`, register in
  `DEFAULT_ADAPTERS` and `BACKEND_CAPABILITIES`, and add a `$0/$0` `_PRICE_TABLE` entry (otherwise the ceiling price
  applies). Operationally it is another `KeepAlive` LaunchAgent like `com.second-brain.capture-api`.
- Dollar cost $0; ops cost: install, 5–9 GB download, keep-awake, memory contention, and a new backend to maintain.
- **Verdict:** viable zero-egress fallback (Gemma 3 12B Q4_K_M via llama.cpp), not the primary. Revisit if Bedrock
  becomes unavailable, if a channel must never leave the laptop, or if a host with more RAM arrives (the Mac mini
  migration is historical only per `docs/MAC-MINI-MIGRATION.md`).

---

## 7. Recommendation, decision rule, risks

**Run now.** Build the skim on Bedrock Flex with **Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`) as the configured model
and **Gemma 3 12B IT** as the configured fallback, both with reasoning off, temperature 0, one document per call, schema
validated after parsing. Expected cost ~$0.39/week (Nova) or ~$0.09/week (Gemma), i.e. under 1% of the monthly spend.
Run the §8 bake-off (Nova 2 Lite, Gemma 3 12B, Haiku 4.5; total under $10, on a non-run day) **before** wiring the
inventory into the Explorer.

**Why Nova 2 Lite over Gemma 3 12B as primary.** The faithfulness gap (5.1% vs 4.4%) is within the leaderboard's noise;
the capability gap (12B dense vs a 1M-context first-party model with 64K output) bears directly on recall of decisions
and open questions, which is what makes the inventory "complete"; Nova is fresher in lifecycle (Gemma 3's EOL floor has
passed); and first-party weights inside the user's account are the cleanest privacy story for Amazon-internal content.
If the bake-off shows Gemma 3 12B equal on recall, switch to it — it is cheaper and more faithful on paper.

**Decision rule.**
1. Disqualify any candidate with a zero-blocking-issue rate < 90% on the Astra grounding audit (§8.2) or first-try JSON
   validity < 99% under constrained decoding (§8.3).
2. Among survivors require recall@40 = 4/4 for the `3a1263a6` sources (§8.1); if only one model reaches it, pick it.
3. Ties → lower blocking-issue rate, then lower cost.
4. Switch **Nova → Gemma 3 12B** if Gemma meets rule 2 and beats Nova on blocking issues by > 5 percentage points.
5. Switch to **Haiku 4.5** only if both small models fail rule 2; accept ~$2/week and the 2× hallucination rate with a
   stricter "null when absent" prompt.
6. Switch to **Kiro** only when all hold: the §5(a) verification passes on three consecutive days; monthly credit
   headroom ≥ 2× the measured skim burn after reserving the user's interactive use; the user explicitly accepts
   Amazon-operated Kiro infrastructure as the boundary for the personal corpus (reversing the standing decision in
   writing). Even then keep Bedrock as primary and Kiro as redundancy, because the credit shadow-cost is higher.
7. Switch to **local** (Gemma 3 12B Q4_K_M, llama.cpp) if Bedrock is unavailable or a channel must not leave the laptop.
8. Re-check the catalog for **Gemma 4 26B-A4B** before the bake-off; if it is callable with structured outputs it joins
   the bake-off as a fourth seat and likely replaces Gemma 3 12B.

**Risks.**
- Nova 2 Lite forced-tool JSON on 15–25k-token Slack threads and its default reasoning state are unverified live.
- HHEM measures summarisation faithfulness on vendor endpoints, not structured-extraction recall on Bedrock; the
  bake-off is the only real evidence.
- Gemma 3 12B lifecycle (EOL floor passed) — keep two models configured.
- Unregistered model ids are priced at the $10/$50 ceiling → cap trips; add `_PRICE_TABLE` entries first.
- If the skim runs inside the dream-cycle run it counts against the $9 cap and the 0.8 grounding share and displaces
  audits/votes; run it as its own job (§9).
- Flex tier requests "may be queued longer" — irrelevant at 02:00 but the job needs generous timeouts.
- 714 of 958 article parents in the 28-day window (2026-08-23..09-08) have no chunks and no vectors; clustering must
  not assume chunk vectors exist.
- Account data-retention mode could not be read (API newer than CLI 2.34.4); default is ZDR for every candidate.

---

## 8. Evaluation protocol before committing

Run on a Tuesday or Thursday (no dream-cycle run) under `AWS_PROFILE=second-brain-jobs`, with its own `SpendTracker`
cap (pattern: `scripts/panel_replay.py`, own `SpendTracker(cap_usd=…)`, `SpendCapExceeded` handled). The experiment is
the first permitted corpus read.

### 8.1 Recall test — arrivals window of run `3a1263a6` (2026-09-17/18)

- Reconstruct the window with `ledger.arrival_window_start` semantics (`src/dream_cycle/ledger.py:171-180`: previous
  completed run of the same `run_type`, clamped to [24 h, 14 d]) and select the parent-level memories in it
  (`parent_id IS NULL AND status IS DISTINCT FROM 'superseded'`, as `get_recent_parent_memories` does).
- Skim every document with each candidate; rank the inventory by the skim's novelty / contradiction flags and cluster
  size; assign clusters.
- Ground truth: the source memory ids behind the four **ACCEPTED** candidates of `3a1263a6`
  (`dream_cycle_candidates.source_memories`; titles per `docs/PLAN-panel-acceptance.md` §5: "Agentic SDLC pitch vs
  governance blocker, two channels"; "`k` alias launches Fable 5.1 at max effort"; "Approval gating as the MCP-server
  safety rationale"; "SAA docs: PII masked only if not in the input").
- Metrics: recall@20 / @40 / @80 of those source memories in the ranked inventory; cluster-level recall (are their
  clusters among the top clusters); as a weak negative control, the rank of the two REJECTED candidates' sources.
- Pass: recall@40 = 4/4.

### 8.2 Faithfulness spot-check — existing Astra grounding-audit prompt

- Sample 30 skim outputs stratified by channel (articles, Slack, email, chat, feed).
- For each, form a pseudo-candidate whose claims are the skim's summary, decisions, open questions and entities, and
  whose sole cited source is the document; run it through the existing grounding audit — `invoke_evaluator(...,
  GROUNDING_AUDIT_ROLE="skeptic", stage="grounding_audit")` (`src/dream_cycle/orchestrator.py:864-891`), graded verdict
  schema with `blocking_issues` / `advisory_issues` (`src/dream_cycle/packet.py`, prompt in `src/prompts/panel.py`), the
  Skeptic seat being `global.openai.gpt-6-astra` in profile `controlled`.
- Metrics: share of outputs with zero blocking issues; blocking-issue rate per field type (entities vs decisions vs
  summary) — fabricated entities are the costliest failure for the downstream Explorer.
- Pass: ≥ 90% zero-blocking.

### 8.3 JSON validity

- First-try parse + `jsonschema` validation rate; rate after one retry; per-field null rates; `max_tokens` stops
  (raised as `RuntimeError` by the adapter, `bedrock.py:381-386`). Pass: ≥ 99% first-try under constrained decoding.

### 8.4 Estimated experiment cost per candidate

Assume the window holds ~110–220 documents (~0.27–0.54M input tokens plus prefix).

| Candidate | Skim cost (Flex / std) | 30 Astra audits (~4.5k in + 0.5k out each at $10/$50 per M) | Total |
|---|---|---|---|
| Nova 2 Lite | $0.10–0.20 | ~$2.10 | ~$2.2–2.3 |
| Gemma 3 12B | $0.03–0.05 | ~$2.10 | ~$2.1–2.2 |
| Haiku 4.5 (on-demand) | $0.60–1.20 | ~$2.10 | ~$2.7–3.3 |
| (optional) DeepSeek V3.2 | $0.15–0.30 | ~$2.10 | ~$2.3–2.4 |

Three candidates ≈ $7–8 total, under the $9 cap and the $10 daily budget if run alone. Using Sonnet 5 as the auditor
would cut the audit line to ~$0.40, but the user asked for the Astra prompt, which is also the production auditor.

### 8.5 The same protocol for the Kiro path (synthetic / non-PII documents only)

- Generate ~30 synthetic documents mimicking each channel's shape (fake Slack thread with named speakers, fake article,
  fake calendar entries, fake chat export up to 25k tokens) with a hand-written answer key. No corpus content.
- Run each through `deepseek-3.2` and `claude-haiku-4.5` via `KiroInvoker` with `--model` set, one document per call.
- Record per call: `model_honored`, stderr, wall time, JSON first-try validity, and `/usage` credits before/after the
  batch (the served-model fingerprint via the multiplier, §5(a)(3)).
- Faithfulness: the same Astra grounding audit (the judge runs on Bedrock; the content is synthetic) or the answer key.
- Cost: $0 for the skim calls, ~20–60 Kiro credits **[estimate]**, ~$2 for 30 Astra audits (or ~$0.40 with Sonnet 5).
- Only after §5(a) passes on three consecutive days may §8.1 be repeated on the real window via Kiro.

---

## 9. Integration sketch (from the integration-fit brief; file references as read on 2026-09-19)

- **Storage:** new table `memory_skim` (migration `014_*.sql`; 001–013 applied): one row per parent document per skim
  pass — `memory_id` FK, `skim_run_id`, `window_start`, `backend`, `model`, `prompt_version`, `schema_version`,
  `summary`, `meta jsonb` (people, entities, projects, topics, decisions, open_questions, novelty, contradiction_flags),
  `cluster_id`, optional `embedding vector(1024)`, `input_tokens`, `output_tokens`, `cost_usd`, PK `(memory_id,
  skim_run_id)`; GIN on `meta`, btree on `window_start` and `cluster_id`. Keeps the "parent = raw capture" invariant
  (`src/ingest.py:171-187`) and lets re-skims with a new prompt/model coexist.
- **Job shape:** a standalone nightly LaunchAgent wrapped by `scripts/jobs/job_wrapper.sh` (inherits
  `AWS_PROFILE=second-brain-jobs`), modelled on `scripts/panel_replay.py`: own `SpendTracker` from
  `SECOND_BRAIN_SKIM_COST_CAP_USD` (e.g. $1), honours `kill_switch_engaged()` (`src/backends/cost.py:180`),
  `run_id=f"skim-<date>"` so metrics land in their own `logs/llm_metrics/*.jsonl`, idempotent on `(memory_id,
  prompt_version)` so a cap trip resumes next night. Running inside `_run_pipeline` instead would need a new
  `invoke_skim` between Step 3 and Step 4 and an update to the pinned method set
  (`tests/test_dream_cycle.py:2378-2413`), and would displace grounding audits under the shared cap.
- **Backend config:** simplest first pass is a skim-specific model setting in the job (not a new resolver role) —
  adding `"skim"` to `VALID_ROLES` (`src/backends/resolver.py:42-49`) forces every profile to define it and ripples
  into `_backend_provenance` and `tests/test_resolver.py:37, 58-74, 182-198`. `Resolver.invoker_for` constructs
  `adapter_cls(model=…)` only, so per-role options (Flex `serviceTier`, `max_tokens` ≈ 2k, forced tool choice) need
  either env vars (`BEDROCK_MAX_TOKENS`) or an options dict passed through `load_profiles` → `invoker_for`.
- **Adapter changes (`src/backends/bedrock.py`):** add `serviceTier: {"type": "flex"}` to the Converse request; make
  `toolChoice` per-model (forced `{"tool": {"name": "structured_output"}}` for Nova, `auto` kept for Fable); keep
  effort mapping (`amazon.*`/`google.*` → `None`) and verify reasoning is off; validate the parsed object against the
  schema with `jsonschema` (nothing does today — `invoke_evaluator` reads fields defensively).
- **Metering:** add `("nova-2-lite", 0.33, 2.75)` and `("gemma-3-12b", 0.09, 0.29)` to `_PRICE_TABLE` (standard
  prices — conservative, since the table is tier-blind; store the real tier and `cost_usd` per row in `memory_skim`).
  Kiro rows would be credits-only (§5(d)).
- **Consumers:** `allocation.build_arrivals_manifest` (`allocation.py:73-96`, called at `orchestrator.py:567-575`)
  renders a cluster-level inventory (cluster → count, channels, representative ids, novelty/contradiction flags) within
  the 7,000-char cap, or `MANIFEST_MAX_CHARS` is raised, or a new MCP tool lets the Explorer page the ~38k-char full
  list (Explorer budget ~20–25 tool calls, `src/prompts/explorer.py:97-116`). `dream_cycle_db.get_recent_parent_memories`
  gains a LEFT JOIN on `memory_skim` so `compose_arrivals_slice` (`allocation.py:175-213`) can stratify by cluster and
  prefer flagged documents; `packet.build_packet` can prepend the skim summary (~300 tokens/memory, cap 120,000 chars).
  All new reads go through `src/dream_cycle_db.py` (tests patch it; `MagicMock`-tolerant callers).
- **Clustering without new model calls:** parent vectors as `avg(embedding)` over chunks (`SELECT parent_id,
  avg(embedding) … GROUP BY parent_id`; pgvector 0.8.2 supports it; `scipy.cluster.hierarchy` is in the venv,
  scikit-learn/hdbscan are not). Fallback: embed the one-line summary with Titan v2 (~382 calls/week). Data gap: 714 of
  958 article parents (2026-08-23..09-08) have no chunks/vectors.
- **IAM:** none needed for Flex. Batch would need `CreateModelInvocationJob`/`Get…`, an S3 bucket, a service role and
  `iam:PassRole` — one reason to prefer Flex. Optionally grant `bedrock:ListFoundationModels` /
  `GetFoundationModelAvailability` to `second-brain-jobs` so the job can self-check availability (denied today).

---

## 10. Open questions for the user

1. Do you accept **Nova 2 Lite primary / Gemma 3 12B fallback, bake-off decides**, or do you want faithfulness-first
   (Gemma 3 12B) regardless of recall?
2. Chinese-origin weights hosted by AWS (DeepSeek V3.2) as a fallback seat — acceptable to you? The constraint permits
   it; the Bedrock report asked for explicit sign-off.
3. Kiro: run the synthetic `--model` verification now (zero dollars, ~20–60 credits)? What credit reserve must the skim
   never breach (e.g. 1,500)? Is overage enabled on the org plan? Are you willing, in principle, to reverse "no Kiro on
   the corpus" for the skim role if the verification passes — given the credits cost more than the Bedrock dollars?
4. Cadence: nightly (~55 docs, fresher inventory, stacks with run days) or per run (~127 docs)? Cost is negligible either
   way; only batch cares about the 100-record minimum, and Flex removes that concern.
5. Skim spend: its own cap (`SECOND_BRAIN_SKIM_COST_CAP_USD`, e.g. $1) as a separate job, or inside the $9 run cap?
6. Region policy for the skim: `us.*` / in-region only (recommended), even though the panel seats use `global.*` ids?
7. Should skim entities also be bridged into the knowledge graph (`entities` / `memory_entities`), or stay in
   `memory_skim` only until the Explorer proves it uses them?
8. Investigate the 714 unchunked article parents before relying on chunk-vector clustering?
9. Explorer inventory rendering: cluster-level summary inside 7,000 chars, or a paged MCP tool?
10. Gemma 3 12B lifecycle (EOL floor passed) and Gemma 4 not in the catalog — accept the risk with two models configured,
    or wait for a Gemma 4 catalog re-check?
