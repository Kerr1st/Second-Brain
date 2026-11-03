## review_dream_cycle

=== Context Gathering Summary ===
FOUND: Full dream_cycle package (orchestrator, allocation, ledger, consensus, storage, packet, digest, feedback), full skim package (runner, schema, store, faithfulness, identity, cluster), the five prompt modules, dream_cycle_db.py, config/skim.toml, all four scripts, all five migrations; supporting evidence from src/backends/cost.py (SpendTracker, DEFAULT_RUN_COST_CAP_USD=5.0), src/backends/resolver.py (AGENT_ROLES + SKIM_ROLE), src/db.get_arrivals_summary, src/models.py dataclasses, src/prompts/__init__.py, scripts/jobs/dream_cycle_scheduled.sh (live cap=9, profile=controlled); grep-verified production vs test-only callers for 17 candidate-dead symbols.
NOT FOUND: No runtime/DB verification (read-only): zeroed-stats runs, actual slice-position drift, existing null quotes in memory_skim. docs/ and .kiro/specs/ excluded by instruction, so code-vs-plan agreement for PLAN-skim-stage/PLAN-panel-acceptance constants is unverified. src/backends/{base,bedrock}.py and config/backends.toml read only in part. Test quality not assessed.
====================================

=== CONTEXT GATHERING COMPLETE ===

ANSWER:
# Dream cycle + skim subsystem review (HEAD 6b8c2d2, read-only)

Nothing was modified. All claims below are from files I read in full: `src/dream_cycle/*.py`, `src/skim/*.py`, `src/prompts/{explorer,panel,revision,skim,thinker}.py`, `src/dream_cycle_db.py`, `config/skim.toml`, the four scripts, the five migrations, plus the supporting files needed to judge them (`src/backends/cost.py`, `src/backends/resolver.py`, `src/db.get_arrivals_summary`, `src/models.py`, `scripts/jobs/dream_cycle_scheduled.sh`).

---

## 1. CONTRADICTIONS

**C1 — HIGH — Two failure paths disagree about whether persisted work is reported.**
`src/dream_cycle/orchestrator.py:498-506` (Step 7 comment) promises: *"keep the candidates already evaluated so the run record reflects real persisted work (no zeroed-stats lie / orphaned accepted memories)"*. The evaluator-abort path honours this (`panel_aborted = True; break` at :531, then Step 8 runs). The spend-cap path does not: `SpendCapExceeded` from `_record_spend` propagates out of `_run_pipeline` to `run()` :327-333, which calls `complete_run(run_id, stats=_ZERO_STATS, digest="")`. Candidates and accepted *memories* are already committed to the DB, but the run row says `candidates_generated=0/accepted=0/rejected=0` — exactly the "zeroed-stats lie" the comment forbids. Same for the generic `except Exception` at :336-347.
Fix: hoist `all_candidates/accepted/rejected` into instance state (or return them from `_run_pipeline` via a result object) and complete the run with the real counts on every abort path; reserve `_ZERO_STATS` for aborts that happen before any candidate exists.

**C2 — HIGH — Per-slice candidate cap (3) contradicts the Thinker contract (2) and the budget arithmetic (2).**
`orchestrator.py:105` `MAX_CANDIDATES_PER_SLICE = 3`, with the comment at :104 "the Thinker is asked for at most 2". `src/prompts/thinker.py` says twice "Return at most 2 candidates per slice" / "Generate 0-2 candidate insights". The budget pre-check at :428 assumes exactly two new candidates: `needed = EST_THINKER_CALL_USD + 2 * EST_JUDGE_CANDIDATE_USD + …`. So a compliant-but-greedy Thinker yields 3 kept candidates while the budget reserved money for 2 — a systematic ~$0.70 per-slice underestimate against a $9 cap.
Fix: pick one number. Either set `MAX_CANDIDATES_PER_SLICE = 2` (matching the prompt) or make the budget formula use `MAX_CANDIDATES_PER_SLICE` instead of the literal `2`.

**C3 — HIGH — The "independent four evaluators" claim is violated by the grounding audit seat.**
`src/prompts/panel.py:4` and the per-call prompt at :232-234 tell every seat "Four independent evaluators … your verdict … is combined with theirs". But `orchestrator.py:103` `GROUNDING_AUDIT_ROLE = "skeptic"`, and `_ground_candidate` (:1131) audits the candidate with the *skeptic* seat, whose findings drive the revision — then `_run_pipeline` :523 asks the same skeptic to vote on the text produced from its own issue list. The skeptic votes on its own correction; independence for seat A is gone, and a 3-of-4 quorum with one correlated seat is weaker than advertised.
Fix: either use a dedicated non-voting audit role (add a `grounding` role to `VALID_ROLES`/profiles) or exclude the audit seat from the vote and tally 3 seats with an explicit threshold. At minimum stop claiming independence in the prompt.

