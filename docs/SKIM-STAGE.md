# The skim stage — read every arrival before the Explorer looks

Source-reviewed 2026-09-20 on branch `skim-stage`. Design history, phase results and decisions are in `docs/PLAN-skim-stage.md`; the model selection is in `docs/RESEARCH-skim-model.md`. Figures below come from the code, two live probes (41 real documents) and the first live run `05171b48`; they record what was measured, not a service guarantee. Prices are configuration values from `src/backends/cost.py`.

## Purpose

A dream-cycle run examines about 30 arrival memories — 0–5 Explorer slices of 10–20 memories plus the fixed 14-memory guaranteed arrivals slice — while about 382 parent-level memories arrive per week (~931k tokens, articles 87% of them). Coverage cannot be bought with cadence: reading everything with the frontier agents would take ~13 runs a week (~$110/week), and the Explorer chose its slices from a manifest of per-channel counts plus four sample ids per channel, so roughly 90% of what arrived was never seen by any agent. The skim fixes the input, not the thinking: a cheap, faithful model reads every arrival once and emits one structured record per document; the Explorer then chooses from a complete inventory, and the guaranteed slice is weighted toward what the skim found salient. The skim is data processing — it states what a document says, never what it means. The frontier models keep the insight work.

## Where it runs

| | |
|---|---|
| Step | 3.5 of `DreamCycleOrchestrator.run` (`src/dream_cycle/orchestrator.py`, `_skim_arrivals`): after the feedback and memory-stats steps, before the Explorer (Step 4). Runs for every run type. |
| Window | `ledger.arrival_window_start(run_id, run_type, started_at)` (`src/dream_cycle/ledger.py`): the previous completed run of the same type, clamped to between 24 hours and 14 days before the run start; 7 days back when there is no previous run. The same window the manifest and the guaranteed slice use. |
| Documents | Parent-level (`parent_id IS NULL`), not superseded, `created_at >= window_start`, excluding the dream cycle's own outputs (tag `dream-cycle` / `metadata.dream_cycle`) — `ARRIVALS_FILTER_SQL` in `src/skim/store.py`. Chunks are reached through their parents. Text is loaded only for the documents that still need skimming. |
| Idempotency | One row per `(memory_id, model, prompt_version)`. Documents already skimmed with the active model and `PROMPT_VERSION` are counted as `reused` and cost nothing, so overlapping windows (a Wednesday run after Monday's, a manual re-run) pay only for new arrivals. |
| Isolation | Each document is skimmed in its own `try`: a transport error, an invalid reply or a storage failure counts against that document (`failed` / `invalid`) and the loop goes on. Documents longer than 120,000 characters (`DEFAULT_MAX_DOC_CHARS`, `src/skim/runner.py`) are cut and flagged `truncated`. Per-call timeout 120 s. |
| Fallback | The stage never aborts the run. If the `skim` role is missing, resolves to a non-Bedrock backend, or anything at all raises, `_skim_arrivals` logs one WARNING and returns `None`; the Explorer then gets the old manifest exactly as before. The only exception that propagates is `SpendCapExceeded` (the run cap), as in every other stage. |
| After the skim | `_load_skim_records` fetches the window's records (this run's and earlier runs') when `skimmed + reused > 0`, clusters them by their existing embeddings (`src/skim/cluster.py`: greedy cosine ≥ 0.86 on the parent's `memories.embedding` or the `avg()` of its chunks; labels `c1`, `c2`, … only for clusters of two or more; no model calls) and hands them to the Explorer inventory and the guaranteed slice. Any failure there also degrades to the manifest. |

## Model and request shape

Role `skim` in `config/backends.toml`, identical in all five profiles:
`skim = { backend = "bedrock", model = "us.amazon.nova-2-lite-v1:0", effort = "low", options = { temperature = 0.0, service_tier = "flex", max_tokens = 4096 } }`.
The `options` table is passed to the adapter's constructor (only `bedrock` accepts one; `ADAPTER_OPTIONS` in `src/backends/resolver.py`). The resolver rejects a skim on any MCP-capable backend (Kiro, Claude Code, Codex) at config load and again at resolution (`assert_backend_supports_role`, `src/backends/base.py`).

