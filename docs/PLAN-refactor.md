# Refactor plan — streamlining Second Brain (v2)

**Status:** proposed, not started. **Base:** `main` @ `6b8c2d2`. **Evidence:** [AUDIT-2026-09-24.md](AUDIT-2026-09-24.md) and the [findings register](audit-2026-09-24/00-FINDINGS-REGISTER.md) — every item below cites its `F-nnn` rows. This plan supersedes v1 ([archived](audit-2026-09-23/PLAN-refactor-v1-superseded.md)); the changes are itemised in the [errata](audit-2026-09-24/00-ERRATA-for-AUDIT-2026-09-23.md) §2.

**How to pick this up later.** Phases are independent and shippable in order; each ends with the hermetic suite green. Within a phase, items are numbered in dependency order — where the order matters it is stated. Each item is small enough to be a spec on its own. When an item is done, mark it here and in the register.

**Constraints that hold throughout**
- One branch per phase, merged to local `main`. Nothing is pushed.
- Working-tree edits under `docs/`, `README.md`, `scheduling/README.md`, `.kiro/specs/**`, `src/backends/kiro.py` are yours; nothing touches them until decision D1 resolves. Phase 6 is blocked on it.
- Corpus content never reaches Kiro or Fable 5.1 — including while doing this work. Use `SECOND_BRAIN_PROFILE=controlled` in every shell.
- Suite: `AWS_PROFILE= AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= AWS_DEFAULT_REGION=us-east-1 .venv/bin/pytest -q -p no:cacheprovider -o addopts=""` — 1,630 passing at base, ~51 s.
- No schema change without a migration; no migration that names a database literally; `migrate.sh` is ported (item 1.13) before any new migration runs.
- Merge duplicates before creating unique indexes; fix expiry before filtering expired edges; admit live vocabulary values before adding CHECK constraints.

---

## Phase 0 — Tonight, and five decisions

### Stop the bleeding — DONE 2026-09-24
- **0.1 ✅** `com.second-brain.distill` booted out and `launchctl disable`d (persists across login). Plist symlink left in place. Reverse with `launchctl enable gui/$UID/com.second-brain.distill && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.second-brain.distill.plist` — only after item 1.2. (F-003)
- **0.2 ✅** `scripts/jobs/dream_cycle_scheduled.sh:44` express_push call commented out with a pointer to this plan. Uncommitted. (F-001)

### Decisions (yours)

| # | Decision | Recommendation |
|---|---|---|
| **D1** | The uncommitted doc layer (87 working-tree entries, 13 days old, 67 commits behind HEAD): commit as a dated snapshot, or discard? | **Commit as-is first.** Phase 6 then reconciles it as a reviewable diff. Its epistemics are the best in the repo; only its facts are stale — and it documented two hazards (`VALIDATION.md:19,56`) that this pass rediscovered. |
| **D2** | Storage: dedup and re-chunk. 261 MB of byte-identical IDE chunks, 1,491 duplicate parents, 14,338 oversize chunks, 19.8 MB unembedded tail. Order is fixed (dedup → chunker fix → re-chunk/re-embed); the question is scope and spend. | Do all of it in Phase 1 as one resumable, dry-run-first batch. Re-embed cost is priced after dedup on the true remaining count (est. 5,937 parents / 14,311 chunks for the 4 k limit). |
| **D3** | Codex capture data-locality. Upstream's design ran the semantic pass on the local Codex CLI with local Ollama embeddings — $0, nothing left the machine. The port replaced that with Bedrock Opus 5 + Titan and removed `CodexInvoker`'s only caller. Restore `codex_local`, or record the inversion as explicit policy? | Either is defensible; **metering the pass is Phase 1 regardless** (item 1.4). If you keep Bedrock, delete `src/backends/codex.py` (620 LOC) in Phase 3. If you restore locality, it is the profile that comes back, not a rewrite. |
| **D4** | Knowledge graph (`entities`/`entity_edges`/`memory_entities`, 77 k rows): zero readers, but written **hourly** by `import_slack_graph.py` (648 k updates), with a `vector(1536)` column no 1024-dim embedder can fill. Stop the writer and drop the tables, or build a reader? | **Stop the writer and drop the tables** (Phase 3). Reading it is a feature, not a refactor — if you want it, say so and it becomes a spec. `idx_entities_embedding` is safe to drop unconditionally. |
| **D5** | `kiro_ide_chat` — 59 % of the corpus, dead since 2026-05-17, new session store at `~/.kiro/sessions/<hash>/` unparsed. Revive the channel against the new format, or retire it explicitly? | **Revive.** It is your primary work surface; the parser is the only piece missing. Retire only if you have stopped using Kiro IDE. |