**C4 — MEDIUM — The anchored-slice thresholds are encoded twice with a copy in raw literals.**
`src/dream_cycle/allocation.py:58-59` define `ANCHOR_ARRIVAL_SHARE = 0.6`, `ANCHOR_MIN_SOURCES = 2`, consumed by `is_anchored` (:95). `src/dream_cycle/ledger.py:125` recomputes the same predicate with hard-coded literals: `m["arrival_share"] >= 0.6 and m["source_types"] >= 2`. Changing the constant silently desynchronises the enforced budget from the reported `arrival_anchored_slices`.
Fix: have `ledger.run_metrics` import `allocation.is_anchored` (or move both constants into a shared module to avoid ledger→allocation coupling).

**C5 — MEDIUM — Semantic-memory composition limit is 4 in the prompt, 3 in the code.**
`src/prompts/explorer.py:132` "at most 4 semantic memories … per slice"; `allocation.py:64` `MAX_SEMANTIC_IN_ARRIVALS = 3`, and `compose_arrivals_slice`'s docstring (:493-499) claims the value "satisfies the evidence-independence composition rule" — i.e. the rule it cites is stated as 4.
Fix: single constant, referenced by the prompt via f-string (the prompt module already builds strings dynamically elsewhere).

**C6 — MEDIUM — Arrivals filters disagree across four call sites; the guaranteed slice can re-ingest the dream cycle's own output.**
`src/skim/store.py:31-42` (`arrivals_filter_sql`) excludes superseded rows *and* dream-cycle syntheses, because "a run must not skim what the previous run synthesised" (:19-22). `dream_cycle_db.get_recent_parent_memories` (:978-995) — the candidate pool for the *guaranteed arrivals slice* — excludes superseded but **not** dream-cycle outputs, so up to `MAX_SEMANTIC_IN_ARRIVALS` of the run's own prior insights can enter the slice as `generated:insight`. `src/db.get_arrivals_summary` (:200-245) filters neither, and `scripts/skim_probe.py:57-63` has a third hand-rolled variant.
Fix: export one filter (`store.arrivals_filter_sql`) and use it in `get_recent_parent_memories`, `get_arrivals_summary` and the probe.

**C7 — MEDIUM — Inventory header counts documents the inventory deliberately excludes.**
`orchestrator.py:706-712` builds `counts` from `get_arrivals_summary` (unfiltered, includes superseded + `generated:*`) and passes it to `allocation.render_arrivals_inventory`, whose header (:247-250) then reads "N documents across M channels; K of them were read by the skim stage". The N−K gap is partly by-design exclusions, not skim misses, so the Explorer is told the skim covered less of the window than it did.
Fix: compute the header counts from the same filtered query the skim uses.

**C8 — MEDIUM — `run_type` gating: comment says "scheduled", code says scheduled *or* session_start.**
`orchestrator.py:381-383` comment: "On a scheduled run an Explorer failure no longer aborts the night"; the condition at :387 is `if run_type not in ("scheduled", "session_start")`. Session-start runs also survive an Explorer crash and fall back to the arrivals slice, which is a different operational promise than documented (session_start is the frequency-capped briefing, with a restricted strategy set at `explorer.py:SESSION_START_STRATEGIES`).
Fix: align the comment, or restrict the fallback to `scheduled` if that was the intent.

**C9 — MEDIUM — Unknown-backend default is "has tools" in one place, "no tools" in another.**
`orchestrator._role_has_tools` (:161-166) returns `True` when `BACKEND_CAPABILITIES.get(backend)` is None ("assume MCP"), so an unknown backend gets no JSON schema and a tool-enabled Thinker call. `_skim_arrivals` (:936-941) treats `caps is None` as a *skip*. Two opposite fail-safe defaults for the same unknown condition.
Fix: one helper (`capabilities_for(role)`) that raises or returns a single explicit default.

**C10 — LOW/MEDIUM — Terminology drift: BFT / quorum / consensus / panel / acceptance.**
`src/dream_cycle/consensus.py:3-5,12-14` states the rule twice as "Lamport's 3f+1 bound with f=1 … BFT quorum 2f+1 = 3 out of 4"; `migrations/004_evaluator_d.sql:2-3` repeats it; `digest.py:32` says "passed BFT consensus (≥3/4 ACCEPT)"; `orchestrator.py:497` calls it "Step 7: Consensus Panel"; `prompts/panel.py` and `docs/PLAN-panel-acceptance.md` call it "panel acceptance" with a *graded* Standard; `scripts/panel_replay.py:275` prints "3-of-4". Four vocabularies for one 3-of-4 majority vote (and BFT is a misnomer: the seats are different prompts over correlated models, not independent replicas tolerating a Byzantine actor).
Fix: pick "panel acceptance (3-of-4 majority)" and drop the BFT framing, or keep it in exactly one docstring with an explicit caveat.

**C11 — LOW — Role→letter mapping duplicated in four places.**
`orchestrator.py:544-560` (`verdict_a` = skeptic … `verdict_d` = methodologist), `dream_cycle_db.get_evaluator_verdicts_for_run` :737-742, `feedback.py:52-57` (`evaluator_a → "Skeptic"`, `evaluator_b → "User Advocate"`), `scripts/panel_replay.py:SEAT_COLUMNS`. The label also drifts: "User Advocate" (feedback) vs "Advocate" (digest `role.capitalize()`, digest.py:141) vs `"advocate"` (prompts).
Fix: one module-level `SEAT_COLUMNS = {"skeptic": "a", …}` plus display names, imported everywhere.