| Request element | Nova 2 Lite (primary, live-verified) | Gemma 3 12B IT (fallback, doc-verified only) |
|---|---|---|
| Model id | `us.amazon.nova-2-lite-v1:0` (geo inference profile; traffic stays in US regions) | `google.gemma-3-12b-it` |
| Structured output | Forced tool use, `toolChoice: {"tool": {"name": "structured_output"}}`, `SKIM_SCHEMA` as the tool's input schema; Nova serves it by constrained decoding | `outputConfig.textFormat` JSON schema (forced tool choice is Nova/Claude-3-only) |
| Reasoning | `additionalModelRequestFields.reasoningConfig.type = "disabled"` — what effort `low` maps to; `medium`/`high` would enable it and is never used for the skim | none (non-reasoning model) |
| Sampling | `inferenceConfig.temperature = 0.0`, `maxTokens = 4096` | same options |
| Service tier | `serviceTier: {"type": "flex"}`; the response echoes the tier actually served, and only a served `flex` is priced at the Flex rate (`price_for`, `src/backends/cost.py`) | same |
| Price, standard ($/M in / out) | 0.33 / 2.75 | 0.09 / 0.29 |
| Price, Flex | 0.165 / 1.375 | 0.045 / 0.145 (derived: 50% of standard) |

Measured: a synthetic document costs ~$0.0009 per call (~2.5k input / ~340 output tokens, 2.3–2.7 s). Live run `05171b48` (2026-09-20, profile `controlled`): 164 arrivals skimmed in 260 s for $0.19 — about $0.001 per document, 1.6 s each — with 0 failed, 0 invalid, 5 coerced and 22 of 23 quotes verbatim (1 stored as unverified). At Mon/Wed/Fri cadence that is roughly $0.15–0.20 per run, under $0.50 per week.

The request is plain text, not JSON (`build_skim_request`, `src/prompts/skim.py`): a three-line header — `channel: <source_type> (<tag>)`, `title:`, `created_at:` — then the document body between `---- document ----` markers, unmodified, so a faithful quote is a literal substring of what the model saw. The system prompt (`SKIM_SYSTEM_PROMPT`) carries the rules, the "About the user" block and the channel priors. `PROMPT_VERSION` is `skim-v2`.

## The record

One JSON object per document, validated by `validate_record` in `src/skim/schema.py`. Bedrock's structured-output grammar accepts neither `maxLength` nor `maximum` (and Nova ignores them when sent), so the schema states limits in field descriptions and the validator enforces them.

| Field | Type | Limit and coercion | Notes |
|---|---|---|---|
| `summary` | string | ≤ 200 chars asked; coercion accepts up to 300 and cuts at the last sentence boundary (`summary_trimmed`) | One neutral sentence; empty when there is no substantive content |
| `people[]` | string[] | — | Names exactly as written |
| `entities[]` | string[] | — | Organisations, teams, products, projects, systems, places, as written |
| `topics[]` | string[] | ≤ 5; extra tags dropped (`topics_truncated`) | One to three words each |
| `decisions[]` | `{text, quote}` | `quote` ≤ 160 chars; longer quotes cut to a word-boundary prefix (`decisions.quote_trimmed`) | Only decisions the text records as made or agreed |
| `open_questions[]` | `{text, quote}` | as above (`open_questions.quote_trimmed`) | Only questions the text itself asks or leaves open |
| `novelty` | integer 0–3 | enum | 0 routine … 3 a surprising fact or a decision with consequences |
| `salience` | integer 0–3 | enum | The user's role in the text — see "Identity and channel priors" |
| `contradiction_hint` | string or null | — | One sentence only when the text itself states a change or reversal; empty string normalised to null |
| `doc_kind` | enum | `article`, `conversation`, `email`, `calendar`, `note`, `code`, `reference`, `other` | What the text is, not which channel captured it |

Empty and null mean "absent"; the prompt forbids inference. With the default `coerce=True` only the three length overruns above are repaired, each recorded in `SkimRecord.coercions` and counted (`skim_coerced`, `coercion_classes`); wrong types, missing or extra fields and out-of-range scores stay invalid in both modes. The decision (2026-09-20) followed the first live probe: all five invalid records out of 33 were length overruns, never wrong content.

