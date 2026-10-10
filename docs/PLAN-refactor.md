# Refactor plan: streamlining Second Brain (v4)

## Status (2026-10-09, R7 deployed)

The plan below was authorized on 2026-09-26 and implemented on the refactor branch `codex/refactor-safety-routing` @ `74ba02d` (its ledgers: [docs/archive/refactor-2026-09-26/](archive/refactor-2026-09-26/README.md)). That branch was not deployed as one step. Its content was re-cut, re-reviewed and rehearsed as the staged release line in [RELEASES.md](RELEASES.md), and deployed on 2026-10-09 in the order R3, R4, R5, R7, with the R6 corpus repair applied the same day (deployment records `R3-DEPLOYMENT.md` … `R7-DEPLOYMENT.md` in the operator's private review folder):

| Release | Plan phases carried | Status |
|---|---|---|
| R1 `8dd3bb7` | 1a routing and accounting (items 1.1–1.6), 1.12 expiry rule, 1.19 isolation, 1.20 plaintext sync stop, migration runner port (1.13) | deployed 2026-10-05 |
| R1b | 1.20 dependency upgrades (`requirements.lock`) | deployed 2026-10-05 |
| R3 `e1cff5c` | 1b ingestion integrity, 1c relationships (016 backfill), 1d data layer and migration 016, 1e run integrity, 1.15 MCP, 4a/4e, 5 decomposition, 7 guardrails for those classes, 3 (dead helpers) | deployed 2026-10-09 16:58 (migration 016) |
| R4 `d063543` | 4b capture framework, 3 (staging hop, migrators, canary script), D5 native Kiro IDE capture | deployed 2026-10-09 18:08 |
| R5 `0302f0f` | 2 truthful operation, 1.20 encrypted bundles and verified restore, 3 (`codex-canary`) | deployed 2026-10-09 18:52 |
| R6 | D2 paid corpus repair (items 1.9–1.11 **application**) | applied 2026-10-09 (metadata 2,832 ops, dedup 70 ops, rechunk 8,240 ops / 96,331 Titan calls; re-plan 0/0/0; HNSW rebuilt, recall@10 0.99) |
| R7 | 1.14 runtime role separation (017, F-138), D4 graph writer and table retirement (018), 6 documentation and specs, 7 documentation guardrails | deployed 2026-10-09 19:30 (017, 018, app role demoted) |

Phase 0 is done (2026-09-24). Phase 4d's 20,000-test-line target was dropped as a goal; integrity and isolation regressions were kept (the isolated suite stood at 2,228 tests at the R7 database head). Phases 4c (shared backend base) and the routing-lane items below are partially carried: `src/mcp_readonly.py` exists but the Explorer still attaches the full MCP server; there is no dedicated `grounding_auditor` role (the audit runs through the skeptic seat's backend); the `latency_s` metrics rename was not done.

**What remains after R7 and R6** (none of it is closed by a passing suite):

