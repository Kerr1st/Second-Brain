# Audit 2026-09-24 — Track 11: Git Archaeology

Repo: /Users/kerrfir/second-brain @ HEAD 6b8c2d2
Method: read-only git (log/show/diff/blame/rev-list/cat-file/count-objects/stash list/tag/worktree list). No pytest, no checkout.
Status: COMPLETE — all eight scope sections written; consolidated findings and "Could not verify" at the end.


## 0. Repo shape (context for everything below)

- 235 commits total, first commit 9ac1f1a (2026-03-03). Commits per month: Mar 82, Apr 3, May 1, Jun 82, **Jul 0, Aug 0**, Sep 67. So "last 90 days" == the September burst (67 commits, 2026-09-14..23, ten calendar days). Everything in sections 1–2 is therefore a ten-day window, not a quarter.
- Two long dormancies (Apr–May, Jul–Aug) bracket the two build bursts. Any "recent" churn analysis is really analysing one sprint.

## 1. CHURN (last 90 days)

### 1a. Commit-touch count (`git log --since=90.days --name-only | sort | uniq -c`)

| touches | file |
|---|---|
| 14 | src/dream_cycle/orchestrator.py |
| 10 | docs/PLAN-skim-stage.md |
| 8 | tests/test_dream_cycle.py |
| 7 | docs/PLAN-panel-acceptance.md |
| 5 | tests/test_resolver.py |
| 5 | scripts/jobs/dream_cycle_scheduled.sh |
| 4 | tests/test_grounding.py, tests/test_allocation.py, src/dream_cycle/packet.py, src/dream_cycle/allocation.py, src/dream_cycle_db.py, scripts/panel_replay.py, docs/MODEL-BACKENDS.md, config/backends.toml |
| 3 | tests/test_skim_*.py (4 files), tests/test_mcp_server.py, src/skim/faithfulness.py, src/prompts/skim.py, src/prompts/explorer.py, src/mcp_server.py, src/dream_cycle/ledger.py, src/db.py, scripts/jobs/verify_liveness.sh, docs/SKIM-STAGE.md, docs/PLAN-mcp-review.md |

### 1b. Lines changed (`--numstat`, added+deleted, top 15)

| Δlines | + | − | file |
|---|---|---|---|
| 1361 | 1361 | 0 | tests/test_codex_capture.py |
| 1098 | 1098 | 0 | src/capture/codex.py |
| 751 | 712 | 39 | src/backends/bedrock.py |
| 706 | 706 | 0 | src/capture/sources/codex.py |
| 702 | 637 | 65 | src/dream_cycle/orchestrator.py |
| 635 | 603 | 32 | src/dream_cycle/allocation.py |
| 588 | 81 | 507 | docs/MODEL-BACKENDS.md |
| 579 | 579 | 0 | tests/test_mcp_server.py |
| 551 | 551 | 0 | tests/test_allocation.py |
| 540 | 539 | 1 | docs/RESEARCH-skim-model.md |
| 504 | 504 | 0 | tests/test_skim_orchestrator.py |
| 473 | 471 | 2 | tests/test_bedrock.py |
| 411 | 394 | 17 | src/skim/schema.py |
| 399 | 365 | 34 | docs/PLAN-panel-acceptance.md |
| 368 | 338 | 30 | docs/PLAN-skim-stage.md |

### 1c. Correlation with the four named complexity hotspots

| hotspot | LOC now | commits (90d / all-time) | churn verdict |
|---|---|---|---|
| src/dream_cycle/orchestrator.py | 1201 | 14 / 34 | **Hot.** 14 touches in 10 days, +637/−65 (net growth ~570 lines, i.e. ~half the file was written in Sept). 9 of the 14 are `feat(...)`, 4 are `fix(...)`. Highest touch count in the repo AND largest single-file net growth among src/. This is the prime refactor seam. |
| src/capture/sources/codex.py | 706 | 1 / 1 | **Cold.** Landed whole in b6d4484 (2026-09-22) and never edited. Complexity is inherited from origin/main, not accreted. Its sibling src/capture/codex.py (1098 LOC, orchestration layer that imports `CodexDesktopSource` from sources/) got one follow-up fix (7abada7). Not duplicates: sources/codex.py = read-only adapter; capture/codex.py = pipeline. |
| src/search.py | 347 | 2 / 10 | **Warm, surgical.** Two Sept commits (13eb23e perf, 3d47290 fix) — the fix is a scoring-semantics change (RRF normalisation, AND-first FTS, inflection overlap). Low churn but every commit is a ranking behaviour change → regression risk per touch is high. |
| src/express.py | 734 | 0 / 3 | **Frozen.** Last touched 2026-06-06 (three commits in one day). Zero Sept edits despite the dream cycle around it being rewritten; 734 LOC untouched for 3.5 months. Candidate for "abandoned or stable?" — see §3 express_feedback. |

Other src/ files with high Sept churn not in the hotspot list: src/backends/bedrock.py (+712 in 2 commits, 03add74 + 9f15696), src/dream_cycle/allocation.py (+603 in 4 commits, all Sept 17–20), src/skim/schema.py (+394). These three plus orchestrator.py are the "skim/panel/allocation" sprint; together they're ~2,350 new src lines in six days (Sep 14–20).

Findings:
- **S2 — orchestrator.py is the single churn+complexity intersection.** 1201 LOC, 14 commits/10 days, 4 of which are fixes following feats within 24–72h (see §2). Fix: extract the skim step (Step 3.5, b6e8717) and the panel/verdict pipeline (aa73701/7ccea6f/1db8bf7) into their own modules; orchestrator keeps sequencing only.
- **S3 — docs churn > code churn for the sprint.** docs/PLAN-skim-stage.md (10 touches) and docs/PLAN-panel-acceptance.md (7) out-touch every src file except orchestrator. docs/MODEL-BACKENDS.md was rewritten (−507/+81 in one sprint). Plans are being used as running logs; consider moving "Phase N live results" out of PLAN-* into a dated log.
- **S3 — bedrock.py grew 712 lines in two commits with zero intermediate fixes.** Not necessarily bad, but it's the one big module with no fix trail — meaning either it's correct or it hasn't been exercised. §3 (Gemma path) suggests the latter for part of it.