Verbatim quotes are the faithfulness metric. Before a record is stored every quote is checked against the text the model saw (`src/skim/faithfulness.py`): `verbatim_exact` is a substring match after normalising whitespace, typographic quotes, dashes and ellipses; `verbatim_tokens` reduces both sides to lower-cased alphanumeric tokens and requires the quote's tokens to occur as a contiguous run (markdown marks and punctuation vanish; reordered or replaced words still fail). A quote that fails both paths is stored as `null` — the item's `text` stays — and the record gains `quotes_unverified: n`; nothing downstream may treat an unverified passage as the document's words, and the inventory marks the document with `!`. This replaced a paid second-model spot-check: in the probes every miss was a near-verbatim edit of one long transcript, never an invention (`docs/PLAN-skim-stage.md` §6).

## Identity and channel priors (`config/skim.toml`)

Salience is "how much this concerns the user's own work or life", which the model cannot judge without knowing who the user is: with `skim-v1`, 34 of 41 probed documents scored 2 and 3 was never used, not even for the user's own e-mail and calendar. `config/skim.toml` (committed; not a secret — name, aliases, role, organisation and focus areas as they already appear throughout the corpus, no credentials or contact details) is read by `load_identity` in `src/skim/identity.py` and rendered by `src/prompts/skim.py` as an "About the user" block plus a per-request channel tag.

- `[identity]` — `name`, `aliases`, `role`, `org`, `focus`. Salience 3 = the user is author, addressee or participant, or it is the user's own decision, commitment, task or meeting; 2 = the user's team, customers, projects, tools or plans are the subject but the user is not an actor; 1 = professional interest for the role; 0 = none. Bulk mail and automated notices do not make the user an addressee. The prompt tells the model never to add these facts to a record unless the text states them.
- `[channels]` — `own_activity` (mail, calendar, Slack, chats, distilled sessions, …: a document starts at 2 and rises to 3 when the user is an actor) and `external` (`article`, `youtube`, `browser_history`, `quick_desktop_feed`: starts at 0 and rises only when the text explicitly concerns a focus area), keyed by `memories.source_type`. The request's channel line carries the tag (`channel: slack (the user's own activity)`, `channel: article (external content)`). An unlisted channel gets no tag; a channel in both lists is a configuration error.
- Missing file → neutral defaults (no identity block, no tags) and a WARNING; malformed file → `ValueError` at prompt build, so a typo fails loudly instead of skewing every record.

Editing rule: the identity block is part of the prompt, and `memory_skim` is unique on `(memory_id, model, prompt_version)`. An edit that should re-skim already-skimmed documents (a new focus area, a renamed role, a channel moved between lists) needs a `PROMPT_VERSION` bump in `src/prompts/skim.py`; without it the new wording applies only to documents not yet skimmed. Old rows stay for comparison and are simply superseded by the idempotency key. With `skim-v2` the 41-document re-probe scored S0/1/2/3 = 4/5/9/23 (articles 0–1, own-activity channels 3), and recall on the 12 sources behind run `3a1263a6`'s accepted candidates was 11/12.

## Storage

Migration `migrations/014_memory_skim.sql`, applied to the live `memory_bank` on 2026-09-20 (additive; `panel-acceptance` runs are unaffected).

| `memory_skim` column | Meaning |
|---|---|
| `id` | row uuid |
| `memory_id` | FK → `memories(id)`, `ON DELETE CASCADE` |
| `run_id` | the run that paid for the row (`NULL` for probes and backfills; no FK) |
| `model`, `prompt_version` | with `memory_id`: `UNIQUE (memory_id, model, prompt_version)` |
| `skim` | JSONB — the validated record in `SKIM_SCHEMA` shape plus `quotes_unverified`; unverified quotes are `null` |
| `cluster_id` | `c<k>` relative to the last run's window, or `NULL`; rewritten by every run over an overlapping window |
| `input_tokens`, `output_tokens`, `cost_usd` | metered usage of the call |
| `truncated` | the document was cut to `max_doc_chars` before the model saw it |
| `created_at` | refreshed on re-skim |

Indexes on `run_id` and `created_at`. `src/skim/store.py` is the only reader/writer: `list_arrivals` (no text), `missing_ids`, `fetch_documents` (text — one of the two readers of `memories.content` for the skim, the probe being the other), `upsert_skim`, `fetch_window_records` (joined with `memories` under the same arrivals filter; the inventory's input), `fetch_window_centroids` / `set_cluster_ids`, `run_summary`.

The same migration adds `dream_cycle_runs.stats JSONB`, where `complete_run` persists every counter that has no column of its own (`NULL` for older runs and for `panel-acceptance` runs):

| Key group | Keys |
|---|---|
| Skim (`SkimResult.to_stats`) | `skim_docs`, `skim_skimmed`, `skim_reused`, `skim_failed`, `skim_invalid`, `skim_coerced`, `skim_truncated`, `skim_quotes_emitted`, `skim_quotes_verbatim`, `skim_quotes_unverified`, `skim_cost_usd`, `skim_aborted_on_cap`, `skim_duration_s`, `skim_model`, `skim_prompt_version` |
| Explorer input | `explorer_input` (`inventory` or `manifest`); with an inventory also `inventory_listed`, `inventory_docs`, `inventory_chars` |
| Budget | `slices_skipped_for_budget`, `candidates_unjudged_for_budget` (present only when non-zero) |

## What the Explorer sees: the inventory

When skim records exist for the window, `render_arrivals_inventory` (`src/dream_cycle/allocation.py`) replaces the manifest at the end of the Explorer's user message; `invoke_explorer` logs `Explorer arrivals input: inventory|manifest` with line and character counts. The block is:

1. The header `## What arrived since the previous run (<window start> UTC)`, the totals (`N documents across K channels; M of them were read by the skim stage and are listed below, most salient first.`) and one `- <channel>: <count> <unit>` line per channel (all arrivals, skimmed or not).
2. The legend (`INVENTORY_LEGEND`).
3. One line per document: `<id> | <channel> | S<salience> N<novelty> | <flags> | <summary ≤ 140 chars> [c<k>]`. Flags: `D<n>` decisions, `Q<n>` open questions, `C` a contradiction hint, `P<n>` people named, `!` a quote could not be verified, `-` none; `[c<k>]` the embedding cluster.
4. When the cap cuts the list: `… k more documents not listed (all ids are retrievable by tools)`.