1. **Human evaluation trial** — re-prepare a frozen record/provider/cost manifest against the deployed release and obtain human grounding, novelty, uptake and outcome judgments ([VALIDATION.md](VALIDATION.md)); the refactor branch's trial runner was not ported.
2. **Static IAM key replacement** — provision and verify an unattended credential provider for `second-brain-jobs`, then revoke the identified old key with a receipt; `~/.aws` residue is 0600 but still present. *2026-10-09: the static key was **rotated** (new key verified by STS, a Titan probe and the credentials job; old key deactivated, re-verified, deleted; one key remains). Replacing the static key with an unattended non-static provider is still open.*
3. **OneDrive recovery-key verification** — confirm the key copy is retrievable from a second device or session ([DISASTER-RECOVERY.md](DISASTER-RECOVERY.md)).
4. ~~**Historical Drive plaintext purge**~~ — done 2026-10-09 after the listing (`kb-sources/` 1,372 files, `config/` 433 files) and with three verified bundles on Drive; only `graph-export/` and encrypted files remain.
5. ~~**Graph archive off-site**~~ — done 2026-10-09 during the R7 window (`gdrive:memory-bank-backups/graph-export/`).
6. **Routing lane** — Explorer on `src.mcp_readonly` with exact tool grants, a dedicated `grounding_auditor` role, served-model metadata check for Claude Code, `latency_s` rename (F-145, F-061 residue).
7. **Documented-open capture findings** — the remaining open rows of the [findings register](audit-2026-09-24/00-FINDINGS-REGISTER.md). *2026-10-10:* F-086 native Codex identity now wins over a legacy mirror (no stored id changes) and F-094 private/incognito QD sessions are excluded (0 flagged today) — both on `main` (`649acfa`, `85da160`). F-090 (branch `fix/f090-guarded`, not yet merged or deployed): the facts stage retires `qd://` parents that left the selection, behind a mass-retire guard — read-only dry run: 669 candidates (580 below threshold, 87 deleted upstream, 2 relabelled); `qd://memory/` 652 > cap 95 is refused, `qd://entity/` 17 ≤ 25 would retire; the bulk retirement needs an operator `--allow-mass-retire` run after review. Still open as decisions: F-086 content-hash second key for legacy-only re-emission; F-090 chats (absence from the active profile is not deletion) and the retired `quick_desktop_doc` rows; F-094 `archived`/non-`main` sessions and chats flagged private after capture.
8. **Operational residue** — stale databases `memory_bank_test` / `second_brain_codex_test` on the installed cluster (optional `DROP DATABASE` after R7), the one-off cleanup of test-polluted metrics rows, `distill` re-enablement and e-mail delivery (product decisions).

**Evidence:** [AUDIT-2026-09-24.md](AUDIT-2026-09-24.md) and the [findings register](audit-2026-09-24/00-FINDINGS-REGISTER.md). Three independent code reviews on September 26 supported the correctness-first direction with the amendments below; each release had its own independent review afterwards. Historical corpus counts in this plan are audit observations, not current measurements. Original item numbers are stable for audit references; the text below is the authorized plan as written on 2026-09-26, kept for those references — the table above, not the per-item prose, says what was carried where.

---

**Constraints that hold throughout**
- Use isolated branches. Nothing is pushed. The installed checkout is a job target: merging into its `main` is deployment even without a restart and requires a separately qualified release.
- The 2026-09-10 working-tree edits (documentation stubs, specs, `src/backends/kiro.py`) were preserved as `preserve/2026-09-10` before R1 was deployed and archived under `docs/archive/2026-09-10-installed/` in R7.
- Corpus content never reaches Kiro or Fable 5.1 — including while doing this work. Use `SECOND_BRAIN_PROFILE=controlled` in every shell.
- Run tests only through the disposable database/offline runner documented in the execution record. Clearing AWS environment variables alone is not isolation. The old 1,630-test result is a historical baseline, not permission to use the installed database.
- No schema change without a migration; no migration that names a database literally; `migrate.sh` is ported (item 1.13) before any new migration runs.
- Merge duplicates before creating unique indexes; fix expiry before filtering expired edges; admit live vocabulary values before adding CHECK constraints.

---

## Phase 0 — Tonight, and five decisions

### Stop the bleeding — DONE 2026-09-24
- **0.1 ✅** `com.second-brain.distill` booted out and `launchctl disable`d (persists across login). Plist symlink left in place. Reverse with `launchctl enable gui/$UID/com.second-brain.distill && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.second-brain.distill.plist` — only after item 1.2. (F-003)
- **0.2 ✅** `scripts/jobs/dream_cycle_scheduled.sh:44` express_push call commented out with a pointer to this plan. Committed in `54b191d`. (F-001)

### Decisions (yours)