## 2. FIX-ON-FIX CHAINS (last 90 days)

17 of 61 non-merge commits (28%) have a `fix(` subject; none are `hotfix` or `revert`. All 17 fall in 2026-09-14..23. Files touched by ≥2 fix commits within 7 days:

### Chain A — scripts/jobs/dream_cycle_scheduled.sh (3 fixes in 12h13m, preceded by feat + chore in the same file 3 days earlier)
```
84982ba 09-14 19:38 feat(dream-cycle): hybrid profile — Claude on Kiro, only GPT-6 Astra metered on Bedrock
5a9d92e 09-14 20:01 chore(dream-cycle): set the per-run Bedrock cap to the $6 budget
c5a63ac 09-17 20:06 fix(dream-cycle): take the corpus off Kiro — Auto routing served Fable   (hybrid → controlled)
dd79bd9 09-17 20:09 fix(claude_code): absolute default binary path (launchd PATH lacks ~/.toolbox/bin)   (first controlled run died: FileNotFoundError 'claude')
1db8bf7 09-18 08:19 fix(panel): unwrap nested candidate JSON …; run Mon/Wed/Fri at a $9 cap   (cap 6 → 9)
```
Cause: the profile default was flipped (c5a63ac) without running the new profile end-to-end under launchd; the very next run failed on PATH (dd79bd9, 3 minutes later), then the budget assumption for the new profile was wrong (1db8bf7).
**S2 — stale header comment survives the chain.** At HEAD the script's header (lines 16–19) still documents `hybrid = Claude roles on Kiro… ≈ $2.30/run measured` and "≈ 2.6x a typical $2.30 run" while the exported default is `controlled` and the cap is $9 for a ~$8.50 night. Three consecutive fixes each edited the block beneath the comment and none updated it. Fix: rewrite lines 16–24 to describe `controlled`; delete the hybrid cost figures or move them to config/backends.toml.

### Chain B — src/dream_cycle/orchestrator.py (3 fixes in 13h24m, on top of 2 feats the same afternoon)
```
d7427f4 09-17 (feat) attention ledger
198de51 09-17 (feat) allocate attention across the user's life
c53d45f 09-17 18:55 fix(dream-cycle): lessons from the first assembled run
95bac25 09-17 19:18 fix(dream-cycle): give every Thinker the slice contents inline
1db8bf7 09-18 08:19 fix(panel): unwrap nested candidate JSON from revisions
```
Three fixes within one day of two feats; each fix message describes a defect found by a live run ("lessons from the first assembled run", "a live revision returned the whole candidate as a JSON string"). Pattern: feat lands with unit tests, live run finds the integration defect, fix follows. **S3** — not a bug per se, but evidence that orchestrator.py has no integration test path that exercises a real (or recorded) model response shape; `_unwrap_nested_candidate` is a symptom-level patch on response parsing. Fix: a recorded-fixture replay test for the revision→accept path (scripts/panel_replay.py exists — 316655e — but is Direct-API-only).

### Chain C — scripts/weekly_digest.py (2 fixes, 7d 7h apart — just outside the 7-day window but included because the second contradicts the first)
```
0597acf 09-16 07:03 fix(jobs): stop distill and weekly-digest timing out on kiro   → KiroInvoker(model=claude-sonnet-5), timeout 600
e998f3b 09-23 14:13 fix(digest): run the weekly digest on Bedrock via the resolver, not Kiro
```
**S1 — the first fix's verification claim is contradicted by the second fix.** 0597acf's body says "Verified live: … digest 2026-W38 created in 24s." e998f3b's body says the job "had been timing out at 600s since ~2026-09-16 … so there was no digest after ~2026-09-16." Both cannot be true unless the 24s run was the one and only success. Separately, 0597acf put corpus content onto Kiro (Sonnet 5 via kiro-cli) on 09-16; c5a63ac on 09-17 established "this corpus never runs on [Kiro/Fable]" and moved the dream cycle off Kiro — but the digest, which also processes the corpus, was left on Kiro for six more days. The policy fix was applied to one caller and not grepped for the other. Fix: a test that asserts no script under scripts/ constructs `KiroInvoker` directly (e998f3b's docstring says test_resolver pins the rule for the resolver; it does not pin direct construction).

### Chain D — scripts/jobs/mcp_serve.sh (2 fixes, 2 days)
```
1cf4842 09-15 fix(mcp): launch the memory server from the project venv
567e97e 09-17 fix(jobs): run unattended jobs on scoped IAM credentials, awake, with a morning probe
```
Second is a sweep across 5 job scripts; benign.

