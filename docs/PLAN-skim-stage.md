# Plan: skim stage — read every arrival before the Explorer looks

Branch: `skim-stage` (off `panel-acceptance` at `1db8bf7`), developed in the worktree
`/Users/kerrfir/second-brain-worktrees/skim-stage` so the main tree (which the 02:00
LaunchAgent runs) stays on `panel-acceptance` until this is verified and merged.
Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped (reason in Decisions).
**Resume here after a restart:** read "Status", then the first unchecked task in "Phases".
Commands run from the worktree root with `/Users/kerrfir/second-brain/.venv/bin/python`
(the venv is shared; nothing is installed editable). Live DB is `memory_bank`; metered calls
use `AWS_PROFILE=second-brain-jobs`, region us-east-1. Research behind this plan:
`docs/RESEARCH-skim-model.md` (2026-09-19).

## 1. Problem

A run examines ~30 arrival memories (0–5 Explorer slices of 10–20 memories, plus a fixed
14-memory guaranteed arrivals slice) while ~382 parent-level memories (~931k tokens/week,
articles 87% of tokens) arrive per week. Coverage therefore cannot be bought with cadence:
full coverage would need ~13 runs/week (~$110/week). The Explorer chooses slices from a
manifest of counts plus 4 sample ids per channel (`MANIFEST_SAMPLES_PER_SOURCE`,
`src/dream_cycle/allocation.py`), so ~90% of arrivals are never seen by any agent.

Second finding (run `3a1263a6`, 2026-09-18): candidates are generated for all slices first
and judged in slice order, and the guaranteed arrivals slice is appended last, so under the
spend cap the arrivals slice is the first thing cut (slices 3–4 generated, never judged).

## 2. Design (decided 2026-09-20; details in §6)

- **Skim = data processing, not insight.** A cheap, faithful model reads every parent-level
  arrival in the run's window and emits one structured record per document. Frontier
  models (Explorer/Thinker/panel) keep the thinking. Faithfulness beats capability.
- **Model:** Amazon Nova 2 Lite on Bedrock, `us.amazon.nova-2-lite-v1:0`, extended reasoning
  off, temperature 0, schema-valid JSON via forced tool use, Flex service tier if the
  Converse API exposes it (else standard). Fallback `google.gemma-3-12b-it`. New role `skim`
  in `config/backends.toml` (all profiles → Bedrock; never Kiro).
- **Where it runs:** inside the dream-cycle run as Step 3.5 (after feedback, before the
  Explorer), over `ledger.arrival_window_start(...)`. Idempotent per
  `(memory_id, model, prompt_version)`. Own sub-cap `SKIM_SPEND_CAP_USD` (default $1.00) and
  metered through `SpendTracker` (counts toward the $9 run cap). **Never aborts the run**:
  any skim failure → Explorer gets today's manifest.
- **Record (per document):** `summary` (≤200 chars), `people[]`, `entities[]`, `topics[]`
  (≤5), `decisions[]` and `open_questions[]` each carrying a verbatim `quote` (≤160 chars)
  from the document, `novelty` 0–3, `salience` 0–3 (how much this concerns the user's own
  work/life vs generic web content), `contradiction_hint` (text|null), `doc_kind`.
  Empty/null when absent; never infer. Quotes make faithfulness mechanically checkable
  (substring match against the source) — this is the primary quality metric.
- **Storage:** table `memory_skim` (migration 014): memory_id FK, run_id, model,
  prompt_version, skim jsonb, cluster_id, input/output tokens, cost_usd, created_at;
  unique `(memory_id, model, prompt_version)`.
- **Consumption:** `build_arrivals_inventory(records, max_chars)` replaces the 4-sample
  manifest when skim records exist: per-channel counts header, then one line per document
  (`id | channel | salience | flags | summary`), ordered salience↓ novelty↓ then
  channel-stratified, capped by chars (≈24k). The guaranteed arrivals slice fills half
  from top-salience/flagged records and half from the existing stratified random sample
  (serendipity preserved).