| # | Decision | Recommendation |
|---|---|---|
| **D1** | Preserve the uncommitted document, specification and backend work. | **Preserve in place.** An external hash manifest records the 128 modified/untracked files observed September 26. Implement in a worktree from committed HEAD; reconcile these edits as a separate review. Do not discard or stage them as part of this refactor. |
| **D2** | Deduplication, re-chunking and paid re-embedding of the live corpus. | Prepare a resumable dry-run manifest after fixing ingestion/chunk bounds. Distinguish source identity from identical content, preserve lineage and references, verify recovery, then price the actual remaining corpus before application. Historical audit counts are not an execution manifest. |
| **D3** | Codex capture inference and embedding locality. A locally running CLI does not establish local model inference or zero cost. | Retain the existing controlled Bedrock route, add a dedicated semantic role and metering. Any provider change must document the actual endpoint, data boundary, model identity and usage. Do not delete the Codex adapter solely on the earlier locality claim. |
| **D4** | Retire the unused graph writer or build a discovery reader. | Prepare retirement separately. `scripts/jobs/backup.sh` reads all three graph tables, so removing them requires coordinated backup/recovery changes and preserved history. Git cannot reverse deleted database data. |
| **D5** | Restore Kiro IDE capture from the newer session format or retire it. | Qualify the format with synthetic fixtures and verify source identity, freshness, deduplication and completeness. Current activity and the claim that only a parser is missing are not yet established. |

Closed on 2026-10-09, both unrelated to the refactor: the identity in `config/skim.toml` (`"Kerr First"`) was confirmed correct by the user, and the dream cycle moved from Mon/Wed/Fri 02:00 to Mon/Wed/Fri 12:30 because closed-lid dark-wake runs lost the network ([scheduling/README.md](../scheduling/README.md)).

---

## Phase 1 — Correctness and safety — carried by R1/R3 (1.14 by R7; 1.9–1.11 application by R6, applied 2026-10-09)

Small, localised edits; nearly all the value. Ship as several merges. **Order within the phase matters and is given.**

**1a. Close the routing rule: after isolated tests and migration runner**
1. Route the editor through a controlled `editor` role and the shared metered invocation helper. Gate normal email sends in `express_push.main()` before the model call; CLI/MCP editor use must not depend on email configuration. Preserve an explicit deterministic mode and narrow failures. The previous weekly-digest pattern missed paid parse failures and unchecked model overrides, so it is not the accounting reference. (F-001)
2. `scripts/distill_sessions.py:150`, `redistill_capped.py:126` — a Bedrock-pinned `distill` role in every profile, resolved through the resolver, metered; detect model refusals and mark the session `do_not_send`; non-zero exit on failure (today: exit 0 at 100 % failure). Qualify the code first; job resumption is a separate operational step. A dry run must state whether it can invoke a model. (F-003)
3. `memory_brief(use_llm=True)` — route or remove the LLM branch; add the missing `try/except ValidationError`. (F-002)
4. Codex semantic pass: dedicated Bedrock role, one spend threshold across the run, fixed bounded batches of new turns, bounded previous-segment context, and separate contract versus infrastructure failures. New arrivals must not revive a failed batch. Cache validated output before embedding/storage retries. Retain oversized turns verbatim and report them without silent truncation. (F-005, F-084)
5. `resolver._validate_profile` — `CORPUS_ROLES`, `DENIED_BACKENDS_FOR_CORPUS={"kiro"}`, `DENIED_MODEL_SUBSTRINGS={"fable"}`; fail at config load like the skim rule does. `default_profile` → `controlled` in `backends.toml:33` and `resolver.py:153`. Delete `hybrid`; move `bedrock`'s Thinker off Fable. Flip `tests/test_resolver.py:64,69-79,97-107` and the other wrong-way pins listed in F-127. `_note_model_honored` raises for corpus roles. Separately inventory before changing the orphaned `~/.kiro/agents/dream_cycle_*.json` (`tools:["*"]`). (F-006, F-008, F-127, F-149)
6. Repo-wide test: no module outside `src/backends/` constructs `KiroInvoker` or `AgentInvoker`; no committed profile routes a corpus role to Kiro or a fable model. Retire `src/agent_invoker.py` once 1–3 land.

