# Refactor plan — streamlining Second Brain

**Status:** proposed, not started. Evidence: [AUDIT-2026-09-23.md](AUDIT-2026-09-23.md). Base: `main` @ `6b8c2d2`.

**Shape of the problem.** The code is not badly written; it is *nine features' worth of correct code that never had a second pass*. Every subsystem was built, verified, and then left in place while the next one was built beside it. So the defects cluster into five shapes, and almost none of them are local bugs:

1. **A rule is stated in prose and enforced nowhere** — no-Kiro/no-Fable, `max_chunk_chars`, the `$9` cap, the `source_type` taxonomy, "the single path to PostgreSQL". The skim role is the counterexample: its rule is enforced at four layers and has never been violated. That is the pattern to copy everywhere else.
2. **A failure is caught and then reported as success** — five channels exit 0 on hard failure, six broad `except Exception` blocks hide programming errors, the dream cycle reports zero work for runs that did real work.
3. **The same thing is implemented N times and the copies have drifted** — 3 column lists, 2 metrics schemas with different key names, 8 header renderers, 6 truncators, 4 arrivals filters, 5 DB access styles, 17 shell scripts each re-deciding PATH.
4. **Identity by substring** — the crawlee incident was one instance of a family with five surviving members.
5. **Documentation asserts present-tense authority from a stale commit** — two doc layers 233 commits apart, never reconciled, with the authority chain looping back on itself instead of terminating on code.

**Sequencing principle.** Fix what is broken or unsafe first (small edits, largest payoff). Then delete, so there is less code to restructure. Then consolidate duplicates. Then split the large modules. Then reconcile the docs. Then add the invariant tests that stop each class from recurring. Every phase ends green and is independently shippable — no phase depends on a later one.

**Constraints.**
- One branch per phase, merged to local `main`. Nothing is pushed.
- 83 files are uncommitted working-tree edits (all of `docs/`, `README.md`, `scheduling/README.md`, `.kiro/specs/**`, `src/backends/kiro.py`). **Nothing touches them until Phase 0 resolves.** Phase 5 is blocked on that.
- Corpus content never reaches Kiro or Fable 5.1 — including while doing this work.
- Hermetic suite must pass at every phase boundary:
  `AWS_PROFILE= AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= AWS_DEFAULT_REGION=us-east-1 .venv/bin/pytest -q -p no:cacheprovider`
- No schema change without a migration; no migration that names a database literally.

---

## Phase 0 — Four decisions I need from you

These block or materially change later phases. Everything else I can carry alone.

| # | Decision | Why it is yours | My recommendation |
|---|---|---|---|
| 0.1 | **The uncommitted doc layer.** All of `docs/` plus `README.md` is a 2026-09-10 snapshot pinned to `7bf8d41`, sitting uncommitted for 13 days, and it is now wrong on adapters, profiles, roles, plists, migrations, capture channels, the run cap and the remote-lineage prohibition. Commit it as a dated snapshot, or discard it? | It is your writing and I will not overwrite it | **Commit it as-is first**, so the reconciliation in Phase 5 is a reviewable diff against a real baseline rather than an argument with the working tree. Its *epistemics* (evidence dates, "code present ≠ working") are the best thing in the repo and worth keeping; only its facts are stale. |
| 0.2 | **Re-embed the 199 MB.** 4,826 rows were embedded from their first 25 k chars only. Fixing the chunker fixes new captures; the existing rows need a backfill that re-chunks and re-embeds them. | It costs money and touches the live corpus | Do it, in Phase 1, as a resumable batch with a dry-run. Titan v2 is cheap; I will price it exactly before running anything. Without this, the fix only helps future content and 199 MB stays dark. |
| 0.3 | **Retire the `hybrid` profile and the `codex` backend?** `hybrid` can route the corpus to Kiro *and* Fable simultaneously. `CodexInvoker` is 620 lines reachable only from tests, because the only profile that would use it (`codex_local`) does not exist. | Both are deliberate past choices | **Delete `hybrid`; keep `CodexInvoker` but mark it explicitly experimental** and remove it from `DEFAULT_ADAPTERS` so it cannot be selected by accident. Deleting the adapter outright is easy to reverse from git if you want it later. |
| 0.4 | **The 77,413-row knowledge graph** (`entities`, `entity_edges`, `memory_entities`) has zero readers in `src/`. | It may be intended for future work | **Keep the tables, drop the two unused indexes.** If you want it read, that is a feature, not a refactor — say so and I will scope it separately. |