Still open from earlier, unrelated to the refactor: the identity name in `config/skim.toml` (`"Kerr First"`, never confirmed); Friday's 02:00 dream cycle and the closed lid.

---

## Phase 1 — Correctness and safety

Small, localised edits; nearly all the value. Ship as several merges. **Order within the phase matters and is given.**

**1a. Close the routing rule — first merge, these fire nightly**
1. `express.py:481` — route through `default_resolver().invoker_for(SKIM_ROLE)` with a `SpendTracker` (the pattern `weekly_digest.py:60` already gets right); gate `edit_briefing` on `email_configured()` *before* the LLM pass; narrow the `except Exception` at `:496`. (F-001)
2. `scripts/distill_sessions.py:150`, `redistill_capped.py:126` — a Bedrock-pinned `distill` role in every profile, resolved through the resolver, metered; detect model refusals and mark the session `do_not_send`; non-zero exit on failure (today: exit 0 at 100 % failure). Re-load the plist after. (F-003)
3. `memory_brief(use_llm=True)` — route or remove the LLM branch; add the missing `try/except ValidationError`. (F-002)
4. Codex semantic pass `codex.py:189` — own Bedrock-pinned role, `SpendTracker` with a per-run cap, incremental (new-turns-only) re-send instead of the whole tail, and distinguish infrastructure failures from contract failures in the attempt budget. (F-005, F-084)
5. `resolver._validate_profile` — `CORPUS_ROLES`, `DENIED_BACKENDS_FOR_CORPUS={"kiro"}`, `DENIED_MODEL_SUBSTRINGS={"fable"}`; fail at config load like the skim rule does. `default_profile` → `controlled` in `backends.toml:33` and `resolver.py:153`. Delete `hybrid`; move `bedrock`'s Thinker off Fable. Flip `tests/test_resolver.py:64,69-79,97-107` and the other wrong-way pins listed in F-127. `_note_model_honored` raises for corpus roles. Clean the orphaned `~/.kiro/agents/dream_cycle_*.json` (`tools:["*"]`). (F-006, F-008, F-127, F-149)
6. Repo-wide test: no module outside `src/backends/` constructs `KiroInvoker` or `AgentInvoker`; no committed profile routes a corpus role to Kiro or a fable model. Retire `src/agent_invoker.py` once 1–3 land.

**1b. Corpus integrity — dedup before anything is re-embedded**
7. Fix `chunk_by_section` to hard-split oversize pieces on line boundaries; property test (`max(len(c)) <= max_chunk_chars * 1.1` for header-less, blank-line-free input — the function has zero tests today). Make `generate_embedding` count and report truncation instead of silently slicing. Drop or persist the whole-body embedding `_discover_relationships` computes and throws away. (F-020, F-021)
8. `ide_chat.py` — strip the repeated context block before chunking (the source of 62 k identical chunks). `ingest_batch` — `already.add(source_url)` after each success; refuse `source_url IS NULL`. (F-022, F-023)
9. `scripts/maintenance/dedup_corpus.py` — dry-run default, resumable: merge the 1,491 duplicate parents by content hash (keep the row with relationships/access), collapse the 68 double-stored CLI sessions, remove the 10 residual `PSROJpBIZ4w` parents, delete-and-re-chunk the 5,493 IDE parents. **Then** migration: unique partial index `(source_type, source_url) WHERE parent_id IS NULL`. (F-023)
10. `scripts/maintenance/rechunk_and_embed.py` — re-chunk/re-embed the oversize and tail rows, repair the 18 half-written parents, embed the 1,087 unembedded leaves. Priced on post-dedup counts; blocked on D2. Make `ingest_content` write parent+chunks in one transaction so this cannot recur. (F-026, F-027)
11. `cli_chat.py:82-85` — stop using the session filename as `conversation_id`/project; null out the 2,675 UUID projects. (F-028)

