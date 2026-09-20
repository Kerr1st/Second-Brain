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

### Phase 1 — Nova adapter + probe  `[ ]`
- [ ] Verify against AWS docs (cite in code comments): Converse support for
      `us.amazon.nova-2-lite-v1:0`; the field that disables Nova 2 extended reasoning;
      the Flex service-tier request field (if any); forced `toolChoice.tool` support.
- [ ] `cost.py`: price entries for `nova-2-lite` (standard and, if separately priced, flex)
      and `gemma-3-12b`; unit tests.
- [ ] `bedrock.py`: per-model forced tool choice (Nova/Gemma) while Fable keeps `auto`;
      Nova reasoning-off fields; optional service tier; unit tests with a fake client.
- [ ] `src/skim/schema.py` (JSON schema + `SkimRecord` dataclass + validation),
      `src/prompts/skim.py` (prompt, `PROMPT_VERSION`), `src/skim/faithfulness.py`
      (verbatim-quote check, normalised whitespace).
- [ ] `scripts/skim_probe.py`: `--ids ... | --since ISO | --limit N`, `--model`, `--cap-usd`,
      `--dry-run`; per-doc call, validation, quote check; writes `logs/skim_probe/*.jsonl`;
      prints aggregates only (validity %, quote-verbatim %, empty-field rates, tokens, $,
      latency). Unit tests with a fake invoker (synthetic docs only).

### Phase 2 — Storage + runner  `[ ]`
- [ ] `migrations/014_memory_skim.sql`; `src/skim/store.py` (upsert, fetch by window,
      already-skimmed filter); `src/skim/runner.py` (`skim_window(...)` with sub-cap,
      idempotency, per-doc error isolation, metrics); orchestrator Step 3.5 with fallback;
      resolver role `skim`; `config/backends.toml`; tests (pipeline tests bypass skim via
      `patch.object`, like `_ground_candidate`).

### Phase 3 — Explorer consumption  `[ ]`
- [ ] `allocation.build_arrivals_inventory`; orchestrator uses it when records exist;
      `prompts/explorer.py` manifest/inventory section; guaranteed slice weighting;
      tests (`test_allocation*`, prompt contract tests).
- [ ] Optional: `cluster_id` via existing embeddings (greedy cosine threshold) if cheap.

### Phase 4 — Ordering + budget-aware Thinker  `[ ]`
- [ ] Sort slices arrival-anchored-first (guaranteed first) before Step 6; skip Thinker
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