- **Ordering fix (independent commit):** judge arrival-anchored slices first (guaranteed
  slice first); Thinker skips a slice when the remaining budget cannot cover judging its
  candidates (~$0.66 each + ~$0.25 Thinker).
- **Privacy:** corpus content goes only to Bedrock in the user's account. Subagents used
  for this work read code and docs, never `memories.content`, skim outputs, or logs with
  document text; probes print aggregates only.

## 3. Constraints (do not violate)

1. No corpus content through Kiro; the `skim` role is Bedrock-only in every profile.
2. Every new model id is added to `_PRICE_TABLE` in `src/backends/cost.py` before any
   live call (unknown ids price at the $10/$50 ceiling and trip caps).
3. Hermetic test suite stays green at every commit
   (`AWS_PROFILE= AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= AWS_DEFAULT_REGION=us-east-1
   /Users/kerrfir/second-brain/.venv/bin/pytest -q -p no:cacheprovider`; 1,067 at branch point).
4. Local branches only; no push. Merge to local `main` only on user approval.
5. The main tree keeps running `panel-acceptance` on Mon/Wed/Fri until the skim is verified.
6. Migrations are additive; applying 014 live does not affect `panel-acceptance` runs.

## 4. Phases

### Phase 0 — Setup  `[x]`
- [x] Worktree + branch `skim-stage`; this plan.

### Phase 1 — Nova adapter + probe  `[x]`
- [x] Verify against AWS docs (cite in code comments): Converse support for
      `us.amazon.nova-2-lite-v1:0`; the field that disables Nova 2 extended reasoning;
      the Flex service-tier request field (if any); forced `toolChoice.tool` support.
- [x] `cost.py`: price entries for `nova-2-lite` (standard and, if separately priced, flex)
      and `gemma-3-12b`; unit tests.
- [x] `bedrock.py`: per-model forced tool choice (Nova/Gemma) while Fable keeps `auto`;
      Nova reasoning-off fields; optional service tier; unit tests with a fake client.
- [x] `src/skim/schema.py` (JSON schema + `SkimRecord` dataclass + validation),
      `src/prompts/skim.py` (prompt, `PROMPT_VERSION`), `src/skim/faithfulness.py`
      (verbatim-quote check, normalised whitespace).
- [x] `scripts/skim_probe.py`: `--ids ... | --since ISO | --limit N`, `--model`, `--cap-usd`,
      `--dry-run`; per-doc call, validation, quote check; writes `logs/skim_probe/*.jsonl`;
      prints aggregates only (validity %, quote-verbatim %, empty-field rates, tokens, $,
      latency). Unit tests with a fake invoker (synthetic docs only).
- [x] Follow-up A (after the first real probe): token-sequence quote path in
      `faithfulness.py` (`verbatim_exact` / `verbatim_tokens` reported per field).
- [x] Follow-up B: `validate_record(raw, coerce=True)` trims summary (hard limit 300, sentence
      boundary) / quote (≤160 prefix) / topics (first 5) and records `coercions`; probe
      reports coerced records and coercion classes; `--strict` keeps the old behaviour.

### Phase 2 — Storage + runner  `[x]`
- [x] `migrations/014_memory_skim.sql`; `src/skim/store.py` (upsert, fetch by window,
      already-skimmed filter); `src/skim/runner.py` (`skim_window(...)` with sub-cap,
      idempotency, per-doc error isolation, metrics); orchestrator Step 3.5 with fallback;
      resolver role `skim`; `config/backends.toml`; tests (pipeline tests bypass skim via
      `patch.object`, like `_ground_candidate`).

### Phase 3 — Explorer consumption  `[x]`
- [x] `allocation.build_arrivals_inventory`; orchestrator uses it when records exist;
      `prompts/explorer.py` manifest/inventory section; guaranteed slice weighting;
      tests (`test_allocation*`, prompt contract tests).
- [x] Optional: `cluster_id` via existing embeddings (greedy cosine threshold) if cheap.