**C12 — LOW — Scheduled-job comment describes the `hybrid` profile; the job exports `controlled`.**
`scripts/jobs/dream_cycle_scheduled.sh:17-19` explains the cost envelope in terms of "hybrid = Claude roles on Kiro … only the two GPT-6 Astra seats metered"; :32 exports `SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"`. The same comment block (:23-25) claims a cap trip means "candidates already accepted are kept, the digest is skipped" — see C1: the rows are kept but the run's stats are zeroed.

---

## 2. INACCURACIES (docs/strings the code contradicts)

**I1 — MEDIUM — "four-agent pipeline".** `orchestrator.py:1` and `:151` ("Coordinates the four-agent dream cycle pipeline"), plus `src/prompts/__init__.py:1` and `migrations/003_dream_cycle.sql:2-3`. The pipeline is seven roles: `resolver.AGENT_ROLES` = explorer, thinker, skeptic, advocate, epistemologist, methodologist (six) plus `SKIM_ROLE` (`resolver.py:56-69`), and there is a grounding-audit + revision stage. Fix: "Explorer → Thinker → grounding audit → revision → four-seat panel, with a skim pre-stage".

**I2 — MEDIUM — "All 11 named exploration strategies".** `src/prompts/explorer.py:7`; the block lists 12 (1–12, ending in ARRIVALS SYNTHESIS) and `ALL_STRATEGY_NAMES` (:206-219) has 12 entries. The header of the same block also says "use at least 3 per cycle" while `OUTPUT_FORMAT` asks for 0–5 slices — a 0-slice reply cannot use 3 strategies.

**I3 — MEDIUM — `dream_cycle_runs.digest` documented as markdown, stores a path.** `migrations/003_dream_cycle.sql:21` — `digest TEXT -- human-readable summary markdown`; `dream_cycle_db.complete_run` docstring says "digest text" (:50); but `generate_digest` returns `str(file_path)` (`digest.py:178`) and `orchestrator.py:602` passes that path. Every completed run stores a filename, not a digest. Fix: rename the parameter to `digest_path` or store the markdown.

**I4 — MEDIUM — `skim/store.py:12-14` "The arrivals filter mirrors `get_arrivals_summary` / `get_recent_parent_memories` (parent-level, not superseded …)".** `get_arrivals_summary` (`src/db.py:200-245`) filters neither superseded rows nor chunks in its `memories` count, and `get_recent_parent_memories` does not exclude dream-cycle output. The filters are *not* mirrors (see C6).

**I5 — MEDIUM — `_skim_arrivals` error string names a backend the check never tests.** `orchestrator.py:939-941` logs "role %r must run on the direct Bedrock backend, not %r", but the predicate is only `caps is None or caps.supports_mcp` — any non-MCP backend passes. Fix: check the backend identity explicitly, or reword to "must run on a tool-less Direct-API backend".

**I6 — MEDIUM — Skim schema docstring: "Accepts exactly the schema's fields — nothing missing, nothing extra" (`schema.py:340-342`), but the *stored* record violates its own schema.** `faithfulness.stored_form` (:188-202) adds `quotes_unverified` and sets failed quotes to `None`. Re-validating any stored row through `validate_record` would raise `skim record has unknown field(s): ['quotes_unverified']` (`schema.py:348-350`) or `decisions[i].quote must be a string, got NoneType` (:299-301). Latent trap for any future re-validation/backfill path; also means `SKIM_SCHEMA["additionalProperties"]: false` no longer describes what is in `memory_skim.skim`.

**I7 — LOW — `_skim_cap_usd` docstring: "A bad env value falls back to the default with a warning, like `SpendTracker.from_env`" (`orchestrator.py:131-133`).** True for the warning, but unlike `from_env` the result is then clamped by `SKIM_RUN_CAP_SHARE`, so the "default" is often not what is used ($0.75 at the default $5 cap, $1.00 at the live $9 cap). Minor, but the sentence invites the reader to assume $1.00.

**I8 — LOW — `ledger.py:22` "`record_run_slices` does the I/O".** The orchestrator does not call it; `_allocate_attention` (:794-825) calls `compute_slice_rows` + `dream_cycle_db.record_slices` + `log_run_metrics` directly. `record_run_slices` survives only for `scripts/backfill_attention_ledger.py:104`.

**I9 — LOW — `migrations/014_memory_skim.sql:14-15` "cluster_id is reserved for Phase 3".** It is now written by `skim/cluster.py:104` → `store.set_cluster_ids`.

**I10 — LOW — `packet.py:18-20` "a database failure degrades to an empty packet rather than aborting the run"** is true of `load_memories_by_ids`, but the file also says content is truncated "per memory and in total" — `format_sources_for_evaluator` (:139-157) re-renders per-memory content with **no total cap**, so a candidate citing many long sources produces an unbounded evaluator prompt.