Two smaller open items unrelated to the refactor, still outstanding from earlier: the identity name in `config/skim.toml` is `"Kerr First"`, derived from the git author and never confirmed by you; and Friday's 02:00 dream cycle will crawl for six hours again if the lid is closed.

---

## Phase 1 — Correctness and safety

Small, localised edits. No structure changes. This is where nearly all the value is.

**1a. The chunker and the silent truncation** (S1, §0 of the audit)
- `src/ingest.py chunk_by_section`: hard-split any piece exceeding `max_chunk_chars` on line boundaries. Property test: for header-less, blank-line-free input, `max(len(c)) <= max_chunk_chars * 1.1`. This function currently has **zero** test references.
- `src/embeddings.py generate_embedding`: stop silently slicing. Count truncations, log the `source_url`, and return the count so ingest can report it.
- `scripts/maintenance/rechunk_oversized.py` — resumable, `--dry-run` default, batched, priced before the first real call. Re-chunks and re-embeds the 4,826 rows over 25 k chars (and optionally the 21,352 over 4 k). Blocked on 0.2.
- Verify: the property test; then re-run the live query from §0 and confirm `rows over 25,000` → 0 for newly captured content, and the backfill's own before/after counts.

**1b. Close the two unmetered corpus paths** (S1, §1)
- `src/express.py:481`: resolve through `default_resolver().invoker_for(SKIM_ROLE)` and pass a `SpendTracker` — copy the pattern `scripts/weekly_digest.py:60` already gets right. Narrow the `except Exception` at `:496` so a routing violation cannot be swallowed as a graceful degrade.
- `src/capture/codex.py:189`: give the semantic pass its own Bedrock-pinned role rather than borrowing `thinker`, and pass a `SpendTracker` with a per-run cap. This job runs every 6 hours against the most expensive configured model with no ceiling.
- Make it structurally impossible to repeat: have `Resolver.invoker_for` return an invoker that **cannot be invoked without a tracker** (wrap it), so the next caller cannot forget.

**1c. Enforce the routing rules in code** (S1/S2, §1)
- `resolver._validate_profile`: add `CORPUS_ROLES`, `DENIED_BACKENDS_FOR_CORPUS = {"kiro"}`, `DENIED_MODEL_SUBSTRINGS = {"fable"}`. Fail at **config load**, in every profile, the way the skim rule already does.
- `config/backends.toml:33` and `resolver.py:153`: `default_profile` → `controlled`. Keep `laptop` selectable but never default.
- Invert `tests/test_resolver.py:64`, which currently asserts the `bedrock` Thinker **is** Fable 5.1 — the prohibition is pinned backwards. Replace the two per-profile assertions with one invariant over all profiles.
- Delete `hybrid` (blocked on 0.3). Move `bedrock`'s Thinker off Fable.
- Collapse the cap to one constant. `$9` is the real value; it currently lives only in `dream_cycle_scheduled.sh:33` while three docs and the config say 5 or 6.

**1d. Cost accounting integrity** (S2, §1)
- `SpendTracker.record`: count the call **before** the `cost == 0.0` early return, and track `unpriced_calls` separately.
- Price on the **served** model, not the requested one. Make `model_honored == False` fatal for metered roles.
- Pre-flight estimate from prompt size before a call, not only post-hoc, so one large call cannot exceed the cap before anything notices.