Ordering (`rank_skim_records`): salience descending, then novelty descending; within one (S, N) tier the channels are interleaved round-robin — each channel newest first, channels by size then name — so a large channel cannot monopolise the top of a tier. The cap is `INVENTORY_MAX_CHARS = 40_000` (~10k tokens; raised from 24k after run `05171b48` listed 121 of 164 documents — a Fri→Mon window of ~200 documents at ~175 chars per line now fits). The Explorer prompt (`src/prompts/explorer.py`, "The arrivals inventory") explains the semantics and tells the Explorer to build arrival-anchored slices from the inventory across channels and salience levels, reading a document flagged `!` before relying on it. Without records the block is the older manifest: counts plus four sample ids per channel, 7,000 chars.

The guaranteed arrivals slice (`select_arrivals`, 14 memories) is weighted the same way: the first 7 slots go to the top of the ranked records, the other 7 to the historical sqrt-stratified sample over the remaining documents; both halves respect the per-channel cap (4) and the semantic cap (3) for the slice as a whole — the important things guaranteed, serendipity preserved. A partial skim just leaves more slots to the stratified half, so the slice never shrinks because the skim was incomplete; without records the selection is byte-identical to before. The slice's ledger metrics gain `skim_weighted: true` and `skim_picked: n`.

## Judging order and the budget-aware soft stop

Run `3a1263a6` (2026-09-18) generated candidates for every slice, judged them in slice order with the guaranteed arrivals slice appended last, and hit the spend cap — so the newest material was the first thing cut (slices 3–4 generated, never judged). Two changes, in `src/dream_cycle/orchestrator.py` and `order_slices` in `src/dream_cycle/allocation.py`:

- Order: the `guaranteed_arrivals` slice first, then the Explorer's slices by `arrival_share` descending (stable sort; unchanged when the ledger rows are unavailable). Logged as `Slice order for the Thinker and the panel: …`.
- Soft stop, computed from the run's real spend (`_budget_remaining`; never estimates; inactive when nothing is metered): before invoking the Thinker for a slice the run must have at least `EST_THINKER_CALL_USD + 2 × EST_JUDGE_CANDIDATE_USD + EST_JUDGE_CANDIDATE_USD × pending` left — $0.30 + $1.40 + $0.70 per candidate already waiting — otherwise the remaining slices are skipped and counted in `slices_skipped_for_budget`. Before judging a candidate the run must have `EST_JUDGE_CANDIDATE_USD` ($0.70) left, otherwise the rest are left unjudged — no verdict stored, none fabricated — and counted in `candidates_unjudged_for_budget`. The run completes normally either way; the hard cap in `SpendTracker` stays the backstop.