---

## 3. DEAD OR UNREACHABLE CODE

**D1 — MEDIUM — `--prompt-variant standard` in `panel_replay.py` is a no-op.** `schema_for` (:106) returns `EVALUATOR_VERDICT_SCHEMA_V2`, which is literally `EVALUATOR_VERDICT_SCHEMA` (`packet.py:159`), and `prompts/panel.get_evaluator_prompt_standard` (:253-263) returns `get_evaluator_prompt(...)` verbatim. Both branches of the "variant" experiment are identical; the only observable difference is the output filename. Fix: delete the alias, the `_V2` name and the `--prompt-variant` flag (or implement a real variant).

**D2 — MEDIUM — `dream_cycle_db.get_previous_run_id` (:775-798) is production-dead.** Superseded by `get_previous_run_started_at` (:897); the only references are test mocks (`tests/test_dream_cycle.py`, `tests/test_pool_properties.py`). Its docstring still says "Extracted from `DreamCycleOrchestrator._get_previous_run_id()`", a method that no longer exists.

**D3 — MEDIUM — DEFERRED verdict machinery is fully dead.** `migrations/003_dream_cycle.sql:42` (`final_verdict -- ACCEPTED, DEFERRED, REJECTED`), `:45 candidates_deferred`, `:47 deferred_twice_rejected`; `complete_run` hard-writes `candidates_deferred = 0` (`dream_cycle_db.py:65`); `consensus.py:4` says "Binary output — no DEFERRED state"; `orchestrator.py:565` comments "REJECTED — no DEFERRED branch". Three columns and a comment maintained for a state that cannot occur.

**D4 — LOW/MEDIUM — Test-only wrappers in `allocation.py`.** `build_arrivals_inventory` (:285), `compose_arrivals_slice` (:501) and `build_arrivals_slice` (:525) have no production caller (orchestrator uses `render_arrivals_inventory` and `build_arrivals_selection`); grep shows only `tests/test_allocation.py`. Three public names documented in the module header (:9, :30) as if they were the API.

**D5 — LOW — Unused skim store/result surface.** `store.fetch_records` (:150) and `store.run_summary` (:317) are referenced only by `tests/test_skim_store.py`; `SkimResult.attempted` (`runner.py:72-74`) and `SkimResult.to_dict` (:104) have no caller anywhere.

**D6 — LOW — `get_memory_stats()["type_distribution"]` is computed and never used.** `dream_cycle_db.py:379-387` runs a `GROUP BY type` query on every run; `invoke_explorer` uses only `total_count`, `date_range`, `recent_activity` (`orchestrator.py:660-687`).

**D7 — LOW — Parameter never passed / default never exercised.** `dream_cycle_db.get_arrivals_summary(samples_per_source=5)` (:973) — the only caller passes `allocation.MANIFEST_SAMPLES_PER_SOURCE = 4`, so the 5 is a phantom default that disagrees with the constant. `scripts/redistill_capped.py:find_capped_sessions(cap=OLD_CAP, …)` (:76) is always called without `cap` (:154), so `--cap` only affects `max_new`, never session selection — despite the docstring "Re-distills each session that has exactly OLD_CAP items".

**D8 — LOW — `get_tier2_metrics` (`dream_cycle_db.py:627`) has no caller** outside `tests/test_pool_properties.py` (a signature registry). `get_tier1_metrics` is likewise only exercised by tests, while `get_golden_queries` *is* used (`scripts/eval/eval_common.py:23`).

---

## 4. DUPLICATION

**P1 — HIGH value for refactor — The arrivals window is recomputed four times per run.** `ledger.arrival_window_start` is called at `orchestrator.py:369` (via `_skim_arrivals` :944), `:856` (`_load_skim_records`), `:699` (`invoke_explorer`) and `:794` (`_allocate_attention`) — four `get_previous_run_started_at` round trips, and four chances to disagree (`invoke_explorer` uses `self._current_started_at`, the others the `started_at` argument; `arrival_window_start` silently falls back to `datetime.now()` when `started_at` is not a datetime, `ledger.py:198`). Fix: compute once in `_run_pipeline`, store on `self._window_start`, pass it down.

**P2 — MEDIUM — "First element of a possibly-list output" repeated five times.** `runner._first` (:113-116), `orchestrator.invoke_evaluator` :1093-1097, `orchestrator._revise_candidate` :1156-1158, `skim_probe.py:129-131`, `panel_replay.py:246-248`. Fix: one `first_object(output)` helper (it belongs next to the invoker contract, not in the skim).

**P3 — MEDIUM — Cost estimation happens in two layers.** `SpendTracker.record` prices the call (`cost.py:191-215`), and `runner.py:224-229` re-prices the *same* usage with `estimate_cost_usd` when the tracker raises, so the skim's `cost_usd` and the tracker's total are computed by two paths. `skim_probe.py` adds a third accounting path (`rec["cost_usd"] = round(tracker.record(...))`), and `panel_replay.py:251` a fourth. Fix: have `record` return `(cost, raised)` or expose `SpendTracker.price(model, usage)` so nothing re-estimates.