**1e. Retrieval defects** (S1, §3)
- `db.get_relationships`: return the **neighbour** id — alias the `CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END` the query already computes — and surface the joined `m.title`/`m.type` that `mcp_server.py` currently discards. Filter `expired_at IS NULL` by default (97 % of edges are expired), with `include_expired=False` for the dream cycle.
- `migrations/015`: `EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search = 200', current_database())` replacing 009's literal `memory_bank`; name the FTS index explicitly and `ALTER INDEX IF EXISTS … RENAME` to match production; add the missing `memories.parent_id` index; drop the three zero-scan indexes.
- Reconcile `correction_episode` into `validation.MEMORY_TYPES` (10 live rows are currently unreachable through MCP) and decide whether `schema` stays advertised, since `schema_context` can never fire with 0 rows.
- `memory_recent_activity`: route through `validation.clamp_days`/`clamp_limit` using the `lo`/`hi` parameters that already exist and no caller uses. Add `try/except ValidationError` to `memory_brief`. Cap `memory_learn`'s topic count. Stop `_guarded` forwarding Postgres `message_primary` verbatim, and translate `OperationalError`/`PoolError` at the tool boundary so `connection to server at "localhost", port 5432` stops reaching MCP clients.
- `DB_POOL_MIN` → a real minimum, so the pool stops closing every connection past the first.

**1f. Dream-cycle run integrity** (S1, §2)
- Carry real counts through **every** abort path. Reserve `_ZERO_STATS` for aborts before any candidate exists. Capped runs currently poison every tier-1 metric.
- Wrap persistence **per candidate**, inside the loop's `try`, and validate `relationships` entries before indexing them. One malformed key from a model currently discards the whole run.
- Add a `pg_try_advisory_lock` around the run. Nothing today prevents two concurrent runs, i.e. 2 × $9. Also serialise the 02:00 `backup` + `dream-cycle` collision.
- Fix `MAX_CANDIDATES_PER_SLICE = 3` vs the Thinker prompt's "at most 2" vs the budget formula's literal `2 *` — pick one and have the formula reference the constant.
- Move the grounding audit off the `skeptic` seat, which currently audits a candidate, drives its revision, and then votes on its own correction while the prompt tells it "four independent evaluators".

**1g. The substring-identity family** (S1, §4)
- `outlook.py:106,152`: refuse to build a `source_url` from an empty timestamp; include a body digest in the hash.
- `chat_extract.py:94`: stop truncating the CLI conversation id to 12 chars; assert `len(set(source_urls)) == len(files)` before ingesting.
- Unify the two readers of `index.md` — `crawlee.build_source_map` is rigorous, `scrape_bookmarks.load_indexed_urls` still uses the old loose whole-file regex.
- Anchor `SKIP_PATTERNS`, escape the dots, and stop `canonical_url` dropping every parameter starting with `ref`.
- `slack._dm_inactive_for`: compare parsed datetimes, not strings.

**1h. Test isolation** (S1, §9)
- Make the `DB_CONFIG` override session-**autouse**, delete the four shadowing local `test_db` fixtures, and rename the conftest fixture so shadowing is impossible. Today, running any of those four files alone sends `UPDATE memories` to the live corpus.
- `_apply_migrations`: statement-by-statement, record `schema_migrations`, stop half-applying non-idempotent files.

**Exit criteria:** suite green; `rows over 25,000` at 0 for new captures; a test that fails if any profile routes a corpus role to Kiro or Fable; a test that fails if `chunk_by_section` exceeds its limit; `memory_graph` returning real neighbours on a live inbound edge.

---

## Phase 2 — Make failure visible

**2a. One exit-code contract for every channel.** Slack and Outlook have `0/3/4/1`; five channels have none and cannot fail. Give `browser`, `youtube`, `ingest_staged.sh`, `scrape_bookmarks` real exit codes and delete `ingest_staged.sh:70`'s unconditional `exit 0`. Stop `youtube`'s `--dry-run` incrementing `processed`.