### Phase 4 — Ordering + budget-aware Thinker  `[x]`
- [x] Sort slices arrival-anchored-first (guaranteed first) before Step 6; skip Thinker
      when remaining budget < estimated judging cost; tests; log lines.

### Phase 5 — Live validation  `[ ]`
- [ ] Probe on ~25 real docs across channels (~$0.10): JSON validity ≥ 98%, quote-verbatim
      ≥ 95%, no fabricated people on a 10-record Astra/Sonnet spot-check (aggregates only).
- [ ] Recall test: skim the source memories of run `3a1263a6`'s four ACCEPTED candidates
      (`dream_cycle_candidates.candidate_json->'sources'`); all must carry salience ≥ 2 or a
      decision/open question with a verbatim quote.
- [ ] One manual live run from the worktree (`--profile controlled`, $9 cap): skim cost,
      inventory size, Explorer slice arrival-share vs `3a1263a6`, no fallback triggered.

### Phase 6 — Wrap  `[ ]`
- [ ] `docs/MODEL-BACKENDS.md` (skim role, Nova), `docs/ARCHITECTURE.md` (Step 3.5),
      `docs/OPERATIONS.md` (sub-cap, migration 014); Status here; memory note; merge decision.

## 5. Status (newest first)

- 2026-09-20 — **Prompt `skim-v2`: user identity + channel priors** (this commit; hermetic suite
  1,332 → 1,370; zero Bedrock calls; no corpus content read). The live probe of 41 real documents with
  `skim-v1` showed salience compressing — 34/41 at S2, S3 never used even for the user's own emails,
  calendar entries and distilled work sessions, 3 of 4 web articles at S2 (novelty was fine: 0/1/2 =
  8/15/18) — because the model was never told who "the user" is and had no channel priors. New
  `config/skim.toml` (committed, not a secret: `[identity]` name / aliases / role / org / focus,
  `[channels]` own_activity / external keyed by `source_type`; fields documented in its header) read by
  `src/skim/identity.py` (`load_identity(path=None) -> Identity`; missing file → neutral defaults and no
  identity block, malformed → `ValueError`). `src/prompts/skim.py`: "About the user" block between the
  intro and the rules (with "never add these facts to the record unless the text states them"); salience
  redefined by the user's role in the text (3 = author / addressee / participant or the user's own
  decision, commitment, task or meeting; 2 = the user's team, customers, projects, tools or plans are the
  subject but the user is not an actor; 1 = professional interest for the role; 0 = none; bulk mail and
  automated notices do not make the user an addressee); channel priors (own-activity starts at 2 and
  rises to 3 when the user is an actor; external starts at 0 and rises only when the text explicitly
  concerns a focus area; untagged = text alone); the request's channel line carries the tag
  (`channel: slack (the user's own activity)`, `channel: article (external content)`). Every other rule is
  byte-identical to v1. Runner, probe and orchestrator import `PROMPT_VERSION`, so v1 rows are simply
  superseded by the idempotency key. Next: re-probe the same 41 documents with `skim-v2` and compare the
  salience distribution before Phase 5.