**P4 — MEDIUM — Truncation logic implemented five ways.** `allocation._trim_summary` (:190-199, word-boundary + ellipsis), `allocation.build_arrivals_manifest` (:118-120, `"(truncated)"`), `schema.trim_summary`/`trim_quote` (:243-274, sentence/word boundary), `packet.packet_entry` (:104-114, `" …[truncated]"`) and `build_packet` (:126-130, `" …[truncated: packet budget]"`), `feedback._first_sentence` (:143-152). Six markers, six boundary policies.

**P5 — MEDIUM — Fixture-name sanitisation inline three times.** `orchestrator.py:1011` (`slice.name.replace(" ","_").replace("/","_")[:50]`), `:1085` (title, `[:40]`), `:1196` (title, `[:40]`). Fix: `_safe_name(text, limit)`.

**P6 — MEDIUM — Candidate-JSON parsing duplicated.** `feedback._candidate_json` (:155-162), `dream_cycle_db.get_evaluator_verdicts_for_run` :729-734, `dream_cycle_db.get_golden_queries` :484-486, `panel_replay.fetch_candidates` :86-88 — four hand-rolled "maybe a str, maybe a dict, swallow JSONDecodeError" blocks.

**P7 — LOW — Two spend-guard idioms for the same budget.** `_ground_candidate` gates on a *share* of the cap (`GROUNDING_SPEND_SHARE`, :1127), the Thinker/panel loops gate on *absolute dollars remaining* (`EST_*_USD`, :428 and :513). Same objective, two units, two failure modes.

**P8 — LOW — `arrivals_filter_sql` exists precisely to avoid SQL duplication inside `skim/store.py` — and is duplicated anyway in `dream_cycle_db.get_recent_parent_memories` and `skim_probe.fetch_documents` (see C6).**

---

## 5. STRUCTURAL PROBLEMS AND REFACTOR SEAMS

### What `orchestrator.py` (1201 lines) actually does
Seven distinct responsibilities in one class:
1. **Run lifecycle / error funnel** — `run` (:268-347): kill switch, `SpendTracker` reset, session-start frequency cap, run-record creation, and the three top-level `except` arms.
2. **Pipeline sequencing** — `_run_pipeline` (:349-615): steps 2–9 inline, including all budget arithmetic and the verdict→DB dict assembly (:544-560).
3. **Per-role invocation + parsing** — `invoke_explorer` (:651-786), `invoke_thinker` (:962-1073), `invoke_evaluator` (:1075-1117): prompt build, invoker call, spend record, fixture capture, output coercion, dataclass construction.
4. **Attention allocation glue** — `_allocate_attention` (:788-831).
5. **Skim stage glue** — `_skim_arrivals` (:891-960), `_load_skim_records` (:833-889).
6. **Grounding audit + revision** — `_ground_candidate` (:1119-1152), `_revise_candidate` (:1154-1201), including the "sources may only shrink / relationships may only be dropped" invariants.
7. **Cross-cutting utilities** — `_as_int`, `_as_str_list`, `_unwrap_nested_candidate`, `_capture`, `_aborted_result`, `_backend_provenance`, `_skim_cap_usd`.

**Natural seams** (in dependency order, each extractable without touching the others):
- `budget.py` — `EST_*`, `GROUNDING_SPEND_SHARE`, `SKIM_*`, `_skim_cap_usd`, `_budget_remaining`, and two pure predicates `can_afford_slice(remaining, pending)` / `can_afford_candidate(remaining)`. Removes the duplicated units of P7 and makes C2 a one-line test.
- `stages/skim_stage.py` — `_skim_arrivals` + `_load_skim_records` (they already return `None`/`[]` on every failure, i.e. they are already an isolated stage).
- `stages/grounding.py` — `_ground_candidate` + `_revise_candidate` + `_unwrap_nested_candidate` (already used standalone by `panel_replay.replay_grounded`, which reaches into `orch._spend`, `orch._slice_memory_cache`, `orch._current_run_id` — a clear sign this wants to be its own object).
- `agents/{explorer,thinker,evaluator}.py` — the three `invoke_*` methods; each needs only `resolver`, `_spend`, `_capture`, `_slice_memory_cache`. The shared parts (`_record_spend`, `_capture`, `_role_has_tools`, output coercion) become a small `AgentRunner` base.
- `run_result.py` — `_ZERO_STATS`, `_aborted_result`, and the stats-merge logic; fixing C1 means this object must carry real counts through the abort paths.
- The **verdict→DB dict** block (:544-560) plus the empty-content REJECT block (:520-528) belong in `dream_cycle_db` (or a `verdicts.py`), which also kills C11.