**1b. Corpus integrity: repair ingestion before maintenance; dedup before paid re-embedding**
7. Fix `chunk_by_section` to hard-split oversize pieces on line boundaries; property test (`max(len(c)) <= max_chunk_chars * 1.1` for header-less, blank-line-free input — the function has zero tests today). Make `generate_embedding` count and report truncation instead of silently slicing. Drop or persist the whole-body embedding `_discover_relationships` computes and throws away. (F-020, F-021)
8. `ide_chat.py` — strip the repeated context block before chunking (the source of 62 k identical chunks). `ingest_batch` — `already.add(source_url)` after each success; refuse `source_url IS NULL`. (F-022, F-023)
9. `scripts/maintenance/dedup_corpus.py`: dry-run default and resumable. Deduplicate only proven duplicate source identities; identical content across projects may have distinct provenance. Enumerate and remap foreign keys, candidate JSON/source-memory arrays, slice ledgers, skim evidence and lineage before deleting anything. Do **not** add global parent `UNIQUE(source_type, source_url)`: Codex legitimately stores multiple distinct `distilled_agent_task` memories with the same URL and no parent. Scope uniqueness to actual captured-source identities, with regressions for multiple task memories per URL. Replace chunks transactionally after replacement data is ready. (F-023)
10. `scripts/maintenance/rechunk_and_embed.py` — re-chunk/re-embed the oversize and tail rows, repair the 18 half-written parents, embed the 1,087 unembedded leaves. Priced on post-dedup counts; blocked on D2. Make `ingest_content` write parent+chunks in one transaction so this cannot recur. (F-026, F-027)
11. `cli_chat.py:82-85` — stop using the session filename as `conversation_id`/project; null out the 2,675 UUID projects. (F-028)

**Retrieval repair prerequisite (F-048, F-130).** Full-text search must reach unembedded parents and sources; the vector-only `embedding IS NOT NULL` predicate must not filter the full-text leg. Add offline retrieval reachability and unique-result regressions now, then a broader quality fixture before restructuring retrieval.