- 2026-09-20 — **Phases 3 + 4 done** (commits `e4aa4e6` Explorer inventory / weighted arrivals slice /
  embedding clusters / unverified-quote policy, `0255abd` judging order + budget-aware Thinker and
  panel soft stop; hermetic suite 1,268 → 1,332; zero Bedrock calls; no corpus content read).
  Inventory line `id | channel | S<salience> N<novelty> | D<n> Q<n> C P<n> ! | summary(≤140) [c<k>]`,
  salience↓ novelty↓ then round-robin across channels within a tier, 24k-char cap with a
  "… k more documents not listed" footer; `invoke_explorer` logs `Explorer arrivals input: inventory|manifest`
  with line/char counts and records `explorer_input` (+ `inventory_listed/docs/chars`) in run stats.
  Guaranteed slice: 7 slots to the top salience/novelty records first, 7 to the stratified method over
  the rest, caps held for the whole slice (`skim_weighted`/`skim_picked` in slice metrics); byte-identical
  without records. Unverified-quote policy applied in the runner: a quote failing both faithfulness
  paths is stored as `null`, the item's text stays, `quotes_unverified: n` on the record and
  `skim_quotes_unverified` in stats. Clustering **done**: `memories.embedding vector(1024)` + pgvector
  0.8.2 exist; the parent's embedding or `avg()` of its chunks covers 100% of the live window (868 docs,
  two set queries ≈ 0.4 s); `src/skim/cluster.py` greedy cosine ≥ 0.86 (numpy), labels `c1..` persisted
  to `memory_skim.cluster_id` and shown as `[c<k>]`. Phase 4: slices judged guaranteed-first then by
  arrival_share↓ (stable; unchanged when the ledger is unavailable), `EST_THINKER_CALL_USD 0.30` /
  `EST_JUDGE_CANDIDATE_USD 0.70` (run `3a1263a6`), Thinker skipped when remaining < 0.30 + 2×0.70 +
  0.70×pending (`slices_skipped_for_budget`), panel stops softly when remaining < 0.70
  (`candidates_unjudged_for_budget`; run completes normally); the hard cap stays the backstop.
  Next: Phase 5 live validation from the worktree.
- 2026-09-20 — **Phase 1 follow-ups + Phase 2 done** (commits `921f59f` token-path quote check +
  coercing validator, `e163b4b` migration 014 / store / runner / `skim` role, `b6e8717`
  orchestrator Step 3.5; hermetic suite 1,166 → 1,268; zero Bedrock calls). Migration 014
  applied to the live `memory_bank` with `migrations/migrate.sh` (recorded in
  `schema_migrations`): table `memory_skim` + indexes, and — beyond the plan — a nullable
  `dream_cycle_runs.stats JSONB` so `complete_run` can persist the skim counters
  (`skim_docs/skimmed/reused/failed/cost_usd`, …) without a column each; additive, `panel-acceptance`
  runs leave it NULL. Adapter options are a new inline table on a role in `backends.toml`
  (`options = { temperature = 0.0, service_tier = "flex", max_tokens = 4096 }`, bedrock-only keys,
  validated at load, part of the invoker cache key); `skim` is Bedrock Nova 2 Lite in all five
  profiles and the resolver rejects it on any MCP-capable backend at load and at resolution.
  Skim sub-cap `SKIM_SPEND_CAP_USD = 1.00` (env `SECOND_BRAIN_SKIM_CAP_USD`, ≤ 15% of the run cap),
  metered on the run's `SpendTracker`. Skim runs for every run type (idempotent per
  `(memory_id, model, prompt_version)`, so overlapping windows cost nothing twice). Safety net:
  `tests/conftest.py` takes `src.skim.store` offline for the whole unit suite. Not yet live-run
  (Phase 5); the stored `skim` jsonb is the plain record — fabricated-quote items are counted,
  not dropped (Phase 3 decides how the inventory treats them).
- 2026-09-20 — **Phase 1 done** (commits `9f15696` backends, `0ca0efb` skim modules + probe;
  hermetic suite 1,067 → 1,166). AWS docs verified and cited in `src/backends/bedrock.py`:
  Converse serves `us.amazon.nova-2-lite-v1:0`; reasoning off =
  `additionalModelRequestFields.reasoningConfig.type = "disabled"`; tier =
  `serviceTier: {"type": "flex"}` (response echoes the served tier); `toolChoice.tool` is
  Nova/Claude-3-only, so Gemma 3 12B uses `outputConfig.textFormat` JSON schema instead
  (doc-verified, not live). Prices (us-east-1, pricing-page data 2026-09-17): Nova 2 Lite geo
  $0.33/$2.75 standard, $0.165/$1.375 Flex; Gemma 3 12B $0.09/$0.29, Flex derived $0.045/$0.145.
  Live-verified on a synthetic document (3 calls, `AWS_PROFILE=second-brain-jobs`): the full
  request — forced `structured_output` tool, `reasoningConfig disabled`, `temperature 0.0`,
  `maxTokens 4096`, `serviceTier flex` — accepted every time; served tier `flex`; ~2.5k in /
  ~340 out tokens; **$0.0009 per call**; 2.3–2.7 s. Quality on the synthetic doc: every quote
  verbatim, people/entities exact; one open-question quote ran two sentences together (163 chars
  > 160) on all three calls, and Nova accepts but does not enforce `maxLength` in the tool
  schema — so the 160-char limit is enforced by `validate_record` and the probe reports
  `invalid_reasons` by class. Watch this rate in Phase 5; relax the limit or trim quotes in code
  if it dominates. Probe `--profile` means the AWS named profile (no backend role exists yet).