### What `dream_cycle_db.py` (994 lines) actually does
Five unrelated concerns behind one module, all sharing only `get_connection`:
1. **Run/candidate writes** — `create_run` (:16), `complete_run` (:46), `store_candidate` (:87), `mark_user_rejected` (:400), `record_slices` (:869).
2. **Feedback reads** — `get_recent_rejections` (:151), `get_user_rejections` (:200), `get_accepted_dissents` (:241).
3. **Scheduling / stats reads** — `get_last_briefing_time` (:287), `should_run_briefing` (:311) *(business logic, not a query — it makes the 24h decision)*, `get_memory_stats` (:361), `get_strategy_usage` (:806).
4. **Metrics/eval** — `get_tier1_metrics` (:520), `get_tier2_metrics` (:627), `get_golden_queries` (:437), `extract_golden_queries` (:480) *(pure text parsing with no DB access at all — misfiled)*.
5. **Attention-ledger reads** — `get_previous_run_started_at` (:897), `get_recent_slice_memory_ids` (:924), `get_slices_for_run` (:950), plus **pass-through delegates** to `src.db` (`get_memory_provenance` :969, `get_arrivals_summary` :973) whose only purpose is "so the orchestrator reaches every attention-related query through this module (which its unit tests patch)" (:965-967).

**Natural seams:** `dream_cycle_runs_db.py` (1), `feedback_db.py` (2), `briefing.py` (3, with `should_run_briefing` moved out of the DB layer since it is a policy decision), `metrics_db.py` + `golden_queries.py` (4, and `extract_golden_queries` has no business in a DB module), `attention_db.py` (5). The delegate shim in (5) is a **test-seam artifact**: a `Protocol`-typed `DcDb` dependency injected into the orchestrator would let the delegates disappear.

### Dependency direction (as it stands)

```
scripts/{dream_cycle_run, panel_replay, skim_probe}
        ↓
src/dream_cycle/__init__ → orchestrator
        ↓            ↓            ↓             ↓            ↓
   allocation     ledger      consensus     storage      digest ──┐
        │            │                         │            │     │
        │            └── (lazy import) ────────┼────────────┘     │
        ↓                                      ↓                  ↓
  src.dream_cycle_db ──(delegates)──→ src.db   src.{classify,depth,embeddings}
        ↓                                                     
   src.skim.{runner,store,cluster,schema,faithfulness,identity}
        ↓
   src.prompts.{explorer,thinker,panel,revision,skim}
        ↓
   src.backends.{resolver,cost,base,bedrock}
```

**No import cycles** — `ledger` and `allocation` avoid one by importing `dream_cycle_db` lazily inside functions (`ledger._dcdb` :169-173, `allocation.build_arrivals_selection` :516-518). That is a cycle *avoided by deferral*, not by layering: both modules still depend on the DB module that the orchestrator also depends on.

**Layering violations worth fixing:**
- **S1 — MEDIUM — The orchestrator reaches into skim internals.** `orchestrator.py:45-47` imports `src.skim.store` (for `fetch_window_records`) and `src.skim.cluster.cluster_window` directly, bypassing `src.skim.runner`. The skim's public surface (`src/skim/__init__.py`) exports none of these. Fix: give the runner a `window_records(...)` entry point and let the orchestrator talk only to `src.skim`.
- **S2 — MEDIUM — `panel_replay.py` reaches into orchestrator privates.** `replay_grounded` (:196-243) mutates `orch._spend`, `orch._current_run_id`, `orch._slice_memory_cache` and calls `orch._ground_candidate`. Four private attributes are a de-facto public API; extracting `stages/grounding.py` (above) turns this into a supported call.
- **S3 — LOW/MEDIUM — `digest.py` is both a formatter and a DB client.** It imports `dream_cycle_db` (:11) for three queries and `ledger.format_attention_section`/`run_metrics` (:12), and performs filesystem writes (:173-176). A pure `render_digest(run, slices, verdicts, accepted, rejected) -> str` plus a thin writer would make it testable without a DB.
- **S4 — LOW — `dream_cycle_db` delegating to `src.db` purely to be patchable** (:965-975) inverts the dependency for test convenience.

---

## 6. CORRECTNESS RISKS

**R1 — HIGH — Spend-cap abort loses the run's accounting (see C1).** A cap trip mid-panel leaves accepted memories in `memories`, candidate rows in `dream_cycle_candidates`, and a run row claiming zero of everything. Every downstream metric (`get_tier1_metrics` acceptance_rate, `acceptance_rate_trend`, `cost_efficiency`) is computed from those run columns (`dream_cycle_db.py:558-566`) and is therefore wrong for any capped run.

**R2 — HIGH — A storage failure on one accepted candidate aborts and zeroes the whole run.** `orchestrator.py:536-563`: `check_duplicate`, `store_accepted` and `store_candidate` are called **outside** the loop's `try` (which covers only the four evaluator calls, :522-531). `storage.store_accepted` does unguarded dict indexing on LLM-supplied data — `rel["target_id"]`, `rel["relation_type"]` (:104-110) — so one malformed relationship raises `KeyError`, unwinds to `run()`'s generic handler (:336), and discards the stats for every candidate judged before it. Fix: wrap persistence per candidate; validate `relationships` entries (the revision path at :1177-1181 already filters by `target_id` but never checks `relation_type`).