### Chain E — scripts/jobs/verify_credentials.sh (2 fixes, 6 days)
```
567e97e 09-17 fix(jobs): … with a morning probe   (introduces the probe)
dfd7b9d 09-23 fix(monitors): stop the nightly false alarm that hid two real outages
```
The probe introduced on 09-17 was itself the false alarm fixed on 09-23 ("hid two real outages"). **S2** — a monitoring fix that introduced a noisier monitor; six days of alert fatigue masked real failures (per the commit's own claim). Fix: monitor scripts need a test for the *negative* case (healthy system → silent), which tests/test_monitor_jobs.py (added in dfd7b9d) now partially does.

Not chains (single fix each in window): search.py, mcp_server.py, crawlee.py, capture/{slack,outlook,mcp_client}.py, codex.py, panel_replay.py, claude_code.py, capture_api.sh.


## 3. ABANDONED / ORPHANED FEATURES

| feature | introduced | later references | status |
|---|---|---|---|
| **Gemma path** (`BedrockInvoker._is_gemma`, `outputConfig.textFormat` json_schema branch, `gemma-3-12b` price rows) | 9f15696 2026-09-20 "price and adapt Nova 2 Lite / Gemma 3 12B for the skim stage" | Only tests (test_bedrock.py, test_cost.py, test_skim_probe.py) and scripts/skim_probe.py's usage docstring (`--model google.gemma-3-12b-it`). **No profile in config/backends.toml selects a Gemma model** (grep `gemma` in config → 0 hits). No later commit. | **Orphaned production code, live only in tests.** S3. The skim role is pinned to Nova 2 Lite in all five profiles. Fix: either add a `[profiles.*]` skim option or delete `_is_gemma`/json_schema branch and the Gemma price rows; keep skim_probe support only if RESEARCH-skim-model.md needs a re-run. |
| **InvokeModel fallback** (`use_invoke_model`, `_invoke_model_fallback`, `_invoke_model_body` sentinel) | 03add74 2026-09-14 (first Bedrock backend commit) | bedrock.py only; `use_invoke_model=` is never passed `True` anywhere in src/, scripts/, config/ or tests/ (only the default `False` and the docstring). Prior audit (docs/audit-2026-09-23/03) also flagged. | **Dead code path since birth.** S3. ~40 lines + three `isinstance(...)_invoke_model_body` branches in `_extract_raw`/usage parsing add a second response shape that nothing produces. Fix: delete; re-add when a Converse-unsupported model is actually configured. |
| **DEFERRED verdicts** | Introduced bf5bf0a 2026-03-17 (original dream-cycle pipeline); **removed** 54d79a3 2026-03-18 ("4-evaluator *binary* BFT consensus panel") | Only as negative-space comments: consensus.py:4,13 "Binary output — no DEFERRED state", orchestrator.py:551 "REJECTED — no DEFERRED branch". Docs commits through 940624b (2026-09-14) still mention it. | **Removed 1 day after introduction; 6-month-old tombstone comments remain.** S3. Fix: drop the three "no DEFERRED" comments; grep docs for the word. |
| **Knowledge graph** (migration 008: `entities`, `entity_edges`) | d8aebec 2026-04-21 "Quick Desktop integration: memories, knowledge graph, hourly sync" | Writers: scripts/migrate/migrate_quick_desktop.py, scripts/migrate/import_slack_graph.py (both still run hourly from scripts/jobs/qd_sync.sh lines 24,27). **Readers in src/: none** — no `FROM entities`/`entity_edges` in src/ (search.py, mcp_server.py, express.py, dream_cycle/* never query it). bf0af59 2026-04-22 added KG table exports to backup. Last code touch of a KG reader/writer: 7db3a6c 2026-05-31. | **Write-only table for 5 months.** S2. Hourly job populates tables nothing reads; backup exports them nightly. Fix: either wire an entity leg into search/express or stop the two writers and drop 008's tables in a new migration (don't edit 008 — see §7). |
| **express_feedback** (migration 010) | ef0d730 2026-06-06 | src/express.py (reads/writes), scripts/brief.py `--useful/--less/--mute`, tests/test_express.py. express.py has **zero commits since 2026-06-06**; express_push.py is still chained in dream_cycle_scheduled.sh. | **Alive but frozen.** S3. No evidence (in git) that the flags were ever used after launch; if the table is empty on the live DB it is a feature nobody adopted. Could not verify table row count (no DB access in this track). |
| **Codex canary** (scheduling/com.second-brain.codex-canary.plist, scripts/jobs/codex_capture_canary.sh, tests/test_codex_canary_job.py) | b6d4484 2026-09-22 (ported from origin/main) | No later commit. `~/Library/LaunchAgents` contains `com.second-brain.codex-capture.plist` but **not** `com.second-brain.codex-canary.plist`. | **Shipped, tested, never installed.** S2. The canary is the thing that would tell you the capture stopped; the capture is installed, its watchdog isn't. Fix: install the plist (or delete it and the test if the liveness monitor already covers `codex` source_type — verify_liveness.sh should be checked). |
| **CodexInvoker** (src/backends/codex.py) | fe8e798 2026-06-17 "add Claude Code and Codex adapters with MCP startup probe" | Registered in resolver.py:31 (`DEFAULT_ADAPTERS`); tests/test_codex.py, test_resolver.py. **No profile in config/backends.toml uses `backend = "codex"`** (grep → 0). Zero commits to the file since 06-17. | **Registered adapter with no consumer, 3 months.** S3. Not the same thing as the Codex *capture* (b6d4484) — naming collision `src/backends/codex.py` vs `src/capture/codex.py` is itself a hazard. Fix: remove from DEFAULT_ADAPTERS or add a profile; consider renaming one module. |
| **hybrid profile** | 84982ba 2026-09-14 | Default for the nightly job for 3 days (84982ba → c5a63ac 09-17); still defined in backends.toml (lines 84–102) with the comment "Deliberately Opus 5 rather than Kiro's claude-fable-5.1" — which is precisely the guarantee c5a63ac discovered Kiro does not honour. Still referenced by dream_cycle_scheduled.sh header comment (§2 Chain A) and c5a63ac's "Revert with SECOND_BRAIN_PROFILE=hybrid once Kiro honours --model". | **Superseded but retained as a documented escape hatch that is unsafe to use.** S2. Anyone reading backends.toml [profiles.hybrid] is told it avoids Fable; the resolver has no guard preventing selection. Fix: either delete `hybrid` and `laptop` (both route corpus to Kiro) or make the resolver refuse a `kiro` backend for corpus roles unless an explicit override env is set. |
| **quick_desktop_doc** (source_type; scripts/migrate/ingest_doc_chunks.py) | 7db3a6c 2026-05-31 / 862d040 2026-06-02 | dfd7b9d 2026-09-23 marks the liveness channel `retired` ("last produced 2026-06-04 … upstream Quick doc export stopped"). **But qd_sync.sh line 29 still runs ingest_doc_chunks.py hourly**, and src/prompts/skim.py (7a99e6e) lists it as a channel prior. | **Retired in the monitor, still executed in the job, still described to the skim model.** S2 — three components disagree about whether this source exists. Fix: comment out line 29 of qd_sync.sh (or gate it on the upstream-file check dfd7b9d added) and remove the prior from the skim prompt. |

Cross-cutting finding, **S2**: five of the nine items are "adapter/branch/table exists, nothing selects it" (Gemma, InvokeModel, KG readers, CodexInvoker, canary plist). The codebase has no lint for "registered but unreferenced" surfaces — a test that every adapter in `DEFAULT_ADAPTERS` and every migration table is referenced by at least one profile / one `src/` reader would have caught all five.


## 4. THE REMOTES (read-only; no fetch — refs are as last fetched)

Remotes: `origin` = github.com/Kerr1st/Second-Brain.git, `mini` = kerrclaw@192.168.1.224:repos/second-brain.git.
`git merge-base main origin/main` and `main mini/main` both return nothing: **unrelated histories**. origin/main's root 0dd78c2 "Initial public snapshot" (2026-06-24) is a squashed copy of roughly local 7bf8d41 (2026-06-17) — `git diff --stat 0dd78c2 7bf8d41` = 52 files, mostly docs/plists/paths (the snapshot scrubbed absolute paths). mini/main == 0dd78c2 exactly (1 commit ahead of nothing; it is the snapshot and nothing else — the Mac Mini clone was never updated after 06-24).

### 4a. origin/main-only commits (20; `git log --oneline main..origin/main`)
```
8c0d58c 09-06 Merge PR #4 codex/current-state-recall
3b3bfcc 09-06 Record live recall verification and remaining ownership coverage gap
e8b9862 09-06 Keep unknown source observation times distinct from processing dates
2e7d49d 09-06 Deliver later topic evidence and exclude internal Codex executions
9911b92 09-06 Merge PR #3 codex/current-codex-message-capture
46911e6 09-06 Merge PR #2 codex/atomic-steering-approval
f56aa96 09-06 Capture current Codex authored text messages with native provenance
09362fb 09-05 Make steering approval atomic and serialize competing approvals
a318fff 09-02 Merge PR #1 codex/codex-task-capture
5335d80 09-01 Activate production Codex capture and stable retrieval
6fc13c6 08-29 Activate bounded Codex capture canary
30ee19e 08-29 Document the Codex vertical slice proof
eeef17c 08-29 Add consensus-gated steering governance
295d63d 08-29 Add bounded context packs and outcome receipts
f2fb911 08-29 Address final Codex capture review findings
de17a23 08-29 Document Second Brain component architecture
6b9e9aa 08-29 Capture Codex tasks with semantic provenance
c073b01 08-29 Add Codex JSONL model backend telemetry
91d2925 08-29 Harden migration and test database safety
0dd78c2 06-24 Initial public snapshot
```
Note the local repo's Jul–Aug gap (§0) is exactly when origin/main was active: the work moved to GitHub for ~6 weeks and came back only as the b6d4484 port.

### 4b. What b6d4484 ported, and what it didn't
- Ported byte-identically (`git diff b6d4484 origin/main -- <f>` empty): src/capture/codex.py, src/capture/sources/codex.py, src/capture/agent_tasks.py, scripts/capture_codex.py, scripts/jobs/codex_capture*.sh, both plists, fixtures, 4 test files. The port message says "PRs #1-#3" but the code matches origin *after PR #4* (3b3bfcc et al.), so PR #4's fixes (e8b9862 observation-time, 2e7d49d internal-execution exclusion) **are** present locally. Message understates scope — see §8.
- Local has since diverged: 7abada7 adds 207 lines (semantic retry bound) not on origin.

### 4c. origin-only fixes local still lacks
| origin commit | what | local state | severity |
|---|---|---|---|
| 91d2925 migrations/migrate.sh | `psql -X -v ON_ERROR_STOP=1`; advisory lock; migration SQL + tracking INSERT in **one transaction** | Local migrate.sh (last touched aadaa15 2026-06-12): no `ON_ERROR_STOP`, so a file whose 3rd statement errors still exits 0 and is **recorded as applied**; `2>/dev/null` on the bootstrap hides errors; dead `CONTAINER` variable. | **S1** — a half-applied migration is silently marked done. Fix: port the origin migrate.sh verbatim (it's env-compatible: DB_HOST/DB_PORT default to the same 127.0.0.1:5432). |
| 91d2925 migrations/009_hnsw_recall.sql | `ALTER DATABASE current_database()` instead of hard-coded `memory_bank` | Local 009 line 27 still `ALTER DATABASE memory_bank` → fails on any test/scratch DB with another name. | S2 (but see §7: don't edit an applied migration; add 016 that re-applies via `current_database()` or fix only in a fresh-DB path). |
| 91d2925 tests/test_schema_migration_runner.py, test_test_database_safety.py | Tests that the runner is transactional and that tests can't hit the prod DB | Absent locally. | S2 |
| 012_agent_task_capture.sql: `uq_memories_codex_captured_task`, `uq_memories_codex_topic_segment_index` partial unique indexes | DB-level dedup for the Codex capture | **Not ported.** Local src/capture/codex.py:528 does SELECT-then-INSERT; the only guard is the file lock `_exclusive_lock` (line 227). A second writer path (manual `scripts/capture_codex.py` while launchd job runs — the lock covers this; a restore/backfill that bypasses the lock does not). | S2 |
| 012: `UPDATE … SET expired_at = NULL WHERE relation_type='derived_from'` + `CHECK (relation_type <> 'derived_from' OR expired_at IS NULL)` | derived_from is provenance, must never expire | **Not ported** (confirms the live-data track). Local `expire_stale_relationships` (src/dream_cycle_db.py:835, called from orchestrator.py:567 every run) expires derived_from edges after 90 days of no access → `get_schema_with_constituents` (db.py:346–403) and the Codex capture's provenance edges (codex.py:1043) silently lose links. **Caveat:** origin's own `expire_stale_relationships` is unchanged (identical to local) — with the CHECK in place that UPDATE would *raise* on the first stale derived_from edge, so origin has a latent crash and local has silent data loss. Porting the CHECK alone would move local from the second to the first. | **S1** — fix both: add `AND mr.relation_type <> 'derived_from'` to the UPDATE, then a new migration adding the CHECK + one-time repair. |
| 012 `idx_relationships_derived_from_target` | reverse-lookup index | Absent. db.py:381,403 query by target — likely seq-scan. | S3 |
| **Migration numbering fork** | origin 012–015 = agent_task_capture, context_governance, local_embedding_space, enforce_active_embedding_space; local 012–014 = dream_cycle_slices, evaluator_details, memory_skim | `schema_migrations.version` is the *filename*, so a future port of origin's 012 as "012_agent_task_capture.sql" would apply cleanly beside local 012 — no collision in the tracking table, but two files share the prefix and the "next number" is ambiguous (015 vs 016). | S2 — decide the canonical sequence now; port origin content as 016+ and never reuse 012–015. |
| 013 context_receipts / src/context_broker.py, eeef17c steering governance / src/steering.py, 014–015 local BGE-M3 embedding space (`legacy_embedding`, `embedding_space`, `reembed-local` plist) | Whole features | Not present locally; no local reference. Not "fixes", but 014/015 imply origin's live DB has a **different embedding column layout** — the two clones are no longer schema-compatible. | S2 (informational) |
| c073b01 src/backends/codex.py telemetry (61 lines), 09362fb steering atomicity | CodexInvoker JSONL telemetry | Local src/backends/codex.py unchanged since 06-17; since no profile uses `codex` (§3), moot. | S3 |

### 4d. mini/main
One commit, 0dd78c2, identical to origin's root. The Mac Mini has never received the June–September work. `[profiles.mini]` in backends.toml (Claude Code on Bedrock) therefore describes a machine running 3-month-old code with the *old* migrate.sh and no 012+ migrations. Could not verify what is actually deployed there (no SSH in this track).


## 5. HYGIENE

| check | result |
|---|---|
| `git worktree list` | one: /Users/kerrfir/second-brain @ 6b8c2d2 [main] |
| `git branch -a` | local: `main` only. Remote: origin/main + 5 `origin/codex/*` feature branches (all merged into origin/main per §4a), mini/main. No surviving local feature branches. The September work *did* use short-lived branches — six merge commits (6bf67bc attention-allocation, 4958caf panel-acceptance, 2dcf10e skim-stage, 619146d mcp-review, 5386c2b codex-capture-port, 34bc2b5 capture-hardening) — all deleted after merge. Fine hygiene; but the merge commits carry the only summary of each branch and docs cite them (e.g. 5177b17 cites `34bc2b5`), so they must never be squashed away. |
| `git tag` | **0 tags.** Nothing marks the June "public snapshot", the profile switch (c5a63ac), or the skim launch. S3 — no rollback anchors. |
| `git stash list` | 0 |
| `git count-objects -vH` | 2712 loose objects (13.41 MiB) vs 731 packed (1.13 MiB); `prune-packable: 221`. Loose:packed ratio ~4:1 — the repo has not been gc'd in a long while. S3 (cosmetic; **do not** run gc from this track). |

### 5a. Largest blobs in history (unique paths, `rev-list --objects --all | cat-file --batch-check`)
| bytes | path |
|---|---|
| 122,644 | tests/test_dream_cycle.py (15 of the top-15 raw blobs are versions of this one file, 103–123 KB each) |
| 59,349 | src/dream_cycle/orchestrator.py |
| 59,234 | docs/DREAM-CYCLE-DESIGN.md |
| **57,803** | **logs/bookmarks-test-20260309.log** |
| **57,515** | **logs/bookmarks-20260309-2044.log** |
| 51,431 | tests/test_codex_capture.py |
| 48,386 | .kiro/specs/dream-cycle/design.md |
| 47,939 | docs/RESEARCH-skim-model.md |
| 46,510 | tests/test_dream_cycle_db.py |
| 45,494 | tests/test_mcp_server.py |
| 41,448 | src/capture/codex.py |
| **41,191** | **logs/bookmarks-20260309-2048.log** |

No SQL dump, `.dump`, `.gz`, sqlite or `.env` (only `.env.example`) was ever committed. But:

### 5b. **S2 — eleven run logs were committed on 2026-03-08..10 and are still in history**
Added by 977a1c4, 38ee2b6, 9c988e1, 7e10cf9, 7ecc00e; removed by ef53835 (2026-03-10 "code review — security, correctness, and consistency fixes"); `logs/` added to .gitignore. Files: logs/{backup-20260310, backup-test-20260309, bookmarks-20260309-2044, bookmarks-20260309-2048, bookmarks-resume, bookmarks-test-20260309, chat_extract-20260308, chat-extract-20260310, ingest-20260310, ingest-chats-20260309, youtube-20260310}.log.
Content check on the three bookmark logs (blobs e4d333fd, 80b491cd, 3b06910c): 439 bookmark title→URL lines in the first; 44 lines match internal Amazon hostnames (inside.amazon.com, *.corp.amazon.com, amazon.dev, …); one line is an `idp.federate.amazon.com/...?id_token` URL (truncated in the log — no token value visible, but the URL shape is a federation callback). No `bearer`/`password`/`secret` matches. Assessment: a personal browser-bookmark inventory including employer-internal URLs is in the permanent history of a repo that has a public GitHub remote. These commits are **local-only** (`git branch -r --contains 7e10cf9` is empty — origin's history starts at the June squash), so the exposure is: this clone and any backup of `.git/`. Fix: acceptable to leave as-is given the remote never received them; if the local history is ever pushed anywhere, filter-repo `logs/` first. Do **not** attempt from this track.

### 5c. S3 — `backups/` committed once (462 path-entries: backups/kb-sources 220 markdown KB exports, backups/config incl. copies of migrations 000/001, docker-compose, DISASTER-RECOVERY.md, and seven `kiro-skills/*/SKILL.md`)
Added and removed the same day (2026-03-09, a5fec7b "add backups/ to .gitignore — should not be in repo"). No dumps; the KB sources are scraped public blog posts. Also local-only.

### 5d. S3 — tests/test_dream_cycle.py is the repo's storage hog
15 consecutive 100–123 KB versions; 8 commits in Sept alone. A 3,000-line test file that is rewritten on every orchestrator change is a churn amplifier (it is also why orchestrator refactors are expensive). Fix: split by stage (skim / allocation / panel / storage) to match the module split proposed in §1.


## 6. THE UNCOMMITTED WORKING TREE (87 entries at time of reading: 77 modified, 10 untracked)

`git diff --stat`: 77 files, +526 / −6,023.

| bucket | count | what |
|---|---|---|
| docs/ (modified) | 38 | 20 top-level + 18 docs/user-guide. **24 files reduced to ≤10-line stubs** (−100..−346 lines each) pointing at `docs/archive/2026-09-10/…` and `docs/SPEC-INDEX.md`/`START-HERE.md`. Pattern: "documentation reconciliation 2026-09-10". |
| .kiro/specs/** (modified) | 36 | Each gains a 3–4 line "Historical specification — documentation reconciliation 2026-09-10" banner. |
| README.md, scheduling/README.md (modified) | 2 | −107 / −45 lines, same reconciliation. |
| **src/backends/kiro.py (modified)** | **1** | 4-line import reshuffle: take `_write_metrics_line` from `src.backends.base` instead of the `agentic_cli` re-export. mtime 2026-09-18 05:33. Both import paths resolve at HEAD (agentic_cli.py:42 re-exports). Cosmetic; behaviour-neutral. |
| untracked | 10 | `.dream/` (one run artifact: explorer_slices_2026-09-16.json — **no writer found in src/ or scripts/ by grep; not in .gitignore**), `.kiro/specs/direct-api-backends/`, docs/{AUDIT-2026-09-23.md, PLAN-refactor.md, SPEC-INDEX.md, START-HERE.md, VALIDATION.md}, docs/archive/ (492 KB, the full pre-stub copies), docs/audit-2026-09-23/, docs/audit-2026-09-24/ (this file). |

### Age
- Oldest modified mtime: **2026-09-10 20:02** (the .kiro/specs banners), docs 20:02–20:15 the same evening. HEAD is 2026-09-23 19:12. So the reconciliation has sat uncommitted for **13 days and 67 commits**. Every September commit — including nine `docs(...)` commits touching docs/PLAN-*.md, docs/MODEL-BACKENDS.md, docs/SKIM-STAGE.md — was made on top of a dirty docs tree.
- src/backends/kiro.py has been dirty for 5 days / ~20 commits.

### Findings
- **S2 — the working tree and HEAD describe two different documentation systems.** HEAD's docs/ARCHITECTURE.md is a 327-line design doc; the working copy is a 107-line pointer to START-HERE.md, which is untracked. Anyone who `git clone`s or `git stash`es gets the June docs with none of the September docs commits' context (they were written assuming PLAN-*.md are the running logs, not SPEC-INDEX). The two states cannot both be "current". Fix: commit the reconciliation as one `docs: reconcile 2026-09-10` commit **excluding** src/backends/kiro.py, or revert it. The decision has been deferred for two weeks; every day adds merge friction with the committed PLAN-*.md edits.
- **S3 — bundling risk.** `git add -A` at this point would bundle: a src import change, 74 doc stubs, a 492 KB archive tree, two audit directories, a run artifact (`.dream/`), and a new spec directory — six unrelated changes in one commit. Fix: commit in ≥4 separate commits; add `.dream/` to .gitignore first (it's a per-run JSON from the attention ledger by name, though I could not locate the writer — see Could-not-verify).
- **S3 — docs/archive/2026-09-10 duplicates history.** The archive folder is a copy of files git already has at HEAD; once the stubs are committed the archive is redundant with `git show HEAD~1:docs/...`. 492 KB of duplicated markdown. Fix: drop the archive folder, keep the stub pointers, cite the commit hash instead of a path.
- Risk of loss: low (all changes are in the tree, not stashed), but risk of *accidental commit* with the wrong grouping is high.


## 7. POST-HOC EDITS TO MIGRATIONS (`git log --follow` on each migrations/*.sql)

Only one migration file has more than one commit: **migrations/001_initial_schema.sql** (4; the first, 93f28a5, is the rename from a pre-migrations schema.sql).

| commit | date | change to 001 | applied-DB state at the time |
|---|---|---|---|
| 4b26ba7 | 03-08 | file becomes a migration (`vector(1536)`) | migrations system born; live DB existed (chat_extract-20260308.log committed same day) |
| 892c972 | 03-09 | `embedding vector(1536)` → `vector(1024)` "for Titan v2" | Live DB had already ingested (ingest-chats-20260309.log). The commit does not add a migration to alter the live column; the live DB's column was presumably fixed by hand or by a re-create. |
| ef53835 | 03-10 | adds `search_vector TSVECTOR`, `access_count INTEGER`, GIN index, `memories_search_vector_update()` trigger — body says "**(matches live DB)**" | 38ee2b6 (03-09) shipped code querying `search_vector` with the message "Schema: added search_vector tsvector + GIN index + auto-populate" but **touched no migration file**. The columns were added to the live DB out-of-band, then back-filled into 001 a day later. |

Findings:
- **S2 — 001 is not the schema that was applied; it is a retro-fitted description of the live DB.** Two out-of-band DDL changes (dimension change, FTS columns/trigger) were folded into an already-applied migration. Consequences: (a) `schema_migrations` says 001 applied on 03-08 but the file's content is from 03-10; (b) any DB that ran 001 between 03-08 and 03-10 (e.g. the Mac Mini if bootstrapped then — could not verify) has a 1536-dim column and no `search_vector`, and the runner will never fix it because 001 is "applied". This is precisely the failure mode origin's 91d2925 tests (`test_schema_migration_runner.py`) exist to catch. Fix: leave 001 alone now (six months later, changing it again makes it worse); add a `tests/test_migration.py` assertion that the live schema's `memories.embedding` typmod is 1024 and `search_vector` exists, so drift is detected rather than assumed.
- **S3 — 008_knowledge_graph.sql declares `entities.embedding vector(1536)`** (line 17) with an ivfflat index, while 001 (post-fix) and src/embeddings.py are 1024-dim. The KG writers (scripts/migrate/*.py) never populate `embedding`, so the column is always NULL and the mismatch is latent — but the day someone wires entity embeddings, every insert fails on dimension. Fix: new migration `ALTER TABLE entities ALTER COLUMN embedding TYPE vector(1024)` + drop/recreate index (or drop the column with the KG, per §3).
- Positive: migrations 002–014 have exactly one commit each — the post-hoc habit stopped after March. 005 legitimately redefines `memories_search_vector_update()` via `CREATE OR REPLACE` in a *new* file, which is the right pattern.
- Related (from §4c): origin's 91d2925 edited 009 post-hoc (hard-coded DB name → `current_database()`); local 009 still hard-codes `memory_bank`. If ported, port as a new migration, not by editing 009.


## 8. MESSAGE-vs-DIFF (15 most recent non-merge commits, 3efb224..6b8c2d2)

| commit | subject claim | diff shows | verdict |
|---|---|---|---|
| 6b8c2d2 docs(mcp) | "dedup backup tables dropped after verifying every deleted row had a surviving twin" | +5 lines in PLAN-mcp-review.md recording a **live DB DROP** (102 MB, two `*_dedup_backup_2026_09_22_1340` tables) done by hand. No script, no SQL in the repo. | Message matches diff (it's a log entry). **S2 process finding**: an irreversible production-data operation exists only as prose; the verification query ("247 groups, 0 without survivor") is not in the repo. Fix: commit the verification SQL alongside. |
| 5177b17 docs(capture) | "all channels healthy, monitors fixed, digest off Kiro — phases 1/2/5 done" | Plan checkboxes + narrative. Cites `34bc2b5` (resolves: merge commit). States Slack/Outlook "**self-healed** … Hardened anyway" | Consistent. Note the word "self-healed": the hardening in f4c4ddc was not what fixed the outage. |
| e998f3b fix(digest) | "run the weekly digest on Bedrock via the resolver, not Kiro" | Also **adds a new job wrapper** scripts/jobs/weekly_digest.sh (53 lines) and repoints the plist. Body does not mention either file. | S3 — subject/body under-describe; the plist change is what actually switches the live job, and it's silent. |
| dfd7b9d fix(monitors) | "stop the nightly false alarm that hid two real outages" | verify_credentials.sh/verify_liveness.sh rewritten (+251/−45), 321-line test. Body matches (PATH bug, three-state, severities, retired quick_desktop_doc). | Consistent. But see §3: retiring quick_desktop_doc in the monitor while qd_sync.sh still runs its ingester is a half-change the message doesn't flag. |
| f4c4ddc fix(capture) | "classify Slack/Outlook failures and survive one server death" | mcp_client.py +259 (one bounded reconnect, `DEFAULT_RECONNECT_BACKOFF`), slack preflight, outlook retry-once. | Consistent. |
| 7abada7 fix(codex-capture) | "bound the semantic retry so a failing task cannot burn money forever" | +207/−0 in codex.py. Zero deletions for a control-flow fix looked suspicious; verified the guard is *inserted* into `_process_semantics` (lines 327–360 at HEAD) before the model call. | Consistent. |
| 9f9f7ea docs(capture) | plan +164 lines | docs only | Consistent. |
| 778a9e5 feat(capture) | "wire Codex capture for this machine" | Also bumps **`PROMPT_VERSION` skim-v2 → skim-v3** and adds two channels to config/skim.toml and the skim prompt. Body does list it ("skim: codex_task and distilled_agent_task are own-activity channels; PROMPT_VERSION skim-v3"). | S3 — subject omits a prompt-version bump that changes every future skim record's provenance; bundled with launchd wiring. Should have been its own commit (`feat(skim): …`). |
| b6d4484 feat(capture) | "port … PRs #1-#3 on origin/main" | Code is byte-identical to origin/main **after PR #4** (§4b). Also claims "42 ported tests pass, full suite 1509 green". | **S3 — message under-states scope** (PR #4's two fixes are included but unattributed). Test-count claim could not be verified (no pytest in this track). |
| 5821319 docs(mcp) | "corrected skim workload figures" | 4 docs files, small edits | Consistent. |
| 3d47290 fix(search) | "normalise RRF …; dedup cleanup script" | search.py/rerank_weights.py/test_rerank.py **plus** a new 135-line scripts/cleanup_duplicate_sources.py that deletes rows from the live DB. | **S2 — bundles a ranking-semantics fix with a destructive one-off data script.** The subject does say "dedup cleanup script", but a reviewer of the search fix would not expect a DELETE tool in the same diff; reverting the search change would also remove the script (and vice versa). Fix: split. |
| e3e2d80 fix(mcp) | "validate tool inputs at the boundary" | new src/validation.py, mcp_server.py +178, 478 test lines | Consistent. |
| bcbac68 fix(ingest) | Crawlee parser | parser +287/−68, ingest script, tests | Consistent. |
| c4f5f5d docs(mcp) | review plan | docs only | Consistent. |
| 3efb224 docs(panel) | "observation run 3 of 5 (720f03b3)" | docs only | Consistent. `720f03b3` is a **dream-cycle run id**, not a commit — five such 8-hex ids appear in Sept docs (05171b48, 3a1263a6, 3dbd6505, 47fffa94, 720f03b3) formatted identically to commit hashes. S3 — ambiguous notation; prefix run ids (`run:720f03b3`). |

Summary: 0 of 15 messages claim something the diff contradicts. 3 under-describe (e998f3b, 778a9e5, b6d4484), 2 bundle unrelated concerns (3d47290, 778a9e5), 1 records an irreversible live-DB operation with no artefact (6b8c2d2). Message quality is high; the risk is in what's *omitted* from subjects (prompt-version bump, plist repoint, destructive script).

## Consolidated findings (severity-ordered)

| # | sev | finding | evidence | fix |
|---|---|---|---|---|
| 1 | S1 | migrate.sh records a migration as applied even if its SQL half-fails (no `ON_ERROR_STOP`, no transaction); origin fixed this 08-29 and local never ported it | migrations/migrate.sh @HEAD vs origin 91d2925 (§4c) | Port origin's migrate.sh |
| 2 | S1 | `expire_stale_relationships` expires `derived_from` provenance edges after 90 days (silent link loss); origin's CHECK constraint was never ported — and porting it alone would make the same UPDATE crash | src/dream_cycle_db.py:835, orchestrator.py:567, origin 012 (§4c) | Exclude derived_from in the UPDATE, then add CHECK via new migration |
| 3 | S1 | Contradictory verification claims: 0597acf "digest 2026-W38 created in 24s" vs e998f3b "no digest after ~2026-09-16"; corpus left on Kiro 6 days after the c5a63ac policy | §2 Chain C | Test forbidding direct `KiroInvoker` construction in scripts/ |
| 4 | S2 | `quick_desktop_doc` retired in monitor, still executed hourly, still a skim prior | dfd7b9d, qd_sync.sh:29, prompts/skim.py (§3) | Gate/disable line 29; drop prior |
| 5 | S2 | Codex canary plist committed, tested, never installed | b6d4484; ~/Library/LaunchAgents (§3) | Install or delete |
| 6 | S2 | `hybrid`/`laptop` profiles remain selectable and documented as Fable-safe after c5a63ac proved otherwise | backends.toml 33–102 (§3) | Resolver guard or delete |
| 7 | S2 | Knowledge-graph tables written hourly, read by nothing, for 5 months; `entities.embedding` is 1536-dim vs 1024 everywhere else | 008, qd_sync.sh, no `FROM entities` in src/ (§3, §7) | Wire or drop |
| 8 | S2 | Docs reconciliation (74 files, −6k lines) uncommitted 13 days across 67 commits; HEAD and tree disagree on the doc system | §6 | Commit in split commits or revert |
| 9 | S2 | 001_initial_schema.sql edited twice after the live DB applied it; file ≠ what was run | 892c972, ef53835 (§7) | Schema-drift assertion test |
| 10 | S2 | Eleven run logs incl. 44 employer-internal URLs in permanent local history (never pushed) | 977a1c4..7ecc00e, removed ef53835 (§5b) | Leave; filter before any push of local history |
| 11 | S2 | dream_cycle_scheduled.sh header still documents `hybrid` after three fixes changed the profile and cap | §2 Chain A | Rewrite lines 16–24 |
| 12 | S2 | Migration numbering fork 012–015 between local and origin | §4c | Fix canonical sequence; port as 016+ |
| 13 | S2 | Codex capture dedup relies on a file lock only; origin's partial unique indexes not ported | codex.py:227,528 vs origin 012 | Port indexes as new migration |
| 14 | S2 | Live-DB DROP and DELETE operations recorded as prose / bundled into a search fix | 6b8c2d2, 3d47290 (§8) | Commit verification SQL; split commits |
| 15 | S2 | orchestrator.py: 1201 LOC, 14 commits/10 days, 3 fixes within 13h of 2 feats; test_dream_cycle.py 15×100–123 KB versions | §1, §2B, §5d | Split skim/panel out of orchestrator; split the test file |
| 16 | S3 | Dead code since birth: `use_invoke_model`, `_is_gemma` (no profile selects Gemma), CodexInvoker (no profile uses `codex`), DEFERRED tombstones | §3 | Delete or wire |
| 17 | S3 | mini/main is the 06-24 snapshot; the Mini has never received Jun–Sep work; `[profiles.mini]` describes stale code | §4d | Verify deployment; update or drop profile |
| 18 | S3 | 0 tags; 2712 loose objects; `.dream/` untracked and unignored; run ids formatted like commit hashes in docs | §5, §6, §8 | Tag releases; gitignore `.dream/`; prefix run ids |

## Could not verify (git-only track; no DB, no SSH, no pytest)
- Row counts in `express_feedback`, `entities`, `entity_edges` on the live DB (whether KG/feedback are *used*, not just written).
- What code/migrations the Mac Mini (`mini` remote host 192.168.1.224) is actually running.
- Whether the live DB's `memories.embedding` is 1024-dim and has `search_vector` (i.e. whether the 001 retro-fit matches reality) — ef53835 asserts it does.
- b6d4484's "full suite 1509 green" and every other test-count claim.
- Whether the `idp.federate.amazon.com/...?id_token` line in blob e4d333fd ever contained a token value (the committed log truncates the URL).
- Whether `~/Library/LaunchAgents/com.second-brain.codex-canary.plist` was deliberately omitted or forgotten — no commit or doc says.
- The writer of `.dream/explorer_slices_2026-09-16.json` (grep for `.dream` and `explorer_slices` in src/ and scripts/ found nothing; may be an ad-hoc export).
- Whether origin/main and mini/main refs are current — no fetch performed by rule; refs reflect the last fetch before this audit.

Status: COMPLETE.