**1c. Relationships — expiry before filter**
12. `expire_stale_relationships` — require `created_at < now() - threshold` *and* both endpoints unaccessed; never expire `derived_from`/`superseded_by` (port origin `012`'s CHECK). Backfill: un-expire edges with `expired_at - created_at < threshold`. **Then** `get_relationships` filters `expired_at IS NULL` by default, returns the neighbour id the JOIN already computes plus the joined title/type, and canonicalises symmetric types so `related_to` is not returned twice. (F-025, F-045, F-046)

**1d. Data layer and migrations — before any new migration**
13. Port `migrate.sh` from origin `91d2925` (`ON_ERROR_STOP`, advisory lock, SQL + ledger insert in one transaction) and origin's 009 (`EXECUTE format(... current_database())`). Pick a local migration number that does not collide with origin's 012–015 fork. (F-103, F-104)
14. Migration (one file): 009 fix; name the FTS index and rename to match production; `memories.parent_id` index; drop the three zero-scan indexes; `dream_cycle_runs.cost_usd`; admit `correction_episode` to `MEMORY_TYPES` and `source` to `MEM_CLASS_BOOST` **before** any CHECK constraint; `SimpleConnectionPool → ThreadedConnectionPool` (`db.py:14,35` — one word, and the design.md rationale "single-threaded" is false). `ALTER ROLE memory_bank NOSUPERUSER` with a separate owner role — *after* 13. (F-105…F-110, F-138)
15. MCP surface: `memory_recent_activity` through `validation.clamp_*` (the `lo`/`hi` params already exist, unused); `memory_learn` topic cap; `_guarded` stops forwarding Postgres `message_primary`; translate `OperationalError`/`PoolError` at the tool boundary; a `SOURCE_TYPES` vocabulary built from the 27 live values. (F-050…F-057)

**1e. Dream-cycle run integrity**
16. Validate `verdict ∈ {ACCEPT, REJECT}` in `EvaluatorVerdict.__post_init__` (make the `ValueError` retry arm reachable); send seats a candidate projection **without** `revision`; move the grounding audit off a voting seat. (F-061, F-062)
17. Carry real counts through every abort path (`_ZERO_STATS` only before any candidate exists); `except SpendCapExceeded: raise` before the Thinker loop's broad except at `:442`; complete the run with `aborted` in the `OperationalError` arm; one-off UPDATE of runs `3a1263a6`, `bfe245b1` from their candidate rows; flip `test_budget_order.py:228-240`, `test_dream_cycle.py:2707-2711`. Write `cost_usd` from `SpendTracker` at `complete_run`; re-fit `EST_JUDGE_CANDIDATE_USD` to the measured $0.40. (F-063, F-069)
18. Persist per candidate inside the loop's `try`; validate `relationships` (`RELATION_TYPES`, target exists) before `store_accepted`. `MAX_CANDIDATES_PER_SLICE` = 2, formula references the constant. Move `generate_digest` after `complete_run` (or pass `feedback_injected` in). `pg_try_advisory_lock` around `run()`; wall-clock `timeout` in `job_wrapper.sh`. (F-064, F-065, F-066, F-067)

**1f. Test isolation**
19. Session-autouse `DB_CONFIG` override; delete the four shadowing `test_db` fixtures; rename the conftest fixture; `assert TEST_DB_NAME.endswith("_test") and != DB_CONFIG["dbname"]` at import; synthetic fixture ids (not `qd-feed://evt_00001`); remove the four `pytest.skip` paths; `_apply_migrations` mirrors `migrate.sh` and writes the ledger. Autouse offline guards for `src.embeddings._get_client`, `BedrockInvoker._lazy_client`, agent-CLI subprocesses; `KIRO_LLM_METRICS_DIR` → `tmp_path` (today 92 % of `adhoc.jsonl` is test pollution). (F-121…F-125)

**1g. Security**
20. Encrypt or stop the `kb-sources`/`docs` rclone sync; second custody copy of `.backup-key`; `chmod 600` the `~/.aws/*.backup` files and remove the static key; upgrade `mcp` to ≥ 1.28.1 and `yt-dlp`; scrub the real session ids/home path/email from `tests/fixtures/codex/real_*`. (F-135, F-136, F-139, F-140, F-145)

**Exit criteria:** suite green; a test fails if any profile or module routes corpus content to Kiro/Fable; a test fails if `chunk_by_section` exceeds its limit; duplicate parents = 0 and the unique index exists; `memory_graph` returns real neighbours and live edges only; `dream_cycle_runs.cost_usd` populated on the next scheduled run; `pip-audit` clean.

---

## Phase 2 — Make failure visible

1. Exit codes for browser, youtube, crawlee_ingest, scrape_bookmarks, distill; `ingest_staged.sh` — capture the Python exit code at `:35` and delete the unconditional `exit 0` at `:70`; `youtube` dry-run stops counting `processed`. (F-031…F-034)
2. `job_wrapper.sh` owns PATH (with `/usr/local/bin` and `postgresql@17` — the fix applied to two scripts, not the wrapper that was named as root cause), `AWS_PROFILE`, repo `cd`, venv, `caffeinate`, log path + rotation, notification, exit passthrough, **a lock and a per-job timeout**. Bring `qd-sync`, `codex-capture`, `capture-api` under it; per-job scripts keep only the command and their exit taxonomy. Comment why the wrapper omits `set -e`. (F-037…F-040)
3. Extend `verify_liveness.sh` first (it exists and works): watch `distilled_chat`, `weekly_digest`, `dream_cycle_runs.completed_at`, and add a "channel that ever produced output has produced zero for N days" predicate — the rule that would have caught F-083 four months ago. Then a machine-readable status line per job and a sweep for "last exit non-zero for N days". Notifications alone cannot work on a headless host. (F-036, F-041, F-042)
4. Narrow the six broad excepts (catch `psycopg2.Error, OSError, ValueError, KeyError`; re-raise programming errors); `SkimResult` distinguishes call failures from store failures and trips a threshold; `qd_sync.sh` stops swallowing tracebacks. (F-043, F-044, F-093)
5. Delete `codex-canary` (plist has `/path/to/` placeholders; script is the unfixed Ollama-gated ancestor) with its test; symlink the two installed plists that are copies; export `DIGEST_TIMEOUT`; empty digest summary exits non-zero. (F-035, F-166)

**Exit criteria:** every scheduled job can fail; every failure reaches a channel that works headless; a deliberately broken channel is detected within one cycle.

---

## Phase 3 — Delete

Do this before restructuring. The list is corrected from v1 — 7 items have callers and need a test edit or retarget first (F-166).

- Unreachable adapter seams: `use_invoke_model`/`_invoke_model_fallback`, `system_prompt_delivery="prepend"`, the Gemma path and its price rows, `_LEGACY_THINKING_BUDGETS`. **Keep** the `claude-opus-4` price row — live for `mini`. (F-171)
- `DEFERRED` columns and `--prompt-variant` (both provably impossible); `get_previous_run_id`; `type_distribution`; `get_schema_with_constituents` and its dead import.
- With retargets: `find_schemas_for_memory` (inline the `test_db.py:186` oracle), `dream_cycle_db.get_memory_provenance` (retarget `ledger.py:194`), `allocation.build_arrivals_*` and `store.fetch_records`/`run_summary` (test-only — delete with their tests or keep as documented test API).
- Scripts: `ingest_chats.py`, `batch_ingest_parallel.py`, `run_golden_capture.py`, the three `scripts/migrate/migrate_{claude,chatgpt,notion}.py` (dead on arrival, `ModuleNotFoundError`). Rename `scripts/migrate/` — nothing in it is a migration: 6 recurring jobs move beside the other channels, 6 one-shots to `scripts/maintenance/`. (F-172, F-174)
- `src/backends/codex.py` — per D3.
- Knowledge graph writer and tables — per D4.
- Disk: `scripts/backups/` (62 MB of plaintext internal wiki — a confidentiality fix, not tidying), `scripts/logs/`, `src/logs/llm_metrics/`, `tests/fixtures/golden_run/` (452 KB, read by zero tests), the 24 k test-polluted `adhoc.jsonl` records. Anchor `.gitignore` **after** deleting — anchoring first makes 62 MB of internal content trackable. Add `.dream/`. (F-143, F-167, F-168)
- Docker vestiges (`CONTAINER=` ×3, "via docker" comments); `BackendCapabilities.structured_output`; `if TYPE_CHECKING: pass`.

Rough scale: ~2,000 LOC of `src/`+`scripts/`, ~65 MB disk, and the 640 lines of tests Phase 4d removes.

---

## Phase 4 — One copy of each thing

**4a. Data access.** `src/db/pool.py` (the only `psycopg2.pool` import; `ThreadedConnectionPool`), `src/db/columns.py` (one `MEMORY_SCALAR_COLUMNS`, one `memory_summary(row)`, one `_iso` — replacing three column lists, six projections, three date idioms), `src/db/memories.py`, `src/db/relationships.py`. `search.py` takes candidate rows from the repository; adopt its `psycopg2.sql.Identifier` pattern everywhere including `update_memory`. Retire the five access styles to one, starting with `express.py`'s cursor injection and its second connection at `:385`. (F-112…F-118)

**4b. Capture framework.** The nine channels already implement the same five steps: preflight → window → render `(source_url, markdown)` → ingest → exit code. `src/capture/framework.py` with `run_channel()`; `render.py` with one underscore-only header vocabulary (retiring `"thread replies"`, `"video id"`) and one `html_to_text`; `window.py` with DST-correct zoneinfo windows; `filters.py` as the single URL-filter vocabulary (unifying the two readers of `index.md`). Slack is the reference (all five steps); Outlook next; then Browser and YouTube (where the exit-code gap closes); then the QD scripts. **From Codex take only the transaction / `FOR UPDATE` / progress-cursor shape** — not its retry or semantic-pass design. Deleting the two-phase staging hop removes four scripts. Decompose `fetch_task` (CC 79) per track 03 §7. (F-085…F-097)

**4c. Backend base.** Hoist into `base.py`: one `_init_metrics` with one key vocabulary (`latency_s` vs `duration_s` gives the metrics log two schemas), one `_parse`, one `_warn_missing_usage`, one `normalize_usage()`, one `price_and_record()`. Collapse the three `METRICS_DIR` re-exports. (F-013…F-018)

**4d. Tests.** Promote `FakeInvoker` (`test_skim_orchestrator.py:43`), one `mock_conn()`, and a shared strategies module into `conftest.py`. Delete ≈640 lines (the signature registry, the self-comparing SQL test, the tautologies listed in track 12b §8); replace `test_dream_cycle_db.py`'s 42 mock-driven SQL-substring tests with ≈8 real-DB round-trips (pattern in `test_ledger.py:95`); fix the vacuous `test_no_deferred_verdicts_and_zero_deferred_count`; make `test_rerank.py`'s oracle draw `encoding_context` and `status` so `context_overlap`/`superseded_penalty` are actually pinned. Target: suite ≤ 20 k lines with the F-128 gaps covered. (F-126, F-131…F-134)

**4e. Vocabulary.** `validation.py` the only declaration; `classify.py` derives from it; `capture_api`'s three off-taxonomy `source_type` values fixed. Then a CHECK constraint — *after* 1.14 admitted the live values. (F-108, F-119)

---

## Phase 5 — Split the large modules

After 3 and 4. `dream_cycle/orchestrator.py` (1,201 lines, 7 responsibilities) → `budget.py`, `stages/skim_stage.py`, `stages/grounding.py`, `agents/{explorer,thinker,evaluator}.py` over a small `AgentRunner`, `run_result.py`, verdict→DB mapping into the DB layer. Extracting `grounding` turns `panel_replay`'s four private reach-ins into a supported call. `dream_cycle_db.py` (994 lines, 5 concerns) → runs, feedback, briefing (policy out of the DB layer), metrics + `golden_queries` (no DB access at all), attention; a `Protocol`-typed DB dependency removes the two test-seam shims. `hybrid_search` → the six pure functions in track 09a §6, with a JSON-fixture seam. `digest.py` → pure `render_digest()` + thin writer. Orchestrator talks to `src.skim`, not `skim.store`/`skim.cluster`. (F-079…F-082, F-047)

---

## Phase 6 — Documentation reconciliation (blocked on D1)

1. Promote the six ownerless contracts into canonical docs: panel acceptance; the `controlled` profile and the Kiro-`--model`/Fable-PII hazard (today only a TOML comment); the $9 cap (today only a shell script); the `quick_desktop_doc` retirement; the four newer capture channels; the Codex data-locality decision (D3). (F-153)
2. Re-pin layer A to HEAD: adapters 4, profiles 5, roles 7, plists 18, migrations 014, cadence Mon/Wed/Fri 02:00; delete START-HERE's "Current local observation"; add Step 3.5 and the audit/revision stage to ARCHITECTURE; regenerate OPERATIONS' schedule table from the plists; delete CAPTURE-COMPONENTS' "no native Codex/Outlook collector"; README drops "do not merge" (the histories are disjoint — it is a port, and two upstream fixes are now in Phase 1). Correct "233 commits" → 67 wherever it appears. (F-151, F-152, F-161)
3. Add a **known-hazards list with owners** to VALIDATION.md, checked by `test_repo_hygiene.py` — the actual failure was that layer A recorded the 009 and fixture hazards on 09-10 and nobody closed them. (F-163)
4. Delete the 35 redirect stubs (archive holds the originals); archive the four completed `PLAN-*.md` and `RESEARCH-skim-model.md` after promoting their contracts; ~50 files → ~18 with content. Terminate the authority chain on code; remove every deferral to the non-existent "local operational inventory". (F-162, F-154)
5. Specs: per track 08a/08b's verdict table — archive the drifted, update the four still-load-bearing, and create the eight missing families the refactor needs (routing enforcement, corpus dedup, relationship lifecycle, run ledger, capture framework, observability, retrieval regression gate, data-locality policy). Fix SPEC-INDEX (13 families; the `direct-api-backends` prose is inverted). (F-155…F-160)
6. One name per concept: panel / consensus / seat; channel ≠ arrival ≠ slice; retire BFT/Byzantine/quorum from live docs.

---

## Phase 7 — Guardrails

One test per defect class, so none of this recurs — most are one-liners once Phases 1–4 land:
- No profile or module routes a corpus role to Kiro/Fable (global, not per-profile). No invoker without a `SpendTracker`.
- `chunk_by_section` never exceeds its limit; `generate_embedding` never silently truncates; `ingest_batch` never writes a duplicate `(source_type, source_url)`.
- Every capture channel maps failure to non-zero; every `source_url` builder rejects empty components.
- Every MCP tool argument passes through `validation.py`; verdict strings validated; `expire_stale_relationships` respects the grace; `memory_graph` returns neighbours.
- No migration names a database literally; `migrate.sh` records only fully-applied files.
- Docs-vs-code check for the counted facts (adapters, profiles, roles, plists, migration high-water, cadence, cap).
- **A retrieval regression fixture that runs without Bedrock**: 30–50 committed query embeddings + expected ids, asserting hit@k, distinct-result count, FTS reachability of parents, and top-k stability (Kendall τ ≥ 0.8). Today no retrieval-quality gate exists anywhere in the repo; this must exist before Phase 4b/5 refactor `hybrid_search`. (F-130)

---

## Effort and payoff

| Phase | Work | Risk | What you get |
|---|---|---|---|
| 0 | 2 one-line changes + 5 decisions | — | Routing violations stop tonight |
| 1 | ~30 localised edits, 1 migration, 2 maintenance scripts | Low; batch scripts dry-run first | Rule enforced in code; 340 MB duplicates gone; graph and ledger truthful; test suite cannot touch production |
| 2 | Shell consolidation + liveness predicates | Low | Every failure visible, including headless |
| 3 | Deletion | Low (git) | ~2,000 LOC, 65 MB, one hourly job |
| 4 | Consolidation | Medium — touches every channel | One copy of each thing; suite −25 % |
| 5 | Module splits | Medium | The four E/F-complexity units become reviewable |
| 6 | Docs + specs | Low | An entry point that is true; specs to build from |
| 7 | Guardrails | Low | The five defect classes stop recurring |

**Not in scope:** new features, reading the knowledge graph, reviving Express email delivery (a product decision — the code is complete and has never been configured), the Astra `effort=medium` cost lever, and reconciling the rest of `origin/main` beyond the three fixes ported in Phase 1.