**R3 — HIGH — Nothing prevents two concurrent dream-cycle runs, and the spend cap is per-process.** `run()` creates a run row unconditionally (`:314`); there is no advisory lock or lockfile — contrast `scripts/redistill_capped.py:_acquire_lock` (:208-221), which does exactly this for a lesser job. A launchd run overlapping a manual `dream_cycle_run.py` gives two independent `SpendTracker`s, i.e. 2×$9, plus interleaved `record_slices` DELETE/INSERT and duplicate skim work (mitigated only by `upsert_skim`'s `ON CONFLICT`). Fix: `pg_try_advisory_lock` on a dream-cycle key, or reuse the lockfile pattern.

**R4 — MEDIUM — Skim sub-cap is checked only between documents, so one call can overshoot it.** `runner.py:186-192` tests `result.cost_usd >= cap_usd` at the top of each iteration; a single large document can push the pass past the sub-cap by its own full cost. With `max_doc_chars = 120_000` (:47) and Nova 2 Lite at $0.33/M input, the overshoot is bounded (~$0.01) — but the *run* cap has the same shape at a much larger scale: `_record_spend` is called after each Thinker/evaluator call, so a single Astra call (bounded only by `BEDROCK_MAX_TOKENS`, ~$0.90 per the job script) can exceed the cap before anything notices. Fix: pre-flight estimate from prompt size before the call, not only post-hoc.

**R5 — MEDIUM — The budget soft-stop is a no-op on unmetered backends, silently.** `_budget_remaining` (:186-194) returns `cap − total_usd` from *real* usage only; `_record_spend` (:168-184) returns early when `usage` is not a dict. With Kiro seats the total stays 0, so `remaining` is always ≈ cap and no slice or candidate is ever deferred — documented in the docstring, but it means the `hybrid`/Kiro profile has *no* budget control at all, and the live `controlled` profile's control is partial (only the metered seats count).

**R6 — MEDIUM — Broad `except Exception` swallows programming errors in three stages that are supposed to only swallow *infrastructure* errors.** `_allocate_attention` :826-829 ("Attention allocation failed (non-fatal); using Explorer slices as-is") will also hide a `KeyError`/`TypeError` in `enforce_budget` or `slice_metrics` — the run then proceeds unbudgeted with no ledger and one WARNING. Same shape at `_load_skim_records` :864-868, `_skim_arrivals` :954-957, `invoke_explorer`'s manifest block :737-738, `digest.py:94-95`. Fix: catch `(psycopg2.Error, OSError, ValueError, KeyError)` explicitly, or re-raise on `AttributeError`/`TypeError`; at minimum log at ERROR so a broken stage cannot run silently for weeks.

**R7 — MEDIUM — One genuinely silent swallow.** `digest.py:59-62`:
```python
try:
    feedback_injected = dream_cycle_db.was_feedback_injected(run_id)
except Exception:
    pass
```
No log at all; the digest then asserts "No feedback was injected (no prior rejections or first run)" (:107-108) — a false statement produced by a DB error.

**R8 — MEDIUM — `except …: continue` hides real failures in the two hot loops.** `orchestrator.py:456-460` ("Thinker failed for slice '%s', skipping") turns a systematic Thinker/backend breakage into N quiet slice skips and a healthy exit 0, since `exit_code_for` keys only on `aborted_early` (`scripts/dream_cycle_run.py:90-103`). Same in `runner.py:207-212` and `:245-250`: a model failure and a *storage* failure both increment `result.failed`, so a fully broken `memory_skim` write path reports "N failed" and the run completes normally. Fix: distinguish call failures from store failures in `SkimResult`; trip a threshold (e.g. >50% of slices/documents failing ⇒ abort loudly).

**R9 — MEDIUM — Retry counts as "bounded" only per seat; the panel's worst case is 12 calls per candidate.** `EVALUATOR_MAX_ATTEMPTS = 3` with `EVALUATOR_BACKOFF_S * attempt` (:57, :641-644) applies independently to each of the four seats, and `_invoke_evaluator_safe` retries on `ValueError` too — which `invoke_evaluator` cannot raise (it defaults to `verdict="REJECT"` on unparseable output, :1104-1112), so that arm is effectively unreachable. Meanwhile `_ground_candidate` and `_revise_candidate` get **no** retry (:1131, :1160) — an inconsistent resilience policy across calls of identical cost. Note the one thing that is right: `SpendCapExceeded` is deliberately not a `RuntimeError` (`cost.py:164-166`) so the retry loop cannot swallow it.

**R10 — MEDIUM — Digest files collide within a day.** `digest.py:175-176` writes `logs/dream-cycle-digest-{YYYY-MM-DD}.md` with `write_text` — a second run on the same date (scheduled + user_triggered, or a re-run) silently overwrites the first, and both run rows point at the same path. Fix: include the run id or a timestamp.

**R11 — LOW/MEDIUM — Persisted slice order ≠ judging order ≠ `explorer_output` order.** `record_slices` persists positions in pre-order (:820, inside `_allocate_attention`), then `order_slices` reorders the list (:416-418) and `complete_run`'s `explorer_output` is serialised from the *reordered* list (:606). `self._slice_rows` is not reordered, so after :417 it no longer aligns with `slices`. Nothing currently reads it afterwards, but the invariant "rows[i] describes slices[i]" that `order_slices` itself depends on (:551-553) is silently broken from that line on — a trap for the next edit.

**R12 — LOW/MEDIUM — Non-deterministic tie-breaking in stratified allocation.** `allocation.allocate_stratified` :345-356 iterates `sorted(channels, key=lambda c: -channels[c])` with no secondary key, so channels with equal counts are ordered by dict-insertion, i.e. by DB row order. `rank_skim_records` (:203-221) does this correctly (`key=lambda c: (-len(...), c)`), as does `render_arrivals_inventory` (:252). Fix: add the name as a tiebreaker in `allocate_stratified` for reproducible slices.

**R13 — LOW — `check_duplicate` can miss a near-duplicate by construction.** `storage.py:127-129`: `search_similar(embedding, limit=5, status="active")`, then chunk rows (`parent_id is not None`) are *skipped after* the limit is applied — so if the top 5 neighbours are all chunks, a parent duplicate at rank 6 is never seen and the insight is stored twice. Fix: filter chunks in SQL, or over-fetch.

**R14 — LOW — Unbounded evaluator prompt via `format_sources_for_evaluator`.** See I10: per-memory caps apply (4,000 chars) but there is no total cap, unlike `build_packet`'s 120,000. A candidate citing 40 sources yields ~160K chars of prompt at Astra input rates, per seat, per candidate.

**R15 — LOW — `storage.store_accepted` UPDATE path tolerates a missing target.** `:60-83`: when `get_memory(target_memory_id)` returns `None`, `existing_metadata` becomes `{}` and `update_memory` is still called on the non-existent id; only the SUPERSEDE path checks for a missing/already-superseded target (:85-92). Fix: apply the same downgrade-to-CREATE guard to UPDATE.

---

## Could NOT verify

- **Runtime behaviour.** I ran no code, no tests, no DB queries (read-only brief); every claim is static reading. In particular R1/R2's abort paths, R3's concurrency window and R12's tie-break non-determinism are reasoned from the code, not observed.
- **Live DB state.** I did not query `memory_bank`, so I cannot confirm how many runs actually have zeroed stats (R1), whether `dream_cycle_slices.position` disagrees with the judging order in practice (R11), or whether any stored `memory_skim.skim` row currently contains a `null` quote (I6).
- **`docs/` and `.kiro/specs/`** were excluded per instructions (they are uncommitted user edits). Several code comments cite `docs/PLAN-skim-stage.md`, `docs/PLAN-panel-acceptance.md`, `docs/DREAM-CYCLE.md` and `docs/MODEL-BACKENDS.md` as the authority for constants and policies; I could not check code-vs-plan agreement. I did read the *backup* copies under `backups/config/` incidentally via grep (they confirm the $9 cap and the 15% skim share) but treated them as non-authoritative.
- **Whether the "six agent roles" framing in the brief matches a documented design** — the code has six thinking roles plus `skim` (`resolver.py:56-69`), and only four of the six vote. Whether the Explorer/Thinker were *intended* to count toward "six agent roles in the panel" is a design question I cannot settle from code.
- **`src/backends/{base,resolver,bedrock}.py` and `config/backends.toml`** were read only in the parts needed for cap/role/capability claims; they were outside my scope, so C9's blast radius across profiles is unverified.
- **Test coverage quality.** I used `tests/` only to determine whether a symbol has a non-test caller (D1–D8). I did not assess whether the tests would catch C1/C2/R2.

RELEVANT FILES (ranked by importance):
  1. src/dream_cycle/orchestrator.py — Source of the high-severity findings: zeroed-stats abort paths (C1/R1), unprotected persistence in the panel loop (R2), candidate-cap vs budget-arithmetic mismatch (C2), skeptic auditing its own vote (C3), four recomputations of the arrivals window (P1), and the seven responsibilities that define the split seams
  2. src/dream_cycle_db.py — Five unrelated concerns in one module plus the test-seam delegates; holds the dead DEFERRED columns, dead get_previous_run_id, the unfiltered arrivals pool that lets dream-cycle output re-enter the guaranteed slice, and the digest-path-as-markdown inaccuracy
  3. src/dream_cycle/allocation.py — Duplicated anchoring thresholds against ledger.py, the 3-vs-4 semantic-memory contradiction with the Explorer prompt, three test-only public wrappers, and the non-deterministic tie-break in allocate_stratified
  4. src/skim/runner.py — Sub-cap checked only between documents, storage failures counted as model failures, duplicate cost estimation against SpendTracker, and the unused SkimResult surface
  5. src/dream_cycle/ledger.py — Hard-coded 0.6/2 anchoring literals that must track allocation's constants, the arrivals-window helper called four times per run, and record_run_slices documented as the I/O path the orchestrator no longer uses

NOTE: Line ranges have been extended by ~50 lines to provide additional surrounding context.

---