The constants come from run `3a1263a6` on the Bedrock profile (Thinker $0.21–0.30 per slice; grounding audit + revision + four votes ≈ $0.66 per candidate), rounded up so the rule errs toward finishing what was started rather than starting more. Run `05171b48` (8 candidates, $6.90 of a $9 cap) needed neither stop.

## Cost controls

| Control | Value | Where |
|---|---|---|
| Skim sub-cap | `SKIM_SPEND_CAP_USD = 1.00`; env `SECOND_BRAIN_SKIM_CAP_USD` overrides it (must be > 0; a bad value falls back to the default with a warning) | `_skim_cap_usd`, `src/dream_cycle/orchestrator.py` |
| Share of the run cap | never more than `SKIM_RUN_CAP_SHARE = 0.15` of `SECOND_BRAIN_RUN_COST_CAP_USD`: $0.75 at the default $5 cap, $1.00 at the $9 cap used live | same |
| At the sub-cap | the runner stops cleanly before the next document (`aborted_on_cap`, stat `skim_aborted_on_cap`); records already stored still feed the inventory | `skim_window`, `src/skim/runner.py` |
| Run cap | every skim call is metered through the run's `SpendTracker` and counts toward the run cap; `SpendCapExceeded` propagates after the call that tripped it has been stored | `src/backends/cost.py` |
| Pricing | `_PRICE_TABLE` / `_FLEX_PRICE_TABLE` entries for `nova-2-lite` and `gemma-3-12b`; an unknown model id is priced at the $10/$50 ceiling (~$0.04 per document instead of ~$0.001, so the $1 sub-cap trips after about 25 documents) — add the price before pointing the role at a new model (`docs/PLAN-skim-stage.md` §3) | `src/backends/cost.py` |

## Operations

**Probe.** `scripts/skim_probe.py` (`--help` for the full list) runs the same prompt, validator and quote check over a few real documents outside a run, metered under `--cap-usd`, and prints aggregates only — validity %, coercion classes, quote-verbatim % by path, empty-field rates, tokens, dollars, latency, per-channel counts — never a title, summary, quote or document text:

    AWS_PROFILE=second-brain-jobs .venv/bin/python scripts/skim_probe.py --since 2026-09-17 --limit 25 --cap-usd 0.25
    ... --ids <uuid> [<uuid> ...]                       # specific documents
    ... --model google.gemma-3-12b-it --service-tier default
    ... --dry-run                                      # build the requests; no calls, no spend
    ... --strict                                       # reject over-long fields instead of trimming

`--profile` is the AWS named profile, not a backend profile. One JSON line per document goes to `logs/skim_probe/<stamp>.jsonl`; that file contains document-derived text and stays local (`logs/` is gitignored). The probe writes nothing to `memory_skim`.

**After a run.** Read the `Skim (<model>): N arrival(s) since … — skimmed, reused, failed, invalid, coerced, truncated; quotes x/y verbatim; $ of $ sub-cap` line and `Explorer arrivals input: inventory` in the run log, then:

```sql
-- the latest runs' skim counters, Explorer input and budget stops
SELECT left(id::text, 8) AS run, started_at::date,
       stats->>'skim_docs' AS docs, stats->>'skim_skimmed' AS skimmed, stats->>'skim_reused' AS reused,
       stats->>'skim_failed' AS failed, stats->>'skim_invalid' AS invalid,
       stats->>'skim_quotes_unverified' AS unverified, stats->>'skim_cost_usd' AS usd,
       stats->>'explorer_input' AS input, stats->>'inventory_listed' AS listed, stats->>'inventory_docs' AS inv_docs,
       stats->>'slices_skipped_for_budget' AS skipped, stats->>'candidates_unjudged_for_budget' AS unjudged
  FROM dream_cycle_runs ORDER BY started_at DESC LIMIT 5;
-- skim coverage and cost per run
SELECT run_id, model, prompt_version, count(*), sum(cost_usd), count(*) FILTER (WHERE truncated) AS truncated
  FROM memory_skim GROUP BY 1, 2, 3 ORDER BY max(created_at) DESC;
-- arrivals in a window (parent-level, not superseded): what the skim should have covered
SELECT source_type, count(*) FROM memories
 WHERE parent_id IS NULL AND status IS DISTINCT FROM 'superseded' AND created_at >= now() - interval '3 days'
 GROUP BY 1 ORDER BY 2 DESC;
-- salience spread of the current prompt version
SELECT skim->>'salience' AS s, count(*) FROM memory_skim WHERE prompt_version = 'skim-v2' GROUP BY 1 ORDER BY 1;
```