**1c. Relationships — expiry before filter**
12. `expire_stale_relationships` — require `created_at < now() - threshold` *and* both endpoints unaccessed; never expire `derived_from`/`superseded_by` (port origin `012`'s CHECK). Backfill: un-expire edges with `expired_at - created_at < threshold`. **Then** `get_relationships` filters `expired_at IS NULL` by default, returns the neighbour id the JOIN already computes plus the joined title/type, and canonicalises symmetric types so `related_to` is not returned twice. (F-025, F-045, F-046)

**1d. Data layer and migrations: runner immediately after isolation, before new migrations**
13. Port `migrate.sh` from origin `91d2925` (`ON_ERROR_STOP`, advisory lock, SQL + ledger insert in one transaction) and origin's 009 (`EXECUTE format(... current_database())`). Pick a local migration number that does not collide with origin's 012–015 fork. (F-103, F-104)
14. Migration (one file): 009 fix; name the FTS index and rename to match production; `memories.parent_id` index; drop the three zero-scan indexes; `dream_cycle_runs.cost_usd`; admit `correction_episode` to `MEMORY_TYPES` and `source` to `MEM_CLASS_BOOST` **before** any CHECK constraint; `SimpleConnectionPool → ThreadedConnectionPool` (`db.py:14,35` — one word, and the design.md rationale "single-threaded" is false). `ALTER ROLE memory_bank NOSUPERUSER` with a separate owner role — *after* 13. (F-105…F-110, F-138)
15. MCP surface: `memory_recent_activity` through `validation.clamp_*` (the `lo`/`hi` params already exist, unused); `memory_learn` topic cap; `_guarded` stops forwarding Postgres `message_primary`; translate `OperationalError`/`PoolError` at the tool boundary; a `SOURCE_TYPES` vocabulary built from the 27 live values. (F-050…F-057)

**1e. Dream-cycle run integrity**
16. Validate `verdict ∈ {ACCEPT, REJECT}` in `EvaluatorVerdict.__post_init__` (make the `ValueError` retry arm reachable); send seats a candidate projection **without** `revision`; move the grounding audit off a voting seat. (F-061, F-062)
17. Carry real counts through every abort path (`_ZERO_STATS` only before any candidate exists); `except SpendCapExceeded: raise` before the Thinker loop's broad except at `:442`; complete the run with `aborted` in the `OperationalError` arm; one-off UPDATE of runs `3a1263a6`, `bfe245b1` from their candidate rows; flip `test_budget_order.py:228-240`, `test_dream_cycle.py:2707-2711`. Write `cost_usd` from `SpendTracker` at `complete_run`; retain conservative estimates until response failures are billed. An observed average is not a maximum per candidate. Meter response-bearing failures before parsing, validate finite/nonnegative usage, and fail closed when usage is unknown. Post-call accounting is a stop threshold, not a hard spending ceiling. (F-063, F-069)
18. Authorize model-proposed UPDATE/SUPERSEDE targets deterministically, independently of panel votes. Captured source evidence and governing instructions are not writable merely because a model selects them. Validate source/relationship IDs and vocabulary before writes; persist the memory mutation, relationships and candidate ledger in one transaction, retaining prior successful candidates on failure. Add an explicitly read-only Explorer surface. Candidate caps, run locks/timeouts and truthful digest/ledger completion follow. (F-064, F-065, F-066, F-067, **F-145, F-146**)

**1f. Test isolation: execute first, before any suite run**
19. Bootstrap isolation **before importing application modules or collecting tests**. Use a fresh socket-only PostgreSQL cluster with a verified disposable manifest and synthetic corpus; reject production connections. Remove shadow fixtures. Apply migrations through the same transactional ledger runner. Block provider/network/CLI access, including shell descendants; isolate HOME, source directories and metrics before import. Injectable shell probes must use test stubs. Missing integration prerequisites fail explicitly. A `_test` suffix or late session fixture alone is insufficient. (F-121…F-125)

**1g. Security**
20. Encrypt or stop the `kb-sources`/`docs` rclone sync; second custody copy of `.backup-key`; `chmod 600` the `~/.aws/*.backup` files and remove the static key; upgrade `mcp` to ≥ 1.28.1 and `yt-dlp`; scrub the real session ids/home path/email from `tests/fixtures/codex/real_*`. (F-135, F-136, F-139, F-140, F-145)

**Exit criteria:** isolated suite green; routing and failure-accounting regressions pass; chunk/source integrity holds; deterministic mutation authorization and candidate atomicity are demonstrated; FTS reaches unembedded sources; relationship lifecycle is correct. Operational exit criteria (corpus rewrites, indexes, role changes, job resumption, backups and live spend evidence) require their own measured, recoverable release. Passing component tests is not evidence of useful discoveries.

---

## Product evidence gate after the minimal repairs

Run a bounded trial on a current decision, comparing ordinary retrieval with cross-project discovery. Preserve source links and evaluate grounded novelty, specific cross-project reuse, timeliness, user uptake, outcome and cost. Keep the chosen three-of-four/cross-family panel policy; do not add another reasoning architecture before this evidence. Model acceptance rate and passing tests support reliability, not demonstrated human benefit. Prepare a concrete record/model/cost manifest before any new private-data transfer.

## Phase 2 — Make failure visible — carried by R5

1. Exit codes for browser, youtube, crawlee_ingest, scrape_bookmarks, distill; `ingest_staged.sh` — capture the Python exit code at `:35` and delete the unconditional `exit 0` at `:70`; `youtube` dry-run stops counting `processed`. (F-031…F-034)
2. `job_wrapper.sh` owns PATH (with `/usr/local/bin` and `postgresql@17` — the fix applied to two scripts, not the wrapper that was named as root cause), `AWS_PROFILE`, repo `cd`, venv, `caffeinate`, log path + rotation, notification, exit passthrough, **a lock and a per-job timeout**. Bring `qd-sync`, `codex-capture`, `capture-api` under it; per-job scripts keep only the command and their exit taxonomy. Comment why the wrapper omits `set -e`. (F-037…F-040)
3. Extend `verify_liveness.sh` first (it exists and works): watch `distilled_chat`, `weekly_digest`, `dream_cycle_runs.completed_at`, and add a "channel that ever produced output has produced zero for N days" predicate — the rule that would have caught F-083 four months ago. Then a machine-readable status line per job and a sweep for "last exit non-zero for N days". Notifications alone cannot work on a headless host. (F-036, F-041, F-042)
4. Narrow the six broad excepts (catch `psycopg2.Error, OSError, ValueError, KeyError`; re-raise programming errors); `SkimResult` distinguishes call failures from store failures and trips a threshold; `qd_sync.sh` stops swallowing tracebacks. (F-043, F-044, F-093)
5. Delete `codex-canary` (plist has `/path/to/` placeholders; script is the unfixed Ollama-gated ancestor) with its test; symlink the two installed plists that are copies; export `DIGEST_TIMEOUT`; empty digest summary exits non-zero. (F-035, F-166)

**Exit criteria:** every scheduled job can fail; every failure reaches a channel that works headless; a deliberately broken channel is detected within one cycle.

---

## Phase 3 — Delete — carried by R3/R4/R5/R7 (see Status)

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

## Phase 4 — One copy of each thing — carried by R3/R4 (4d target dropped)

**4a. Data access.** `src/db/pool.py` (the only `psycopg2.pool` import; `ThreadedConnectionPool`), `src/db/columns.py` (one `MEMORY_SCALAR_COLUMNS`, one `memory_summary(row)`, one `_iso` — replacing three column lists, six projections, three date idioms), `src/db/memories.py`, `src/db/relationships.py`. `search.py` takes candidate rows from the repository; adopt its `psycopg2.sql.Identifier` pattern everywhere including `update_memory`. Retire the five access styles to one, starting with `express.py`'s cursor injection and its second connection at `:385`. (F-112…F-118)

**4b. Capture framework.** The nine channels already implement the same five steps: preflight → window → render `(source_url, markdown)` → ingest → exit code. `src/capture/framework.py` with `run_channel()`; `render.py` with one underscore-only header vocabulary (retiring `"thread replies"`, `"video id"`) and one `html_to_text`; `window.py` with DST-correct zoneinfo windows; `filters.py` as the single URL-filter vocabulary (unifying the two readers of `index.md`). Slack is the reference (all five steps); Outlook next; then Browser and YouTube (where the exit-code gap closes); then the QD scripts. **From Codex take only the transaction / `FOR UPDATE` / progress-cursor shape** — not its retry or semantic-pass design. Deleting the two-phase staging hop removes four scripts. Decompose `fetch_task` (CC 79) per track 03 §7. (F-085…F-097)

**4c. Backend base.** Hoist into `base.py`: one `_init_metrics` with one key vocabulary (`latency_s` vs `duration_s` gives the metrics log two schemas), one `_parse`, one `_warn_missing_usage`, one `normalize_usage()`, one `price_and_record()`. Collapse the three `METRICS_DIR` re-exports. (F-013…F-018)

**4d. Tests.** Promote `FakeInvoker` (`test_skim_orchestrator.py:43`), one `mock_conn()`, and a shared strategies module into `conftest.py`. Delete ≈640 lines (the signature registry, the self-comparing SQL test, the tautologies listed in track 12b §8); replace `test_dream_cycle_db.py`'s 42 mock-driven SQL-substring tests with ≈8 real-DB round-trips (pattern in `test_ledger.py:95`); fix the vacuous `test_no_deferred_verdicts_and_zero_deferred_count`; make `test_rerank.py`'s oracle draw `encoding_context` and `status` so `context_overlap`/`superseded_penalty` are actually pinned. Target: suite ≤ 20 k lines with the F-128 gaps covered. (F-126, F-131…F-134)

**4e. Vocabulary.** `validation.py` the only declaration; `classify.py` derives from it; `capture_api`'s three off-taxonomy `source_type` values fixed. Then a CHECK constraint — *after* 1.14 admitted the live values. (F-108, F-119)

---

## Phase 5 — Split the large modules — carried by R3

After 3 and 4. `dream_cycle/orchestrator.py` (1,201 lines, 7 responsibilities) → `budget.py`, `stages/skim_stage.py`, `stages/grounding.py`, `agents/{explorer,thinker,evaluator}.py` over a small `AgentRunner`, `run_result.py`, verdict→DB mapping into the DB layer. Extracting `grounding` turns `panel_replay`'s four private reach-ins into a supported call. `dream_cycle_db.py` (994 lines, 5 concerns) → runs, feedback, briefing (policy out of the DB layer), metrics + `golden_queries` (no DB access at all), attention; a `Protocol`-typed DB dependency removes the two test-seam shims. `hybrid_search` → the six pure functions in track 09a §6, with a JSON-fixture seam. `digest.py` → pure `render_digest()` + thin writer. Orchestrator talks to `src.skim`, not `skim.store`/`skim.cluster`. (F-079…F-082, F-047)

---

## Phase 6 — Documentation reconciliation (D1 preserved) — DONE in R7

1. Promote the six ownerless contracts into canonical docs: panel acceptance; the `controlled` profile and the Kiro-`--model`/Fable-PII hazard (today only a TOML comment); the $9 cap (today only a shell script); the `quick_desktop_doc` retirement; the four newer capture channels; the Codex data-locality decision (D3). (F-153)
2. Re-pin layer A to HEAD: counts generated from current code, configuration and plists (roles, migration high-water, cadence, spend thresholds); drop the dated "current local observation" from START-HERE; add Step 3.5 and the audit/revision stage to ARCHITECTURE; derive the schedule table from the plists; drop the obsolete "no native Codex/Outlook collector" claim; drop the README warning about the other lineage and its wrong commit count (the histories are disjoint — this is a port). (F-151, F-152, F-161) — done in R7 (`scripts/docs/inventory.py`, `docs/generated/`).
3. Add a **known-hazards list with owners** to VALIDATION.md, checked by `test_repo_hygiene.py` — the actual failure was that layer A recorded the 009 and fixture hazards on 09-10 and nobody closed them. (F-163) — done in R7.
4. Delete the redirect stubs (the archive holds the originals); archive the completed `PLAN-*.md` and `RESEARCH-skim-model.md` after promoting their contracts. Terminate the authority chain on code; remove every deferral to an operational inventory that does not exist in the repository. (F-162, F-154) — done in R7 (`docs/archive/2026-09-26/`, `docs/archive/2026-09-10-installed/`).
5. Specs: per track 08a/08b's verdict table — archive the drifted, update the four still-load-bearing, and create the eight missing families the refactor needs (routing enforcement, corpus dedup, relationship lifecycle, run ledger, capture framework, observability, retrieval regression gate, data-locality policy). Fix SPEC-INDEX (13 families; the `direct-api-backends` prose is inverted). (F-155…F-160)
6. One name per concept: panel / consensus / seat; channel ≠ arrival ≠ slice; the fault-tolerance analogy and its vocabulary leave the live docs (archives keep them). — done in R7 ([SPEC-INDEX.md](SPEC-INDEX.md), "Terminology").

---

## Phase 7 — Guardrails — carried by R1/R3/R7 (see Status)

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
| 0 | 2 one-line changes + 5 decisions | — | Known scheduled violations paused at audit time |
| 1 | Safety, routing, capture, retrieval and storage integrity | Moderate for code; high for live corpus/role changes | Reliable execution and test evidence before a bounded discovery trial |
| 2 | Shell consolidation + liveness predicates | Low | Every failure visible, including headless |
| 3 | Remove unused code and prepare data retirement | Low for isolated code; separate recovery work for tables/jobs | Less maintenance without silently breaking backups |
| 4 | Consolidation | Medium — touches every channel | One copy of each thing; suite −25 % |
| 5 | Module splits | Medium | The four E/F-complexity units become reviewable |
| 6 | Docs + specs | Low | An entry point that is true; specs to build from |
| 7 | Guardrails | Low | The five defect classes stop recurring |

**Not in scope:** new features, reading the knowledge graph, reviving Express email delivery (a product decision — the code is complete and has never been configured), the Astra `effort=medium` cost lever, and reconciling the rest of `origin/main` beyond the three fixes ported in Phase 1.