- 2026-09-20 — Plan written; worktree `skim-stage` created off `panel-acceptance@1db8bf7`.
  Research doc `docs/RESEARCH-skim-model.md` exists untracked in the main tree (to be
  committed on this branch). User decisions: model = Nova 2 Lite; cadence stays Mon/Wed/Fri;
  build the skim now; `panel-acceptance` merge still pending.

## 6. Decisions

- **2026-09-20 Nova 2 Lite over Gemma 3 12B / GLM 5 / DeepSeek V3.2.** Faithfulness
  (HHEM 5.1% vs 4.4% / 10.1% / 6.3%) within noise of the best, 1M context, first-party data
  handling, authorised already; "most advanced" is the wrong axis for grounded extraction
  (reasoning-heavy models fabricate more). Bedrock-hosted Chinese open weights pass the
  privacy filter; they lost on faithfulness or context, not privacy.
- **2026-09-20 Skim inside the run, not a nightly job.** One process, one `SpendTracker`,
  window already defined by the ledger; ~$0.15/run at Mon/Wed/Fri. A nightly job can be
  added later if `memory_brief` freshness needs it.
- **2026-09-20 Verbatim quotes as the faithfulness metric.** Mechanical, free, and the
  same failure mode (invented details) the panel's grounding audit exists to catch.
- **2026-09-20 Flex tier over batch inference.** AWS docs disagree on whether batch supports
  structured output; Flex gives the same 50% discount without S3/IAM or a 100-record minimum.
- **2026-09-20 Cadence unchanged (Mon/Wed/Fri).** With the skim, coverage is independent of
  cadence; extra candidates per run (~$0.66) are cheaper than extra runs (~$1.95 fixed).

## 7. Useful queries

```sql
-- arrivals in a window (parent-level, not superseded)
SELECT source_type, count(*) FROM memories
 WHERE parent_id IS NULL AND status IS DISTINCT FROM 'superseded' AND created_at >= now() - interval '3 days'
 GROUP BY 1 ORDER BY 2 DESC;
-- sources behind the four accepted candidates of run 3a1263a6
SELECT left(id::text,8), jsonb_array_length(candidate_json->'sources') FROM dream_cycle_candidates
 WHERE run_id::text LIKE '3a1263a6%' AND final_verdict='ACCEPTED';
-- skim coverage per run (after migration 014)
SELECT run_id, model, count(*), sum(cost_usd) FROM memory_skim GROUP BY 1,2 ORDER BY max(created_at) DESC;
```

## 8. File map

- `src/skim/{schema,store,runner,faithfulness}.py`, `src/prompts/skim.py` — new.
- `src/backends/cost.py` (`_PRICE_TABLE`), `src/backends/bedrock.py`
  (`_shape_structured_output`, `_map_effort_to_model_fields`, request kwargs), `src/backends/resolver.py`
  (`VALID_ROLES`), `config/backends.toml` — model/role wiring.
- `src/dream_cycle/orchestrator.py` (Step 3.5, slice ordering, budget-aware Thinker),
  `src/dream_cycle/allocation.py` (`build_arrivals_inventory`, slice weighting),
  `src/prompts/explorer.py` — consumption.
- `migrations/014_memory_skim.sql`, `scripts/skim_probe.py`, `tests/test_skim*.py`.