Healthy: `skim_failed` and `skim_invalid` at zero or a handful; `skim_quotes_unverified` a small fraction of `skim_quotes_emitted`; `explorer_input = inventory` with `inventory_listed = inventory_docs`; `skim_aborted_on_cap` false; and the arrivals share of the examined memories well above the manifest era's ~50% (91% in run `05171b48`, 52% in `3a1263a6`). Watch `skim_coerced` (summary and quote overruns) and the salience spread; if salience compresses again, revisit `config/skim.toml` and bump `PROMPT_VERSION`.

**Disabling the skim.** Set `SECOND_BRAIN_SKIM_DISABLED=1` (also `true`/`yes`) in the run's environment: `_skim_arrivals` logs one WARNING, makes no model calls and returns `None`, so the Explorer gets the manifest exactly as before the skim existed (the LaunchAgent job inherits the environment from `scripts/jobs/dream_cycle_scheduled.sh`, so export it there or in the plist). Removing the `skim` role from `config/backends.toml` is not the way: the resolver requires the role in every profile and rejects the profile at config load (`missing required role(s): ['skim']`, pinned by `tests/test_resolver.py`), so the run would not start at all. A tiny `SECOND_BRAIN_SKIM_CAP_USD` is not a substitute either — the runner checks the sub-cap before each document, so one document is still skimmed and the Explorer would get a one-line inventory.

## Privacy

- Document text goes to exactly one place: the skim model on Bedrock, inside the user's AWS account, over the `us.` geo inference profile (US regions; Nova is first-party with zero data retention by default — `docs/RESEARCH-skim-model.md` §4.1). Nothing from the skim reaches Kiro or any agentic CLI: the `skim` role is rejected on every MCP-capable backend at config load and at resolution, in every profile.
- Two code paths read `memories.content` for the skim — `fetch_documents` in `src/skim/store.py` inside a run, and `scripts/skim_probe.py` at the user's request. Logging in the runner, the cluster step and the orchestrator carries counts and ids only; the probe's stdout is aggregates only.
- `logs/skim_probe/*.jsonl` contains document-derived text (records, quotes, validation details). It is gitignored and stays on the machine; delete it when a probe is done.
- `memory_skim.skim` holds summaries and verbatim quotes of the user's documents. It lives in the same database as the documents and reaches the Explorer (Claude Code on Bedrock in profile `controlled`) through the inventory — the same boundary the memories themselves already cross.
- `config/skim.toml` is committed on purpose: the name, role, organisation and focus areas already appear throughout the corpus; it carries no credentials or contact details.

## Files

- `src/skim/schema.py` (record, `SKIM_SCHEMA`, `validate_record`), `src/skim/faithfulness.py` (quote check, `stored_form`), `src/skim/identity.py` (reads `config/skim.toml`), `src/skim/store.py` (`memory_skim`), `src/skim/runner.py` (`skim_window`, `SkimResult`), `src/skim/cluster.py` (embedding clusters), `src/prompts/skim.py` (prompt, `PROMPT_VERSION`).
- `src/backends/bedrock.py` (Nova/Gemma request shaping), `src/backends/cost.py` (prices, Flex tier), `src/backends/resolver.py` (`skim` role, `options` tables), `config/backends.toml`, `config/skim.toml`.
- `src/dream_cycle/orchestrator.py` (Step 3.5, `_skim_cap_usd`, judging order, soft stop), `src/dream_cycle/allocation.py` (inventory, weighted slice, `order_slices`), `src/prompts/explorer.py` (inventory section).
- `migrations/014_memory_skim.sql`, `scripts/skim_probe.py`, `tests/test_skim*.py`, `tests/test_budget_order.py`, `tests/test_allocation.py`, `tests/test_resolver.py`, `tests/test_cost.py`, `tests/test_bedrock.py`.