**2b. `job_wrapper.sh` owns what it should.** Move the canonical PATH into it — including the `/usr/local/bin` that was fixed in two scripts while the wrapper named as root cause was left alone — plus `postgresql@17`, `AWS_PROFILE`, repo `cd`, venv resolution, log path and rotation, notification, exit passthrough, and two things nothing owns today: **a lock** and **a per-job timeout**. Bring `qd-sync`, `codex-capture` and `capture-api` under it. Per-job scripts keep only the command, their exit taxonomy, and job-specific env. Add a comment explaining why the wrapper deliberately omits `set -e`, so nobody "fixes" it.

**2c. A status surface that survives a headless host.** `osascript` is the only alerting channel and it cannot work with nobody logged in — and both monitors append `|| true` so a failed notification is swallowed. Have each job write one machine-readable status line (`verify_liveness.sh:102-113` already computes exactly the right structure and throws it away), and add a sweep that reports any job whose last exit was non-zero. `weekly-digest` has been red since Sep 21 and nothing says so.

**2d. Narrow the broad excepts.** Catch `(psycopg2.Error, OSError, ValueError, KeyError)` explicitly, or re-raise on `AttributeError`/`TypeError`, in the six stages that currently swallow programming errors. Priority: `browser._import_bookmark_filters`, whose fallback reclassifies internal `*.a2z.com`/`*.aws.dev` URLs as public and ships them to Crawlee. Give `digest.py:59` a log line. Distinguish call failures from storage failures in `SkimResult` and trip a threshold instead of exiting 0 on N quiet skips.

**2e. Fix the entrypoint/plist mismatches.** Substitute or delete `codex-canary` (its plist still contains `/path/to/second-brain/`). Replace the two installed plists that are copies rather than symlinks. Make `weekly_digest.sh`'s documented exit codes real or delete the claim, and export the `DIGEST_TIMEOUT` that caused the Sep 21 outage.

**Exit criteria:** every scheduled job can fail; every failure reaches a channel that works headless; a deliberately broken channel is detected within one cycle.

---

## Phase 3 — Delete

Do this before restructuring, so there is less to restructure. Each item in the audit's §7 has a verified absence of live callers. Rough scale: **~1,300 lines of `src/` and `scripts/`, ~62 MB of disk, and 27 doc files.**

Order by risk: dead functions and unreachable branches first (`db.get_schema_with_constituents` and its dead import, `find_schemas_for_memory`, the `dream_cycle_db` shims, `allocation`'s three test-only public wrappers, the unused `SkimResult` surface, `type_distribution`); then unreachable adapter seams (`use_invoke_model`, `system_prompt_delivery="prepend"`, the Gemma path, the Claude-4.x-on-Bedrock branch and its price rows); then the `DEFERRED` columns and `--prompt-variant`, both provably impossible; then the superseded scripts (`ingest_chats.py`, `batch_ingest_parallel.py`, `run_golden_capture.py`); then `codex_capture_canary.sh` + its plist; then `scripts/backups/` (62 MB) and `scripts/logs/` (dead since June); then the Docker vestiges.

Also: move `cleanup_duplicate_sources.py` and the two non-executable backfill scripts out of the live-job surface into `scripts/maintenance/`; add `.dream/` to `.gitignore`; anchor `.gitignore`'s `backups/`/`logs/` **after** deleting the two stale directories, since the unanchored patterns are exactly why 62 MB stayed invisible.

`CodexInvoker`'s fate is decision 0.3.

---

## Phase 4 — One copy of each thing

This is the streamlining proper. Each item collapses a row of the audit's §5 table.

**4a. Data access.** `src/db/pool.py` (the only module importing `psycopg2.pool`), `src/db/columns.py` (one `MEMORY_SCALAR_COLUMNS`, one `memory_summary(row)`, one `_iso` — replacing three column lists, six row projections and three date idioms), `src/db/memories.py`, `src/db/relationships.py`. `search.py` keeps the algorithm and takes candidate rows from the repository. Adopt `search.py`'s `psycopg2.sql.Identifier` composition everywhere, including `update_memory`'s interpolated `set_clause`. Retire the five access styles down to one, starting with `express.py`'s cursor injection and its second connection at `:385`.

**4b. Capture framework.** The nine channels already converged on the same five steps; make it explicit. `src/capture/framework.py` with `run_channel()`; `render.py` with one canonical **underscore-only** header vocabulary (retiring `"thread replies"`, `"video id"`, `"raw visits"`) and one `html_to_text`; `window.py` with DST-correct `completed_local_days()` and `rolling_utc_window()` via real zoneinfo, replacing the fixed-offset snapshot that misfiles an hour twice a year; `filters.py` as the single URL-filter vocabulary. Then `ingest.ingest_document(conn, …)` writing parent **and** chunks in one transaction, so a mid-document failure stops being permanent and silently skipped.

**Adopt Codex's patterns, not the reverse.** `src/capture/codex.py` is the only transactionally correct channel, the only one that detects a partial prior capture, and the only one that enforces timezone awareness. Move Slack and Outlook onto the framework first (closest), then Browser and YouTube (where the exit-code gap closes), and leave Codex until the framework has its transaction and progress-cursor semantics.

Deleting the two-phase staging hop removes four scripts and fixes the `conv_id[:12]` collision and a dead dedup branch at the same time.

**4c. Backend base.** Hoist into `base.py`: one `_init_metrics` with **one key vocabulary** (`latency_s` vs `duration_s` currently gives `logs/llm_metrics/*.jsonl` two schemas), one `_parse`, one `_warn_missing_usage`, one `normalize_usage()` so `cost.py` stops guessing between camelCase and snake_case, and one `price_and_record()` so no call is priced twice. Collapse the three `METRICS_DIR` re-exports that exist only to keep monkeypatch targets alive, then retire `src/agent_invoker.py` once `express.py` no longer needs it.

**4d. Shared test doubles.** Move `FakeStore`/`FakeInvoker`/`FakeSession`/the Bedrock client fakes into `conftest.py`. Three collaborators, ~11 hand-rolled copies.

**4e. Vocabulary.** `validation.py` becomes the only declaration; `classify.py` imports `MEMORY_TYPES` and derives its subset. Add a `SOURCE_TYPES` frozenset — `source_type` is validated in zero of the three tools that accept it. Then push enforcement **down** into a CHECK constraint, because every non-MCP writer bypasses `validation.py` today. Fix `capture_api`'s three off-taxonomy values, one of which collides with the real Slack channel.

---

## Phase 5 — Split the large modules

Only after Phases 3 and 4, when there is less code and the duplicates are gone.

`dream_cycle/orchestrator.py` (1,201 lines, 7 responsibilities) → `budget.py`, `stages/skim_stage.py`, `stages/grounding.py`, `agents/{explorer,thinker,evaluator}.py` over a small `AgentRunner`, `run_result.py`, and the verdict→DB mapping into `dream_cycle_db`. Extracting `stages/grounding.py` also turns `panel_replay`'s four private-attribute reach-ins into a supported call.

`dream_cycle_db.py` (994 lines, 5 concerns) → runs/candidates, feedback, briefing (with `should_run_briefing` moved out of the DB layer, since it is a policy decision), metrics + `golden_queries` (`extract_golden_queries` performs no DB access at all), attention. A `Protocol`-typed DB dependency injected into the orchestrator makes the two pass-through shims — which exist purely as a test seam — disappear.

Fix the layering while in there: the orchestrator talks to `src.skim`, not to `skim.store`/`skim.cluster` directly; `digest.py` becomes a pure `render_digest(...) -> str` plus a thin writer; the lazy in-function imports that avoid the `ledger`/`allocation` cycle become real layering.

---

## Phase 6 — Documentation reconciliation

Blocked on decision 0.1.

**6a. Promote the five ownerless contracts** into canonical docs: panel acceptance (currently only a PLAN), the `controlled` profile and the Kiro-`--model`/Fable-PII hazard (currently only `backends.toml:104-113`), the `$9` cap (currently only a shell script), the `quick_desktop_doc` retirement and the four new capture channels (currently only `PLAN-capture-repair.md`, which **nothing links to**). The PII hazard is the highest-harm doc defect in the repo: the file README tells every reader to open first still presents all-Kiro as the normal path with no warning.

**6b. Re-pin layer A to HEAD.** `START-HERE.md`: adapters 3→4, profiles 2→5, roles 6→7, plists 12→18, migrations 005→014; delete the "Current local observation" block; add `INTERFACES.md` to the component map. `ARCHITECTURE.md`: add Step 3.5 and the grounding-audit/revision stage to both the mermaid flow and the walkthrough. `OPERATIONS.md`: regenerate the schedule table from the 18 plists, fix the dream-cycle cadence, record the `$9` cap, fix the inverted `mcp_serve.sh` interpreter claim. `CAPTURE-COMPONENTS.md`: delete the "no native Codex/Outlook collector" denial. `README.md`: drop "do not merge", since `5386c2b` ported Codex capture from `origin/main`. `VALIDATION.md`: close the token finding that `MODEL-BACKENDS.md` records as fixed.

**6c. Shrink the surface.** Delete the 27 redirect stubs — the archive already holds the originals at identical relative paths, and 33 live docs have zero inbound links. Archive the four completed `PLAN-*.md` and `RESEARCH-skim-model.md` after promoting their contracts. ~50 files → ~13 with content.

**6d. Terminate the authority chain on code.** Remove the circular "see X for authority" loops, and either add the "local operational inventory" to the repo or delete every deferral to it — it is referenced twice as the source of all runtime truth and does not exist.

**6e. One name per concept.** *panel* / *consensus* / *seat*; retire BFT/Byzantine/quorum from live docs. Define **channel** ≠ **arrival** ≠ **slice** in `INTERFACES.md` and use them strictly — they are three different things currently used interchangeably. Adopt layer B's dated-evidence convention, which is what `VALIDATION.md:79` already prescribes.

**6f.** Fix `test_rerank.py:214`'s docstring, which omits three weights the same file asserts 20 lines below.

---

## Phase 7 — Guardrails

One test per defect class, so none of this comes back:

- No profile routes a corpus role to Kiro or Fable (invariant over all profiles, not per-profile).
- No invoker can be obtained or invoked without a `SpendTracker`.
- `chunk_by_section` never exceeds its limit, for any input shape.
- `generate_embedding` never silently truncates.
- Every capture channel maps failure to a non-zero exit.
- Every `source_url` builder rejects empty components (the crawlee-bug family).
- Every MCP tool argument passes through `validation.py`.
- No migration names a database literally.
- `docs`-vs-code check for the counted facts (adapters, profiles, roles, plists, migration high-water, cadence, cap) — these drifted once and will drift again.

---

## Effort and payoff

| Phase | Shape of work | Risk | Payoff |
|---|---|---|---|
| 0 | Four decisions | — | Unblocks 1 and 6 |
| 1 | ~20 localised edits, 1 migration, 1 backfill | Low, except the backfill (dry-run first) | **199 MB searchable; no unmetered corpus spend; runs stop lying about their own results** |
| 2 | Shell consolidation + status surface | Low | Failures become visible, including on the Mac mini |
| 3 | Deletion | Low (git) | ~1,300 LOC, 62 MB, 27 docs |
| 4 | Consolidation | Medium — touches every channel | One copy of each thing; drift becomes impossible rather than unlikely |
| 5 | Module splits | Medium | 1,201- and 994-line modules become reviewable |
| 6 | Doc reconciliation | Low | An entry point that is true, and reaches the last 233 commits |
| 7 | Invariant tests | Low | The five defect classes stop recurring |

Phases 1 and 2 are where the value is concentrated and they are mostly small, independent edits — I would ship them as several small merges rather than one. Phase 4 is the largest single piece of work and the one worth doing carefully, channel by channel, with the suite green between each.

**Not in scope:** new features, a retrieval-quality push, reading the knowledge graph, reconciling the 20 remote-only `origin/main` commits, and the Astra `effort=medium` cost lever. Each is a separate decision, not a refactor.
