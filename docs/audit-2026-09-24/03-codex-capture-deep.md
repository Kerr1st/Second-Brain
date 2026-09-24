# 03 — Codex capture subsystem, deep review

Repo `/Users/kerrfir/second-brain` @ `6b8c2d2` (2026-09-24). Read-only review; the only file written is this one.
Input: `docs/AUDIT-2026-09-23.md`, `docs/PLAN-refactor.md`, `docs/audit-2026-09-23/02-capture-and-ingest.md` (track 02), `docs/audit-2026-09-24/static/*`.

Scope read in full: `src/capture/sources/codex.py` (706), `src/capture/codex.py` (1098), `src/capture/agent_tasks.py` (185), `scripts/capture_codex.py` (63), `scripts/jobs/codex_capture.sh` (54), `tests/test_codex_capture.py` (1361), `tests/test_codex.py` (742 — this file tests the **`CodexInvoker` backend**, not capture; it is out of the capture subsystem and is noted only in §7), plus `src/db.get_connection`, `src/backends/{base,direct_api,resolver}.py`, `config/backends.toml`, the plist, `scripts/jobs/verify_liveness.sh`, and `origin/main` versions of every ported file.

Evidence sources beyond code: `git diff origin/main HEAD`, `logs/codex-capture.log`, `logs/llm_metrics/adhoc.jsonl`, read-only `psql` on `memory_bank`, read-only `sqlite3 ?mode=ro` on `~/.codex/state_5.sqlite`, and a structural pass over the 32 user-owned rollout files under `~/.codex/sessions` (record types and metadata shapes counted; **no prompt or answer text was read, quoted, or retained**).

Severity key (as instructed): **S1** corrupts/hides data or spends unmetered · **S2** rule unenforced or failure invisible · **S3** duplication / dead code / drift.

---

## 0. Verdict on the "reference implementation" claim

Track 02 and `PLAN-refactor.md` §"Adopt Codex's patterns" call `src/capture/codex.py` "the only transactionally correct channel, the only one detecting partial prior capture, the only one enforcing tz-awareness — the framework should adopt its patterns". Having read the 450 lines the prior audit admits it skipped:

| Prior claim | Verdict | Where |
|---|---|---|
| Transaction/`FOR UPDATE`/`rollback()` design is correct | **Confirmed for the three write functions** (`_capture_snapshot`, `_update_task_metadata`, `_store_semantic_result`). Every early return and exception path either commits or rolls back; `db.get_connection` (`src/db.py:55-63`) additionally rolls back and `putconn`s on exception. No leaked locks found. | §1 |
| Detects partial prior capture via turn-id set difference | **Confirmed** and **verified against 313 live turns** (my re-implementation of the gating over the 32 live rollouts reproduces exactly the 313 stored turns). But identity soundness depends on *which* of two prompt representations Codex emits, and the code deliberately lets the legacy one win (§2). | §2 |
| Bounded, persisted semantic retry | **Confirmed**, but the bound counts **infrastructure failures** (Bedrock throttle, expired AWS creds, Titan outage, timeout) identically to contract failures, so an 18-hour outage permanently abandons every eligible task's semantic layer with exit 0 (§4, C-1). | §4 |
| Enforces tz-awareness | Confirmed (`agent_tasks._aware`, `codex.py:250`, `sources/codex.py:382`). | — |
| "Only channel that is transactionally correct" ⇒ adopt as reference | **Refuted as a *reference pattern***: the adapter's `fetch_task` is a 79-branch state machine of which the legacy half handles a rollout format that appears in **0 of 32 live rollouts and 0 of 313 stored turns**; the module silently drops 6.7 % of authored user turns with no counter; it re-parses ~835 MB of JSONL every 6 h with no persisted fingerprint; the profile that keeps corpus content off Kiro/Fable is pinned only in a shell script; and the port inverted the upstream design's data-locality intent (local Codex CLI + local Ollama → Bedrock Opus 5 + Titan). The *transaction* shape is worth copying. The *module* is not. | §3, §5, §6 |

Stale line references: track 02 cites `codex.py:186-200` for the lock and `codex.py:471-520` for the retry; at HEAD these are `226-239` and `323-383` (commit `7abada7` inserted ~200 lines above them).

---

## 1. Transaction design — every path checked

`_capture_snapshot` (`src/capture/codex.py:519-659`):

| Path | Lines | Outcome | OK? |
|---|---|---|---|
| No row → INSERT | 540-573 | falls to `connection.commit()` :647 | ✓ |
| Row, no unseen, nothing changed | 576-599 | `connection.rollback()` :591 then `return` inside `with`; `FOR UPDATE` lock released | ✓ |
| Row, no unseen, title/metadata changed | 600-623 | UPDATE, `connection.commit()` :614, `return` | ✓ |
| Row, unseen turns | 624-646 | UPDATE, falls to commit :647 | ✓ |
| Any exception | 648-650 | `rollback()` + re-raise; `get_connection` rolls back again (no-op) and `putconn`s | ✓ |

`_update_task_metadata` (:754-786) and `_store_semantic_result` (:899-1068): single try/commit/except-rollback each; the optimistic `semantic_cursor` check (:922-925) runs inside the locked transaction. ✓

`_last_segment` (:788-814) issues a SELECT with no commit; the connection is returned to the pool idle-in-transaction and psycopg2's `putconn` rolls it back. Harmless, but it is the one read that is *not* inside the write transaction, so `_store_semantic_result` re-validates only the cursor, not the segment (see T-3).

Findings:

**T-1 · S3 · `src/capture/codex.py:323-354`** — `_unprocessed_tail` (raises `RuntimeError` :669 if the cursor is not among the stored turns), `_attempt_key`, `_record_attempt` (a DB write) and the first `_record_abandonment` (:332) run **outside** both `try` blocks in `_run`. Any exception there aborts the whole run with a traceback and no JSON summary; the `TaskFailure` taxonomy that track 02 praised does not cover them. Evidence: the two `try:` blocks are :296-299 and :356-357 only. Fix: wrap the per-task body in one `try` with stage labels `budget`/`semantic`.

**T-2 · S3 · `src/capture/codex.py:947`** — extending-segment UPDATE `WHERE id = %s AND parent_id = %s` never checks `cursor.rowcount`; if the segment row was deleted/reparented between `_last_segment` and the write, the code proceeds and inserts `memory_relationships` pointing at `previous.memory_id` (FK to a missing row raises — but with a generic `ForeignKeyViolation`, not a cursor-changed error). Fix: `if cursor.rowcount != 1: raise RuntimeError("segment changed")`.

**T-3 · S3 · `src/capture/codex.py:818` vs `:922`** — the segment read before the model call and the cursor check after it protect different invariants; the cursor check does not detect a *modified* last segment (e.g. `memory_update` from MCP, dream-cycle status change). Fix: store `(segment_id, updated_at)` in `_StoredSegment` and re-check inside the transaction.

**T-4 · S3 · `src/capture/codex.py:500` vs `:587`** — `captured_at` is preserved on a refresh **without** new turns (:587-589) but overwritten with `now` on a refresh **with** new turns (:626-632 passes `captured_at=now` and never restores it). Two code paths, two meanings. **Verified live**: 4 of 28 parent rows have `metadata.captured_at` = `2026-09-23 23:48:12` (the run that extended them) while `created_at` is 2026-09-22. Fix: always carry `previous["captured_at"]`; add `last_extended_at` if the second meaning is wanted.

**T-5 · S3 · `src/capture/codex.py:352`** — `semantic_retried` is incremented only when `disposition == "unchanged"`; a retry on a task that was also `refreshed` in the same run is not counted. Fix: count `spent > 0` instead.

**T-6 · S3 · `src/capture/codex.py:221-222`** — singleton lock path is `tempfile.gettempdir()/second-brain-codex-capture-<uid>.lock`, i.e. `$TMPDIR`-dependent. On this machine both the manual run (lock file dated Sep 22 12:05 in `/var/folders/.../T/`) and the launchd runs resolve to the same directory (no second lock file exists under `/tmp`), so it works — by coincidence of environment, not by design. Fix: put the lock under the repo `logs/` or `XDG_RUNTIME_DIR`.

Conclusion: the transaction code is correct. It is also ~250 lines of hand-written SQL for three row kinds — the *pattern* to extract is "one connection, `FOR UPDATE` on the parent, optimistic cursor, commit-or-rollback", not the functions.

---

## 2. Turn identity and dedup across rollout rewrites

Identity rules in `fetch_task`:

- **Current format** (`response_item.message role=user` with `internal_chat_message_metadata_passthrough.content_item_kinds == ["user.text"]`): `turn_id = payload["id"]` of the *first* message of the turn (`sources/codex.py:649`, `_authored_text_message` :130-157).
- **Legacy format** (`event_msg.user_message`): `turn_id = client_id`, else `sha256(task_id + timestamp)` (`:586-616`, `_fallback_turn_key` :124).
- When both are pending for one final answer, **legacy wins** (`:503-505`, `:626`).

Live evidence (structure only): across the 32 user-owned rollouts, `event_msg.user_message` occurs **0** times; `response_item message user` occurs 473 times (363 `["user.text"]`, 101 injected-context kinds, 9 with `user.image`). All **313** stored turn IDs are `msg_*` (SQL: shape `msg_id` = 313, `fallback-sha` = 0, attachments = 0).

**D-1 · S2 · `src/capture/sources/codex.py:503-505, 626-635`** — identity is representation-dependent and the *other* representation is preferred. If a future Codex build (or a rollout migration — `state_5.sqlite` has `rollout_migration_state` and `rollout_migration_skipped_rollouts` tables) re-emits historical turns with `user_message` events, every stored `msg_*` turn becomes "unseen" under its new `client_id`, `_capture_snapshot` appends duplicates (:624-632), and the semantic pass re-derives segments and memories for content already stored. `test_current_and_legacy_mirrors_keep_legacy_identity` (`tests/test_codex_capture.py:1109`) *asserts* this preference. The turn-id set difference is sound only while the emitting format is stable. Fix: since the legacy format produces 0 % of production rows, delete it (or make it opt-in) and dedup on **content hash of (prompt_parts, visible_outcome)** as a second key so a rewrite cannot duplicate.

**D-2 · S3 · `agent_tasks.py:107-110`** — `AgentTaskSnapshot` raises `ValueError` on duplicate turn IDs (possible if Codex re-delivers a message across turns). `_run` records it as `source_read/ValueError` **every run forever** with no message text (see S-4); the task is never captured. Fix: dedup by ID in `fetch_task` before constructing the snapshot, log once.

**D-3 · S3 · `src/capture/sources/codex.py:61, :251-254`** — `CodexSourceChangedDuringRead` docstring says the task "must be retried"; nobody retries — `_run` :296-299 records `source_read` and moves on to the next 6-hour run. Harmless because of the idle threshold, but the contract in the docstring is false.

---

## 3. What `fetch_task`'s 79 branches actually handle (live-data audit)

Method: re-implemented the gating logic outside the repo and ran it over the 32 live user rollouts, counting each drop reason. Result reproduces the DB exactly (313 turns), so the counts below are authoritative for this machine.

| Branch group | Lines | Live occurrences | Status |
|---|---|---|---|
| `session_meta` → workspace + git | 527-542 | 32 (27 with `git`) | live |
| `task_started/task_complete/turn_aborted` lifecycle | 544-555 | 370 / 369 / **1** | live |
| Current authored `["user.text"]` message | 557-584 | 363 → 313 turns | **the only producer of stored data** |
| Authored kinds ≠ `["user.text"]` → `unsupported_message_turns` | 569-574 | 9 messages (1× `["user.text","user.image"]`, 8× `["user.text","user.text","user.image","user.text"]`) | live, **silently drops the turn** |
| `active_turn_id` mismatch guard | 577 | 0 (all 363 authored messages follow their `task_started`) | defensive only |
| Legacy `event_msg.user_message` (+ `client_id`, `_fallback_turn_key`, `_local_image_descriptors`) | 586-616, 124-127, 260-291 | **0** | **dead on this machine** |
| `final_answer` pairing, `pending_prompts` preference | 618-635 | 348 finals; 34 without a pending prompt (31 app/automation-initiated turns, 3 second-finals) | live |
| `_sqlite_git_provenance` | 338-364, 511 | `git_*` columns are NULL for all 263 threads | dead-in-practice (always overwritten by `session_meta.git`) |
| `_explicit_codex_project` (parses 2 MB `.codex-global-state.json`) | 293-335, 674 | 23 of 28 tasks have a project | live, but the same data exists in SQLite `projects`/`project_roots` |
| `incomplete_turn_count` provenance | 699-702 | **0** for all 28 stored tasks | misleading (see F-2) |

**F-1 · S2 · `src/capture/sources/codex.py:138, 569-574, 630-631`** — 9 authored prompts containing an image are excluded *together with their final answer*, and multi-part text prompts (`["user.text","user.text"]`) would be excluded too, although the content check at :146-152 already supports multiple `input_text` items. Nothing counts this: not `incomplete_turn_count`, not `CaptureReport`, not a log line. Meanwhile 32 lines of attachment-descriptor code (:260-291) exist for the legacy format that never occurs. Fix: accept any `content_item_kinds` that is all `user.*`; describe `user.image` items as `AttachmentDescriptor(media_kind="image", reference_is_durable=False)` (the type already exists); add `skipped_unsupported_turns` to the report.

**F-2 · S2 · `src/capture/sources/codex.py:699-702` + `codex.py:305-307`** — 16 live turns ended via `task_complete` with no `final_answer` (agent finished in commentary/tool phase). `pending_messages.clear()` at :552-553 erases them *before* EOF, so `incomplete_turn_count` — the only provenance for "a user turn was not captured" — is 0 for every stored task, and the run summary's `incomplete` (2 every run) counts *tasks with zero turns*, a different thing. 25 of 372 authored user messages (6.7 %) are dropped with no observable signal. Fix: count `dropped_no_final`, `dropped_unsupported_kind` per task into provenance and the report.

**F-3 · S3 · `src/capture/sources/codex.py:219-257`** — `_rollout_records` `readlines()`s the entire file and `json.loads` every record (tool outputs, reasoning, 14 835 `item_completed` events…) into a list before yielding. Live user rollouts total **835 MB**, largest **156 MB**; the ~20 eligible tasks are fully re-parsed **every 6 hours** even when the SQLite `updated_at_ms` and the file fingerprint prove nothing changed. The fingerprint `(dev, ino, size, mtime_ns)` is computed (:222-227) and thrown away. Measured run time is dominated by Opus, so this is efficiency and memory risk, not latency, today. Fix: persist the fingerprint in `metadata.source_fingerprint`; skip `fetch_task` when SQLite `updated_at_ms` and fingerprint are unchanged; stream records instead of materialising.

**F-4 · S3 · `src/capture/sources/codex.py:374`** — `state_5.sqlite` hard-coded. Codex bumps the suffix on schema migrations; a missing file surfaces as an uncaught `sqlite3.OperationalError` traceback, not `CodexSourceCompatibilityError`. Fix: glob `state_*.sqlite`, pick the highest, raise the typed error.

**F-5 · S3 · `src/capture/sources/codex.py:373-377, 670, 704`** — `CodexDesktopSource` is stateful: `enumerate_tasks` writes `_observed_at`, `skipped_*`, `_title_provenance`, which `fetch_task` reads back. A `fetch_task` on a ref not produced by the *last* `enumerate_tasks` silently loses title provenance and uses `datetime.now()`. Fix: put the provenance on `AgentTaskRef`, return counts from `enumerate_tasks`.

**F-6 · S3** — SQLite already exposes `has_user_event` (0 for all 18 unknown-ownership threads, also 0 for all 32 user threads — so **not** usable as positive evidence), `first_user_message`, `project_id` (NULL for all user threads today), `projects`, `project_roots`. Ownership classification (`_task_ownership` :100-121) is empirically right (213 delegated = 125 guardian + 88 spawned; 18 unknown = 15 `thread_source NULL` + 2 `onboarding_checklist` + 1 `realtime_voice`, none containing user turns) — no data loss there.

---

## 4. Semantic pass, end to end

Flow per eligible task with a non-empty tail (`codex.py:323-383` → `_process_semantics` :817-843 → `_store_semantic_result`):

1. `_attempt_key` = `contract_id | semantic_cursor | tail[-1].turn_id`; `contract_id` calls `default_resolver()` (TOML load + validation) **per task**.
2. `_record_attempt` **commits** `semantic_attempts{count}` before any model call (crash-safe by design).
3. `_last_segment` (separate connection) → `_BackendSemanticPass.run` builds the payload `{previous_segment:{title,turn_ids,content}, new_turns:[…]}` where `content` is the **verbatim rendered previous segment** and each new turn carries full prompt + full visible answer, and calls `invoker.invoke(system, json, tools=False, effort=spec.effort)` with default `timeout=300`, no `stage`, no `run_id`, no structured-output schema.
4. `_parse_semantic_output` → `_validate_semantic_result` (all-or-nothing coverage of the tail).
5. `services.embed` (Titan, metered) once per segment and per memory — **before** the transaction.
6. `_store_semantic_result` writes segments (children, `#segment-N`), memories (`distilled_agent_task`, parent NULL, **same `source_url` as the task**), `derived_from` relationships, advances the cursor, drops the budget.

What is persisted on partial failure: nothing from steps 3-6 (single transaction) — confirmed by `test_semantic_failure_is_atomic…`. What is re-sent: the **whole tail**, every attempt, plus the previous segment's verbatim content.

Metering reality (contradicts "no tracker exists" in AUDIT §03:G-1 only partly): `DirectAPIInvoker.invoke` (`src/backends/direct_api.py:112-165`) writes a metrics line per call to `logs/llm_metrics/adhoc.jsonl` (because `run_id=None`). I found the 11 semantic calls since the port there: **122 513 input / 46 338 output tokens ≈ $1.77** at $5/$25. The largest single call (the 58-turn backfill) was 78 048 chars in → 31 490 tokens in, 11 236 out, 147 s. `result["usage"]` is indeed discarded by `codex.py:205-211`; the file is the only ledger, it is shared with every ad-hoc call (11 MB, and the running test suite is appending to it), and nothing reads it.

Corpus content logging: **none found.** `_record_failure` keeps only `type(exc).__name__` (:391); `direct_api._parse` logs only char counts (:210-213); `parse_json_output`'s `ValueError` message embeds `raw[:200]` (`base.py:319`) but that exception is swallowed into a type name. The report JSON has task IDs and error types only. ✓

**C-1 · S1 · `src/capture/codex.py:356-375` with `:359-370`** — the attempt budget does not distinguish contract failures (`SemanticResultError`, `ValueError` from the parse backstop) from infrastructure failures (`RuntimeError` from Bedrock throttling/credentials, `TimeoutError` at 300 s on a large tail, Titan embedding errors — `test_semantic_failure_is_atomic…` shows an embedding outage consuming an attempt). Three consecutive 6-hourly runs during an AWS-credential or Bedrock outage abandon the semantic layer of **every** eligible task permanently: the docstring (:9-16) says "never sent to the model again", the test at `tests/test_codex_capture.py:1193` cements it, the exit code returns to 0 (`scripts/capture_codex.py:59`), the liveness check (`verify_liveness.sh:78`) still sees fresh `codex_task` rows, and the only trace is one WARNING line and `semantic_abandoned: N` in a JSON line nobody parses. The verbatim task survives (so this is "hides data", not "loses source"), but the derived layer — the reason the pass exists — is silently gone for those tails. Fix: count only `SemanticResultError`/`ValueError` toward the bound; treat `RuntimeError`/`TimeoutError`/`botocore` errors as transient with no budget cost (or a separate, larger bound); notify on abandonment; provide `--reset-abandoned`.

**C-2 · S1 · `src/capture/codex.py:189-212`, `_attempt_key` :692-702** — cost has no ceiling and grows with the tail. All-or-nothing validation means one unsatisfiable early turn poisons the tail; every *new* turn creates a fresh key (`tail[-1]` moved) and a fresh 3-attempt budget over the **entire** accumulated tail — O(n) tokens per new turn, O(n²) over the task's life. The plist comment estimates "~$0.10/call"; the observed 58-turn call was ≈$0.44. There is no per-run cap although `src/backends/cost.py:168` already provides `SpendTracker`. (Prior audit's 03:G-1 covers "unmetered"; the growth mechanism and the `SpendTracker` seam are new.) Fix: `SEMANTIC_MAX_TURNS_PER_PASS` (the cursor design already supports partial advance); pass `stage="codex_semantic"`, `run_id=<run ts>` so metrics are attributable; feed `result["usage"]` into a `SpendTracker` with a per-run cap.

**C-3 · S2 · `scripts/capture_codex.py` (no profile), `config/backends.toml:33` `default_profile = "laptop"`, `:37` thinker = Kiro** — the rule "the corpus never runs on Kiro/Fable" is enforced only by `scripts/jobs/codex_capture.sh:39` and the plist. `python scripts/capture_codex.py` from a shell (no `SECOND_BRAIN_PROFILE` in `~/.zshrc`/`.zprofile`/`.zshenv`) resolves the Thinker to `kiro/claude-opus-4.8` and ships verbatim Codex turns there with `tools=False`. Nothing in `src/capture/codex.py` names a profile or checks `BackendCapabilities`. Fix: give the pass its own role (`codex_semantic`) pinned in TOML, and have `_BackendSemanticPass` refuse any backend whose family is Kiro/Fable (PLAN-refactor §1 already proposes a role; it should also propose the refusal).

**C-4 · S2 · `src/capture/codex.py:205-211`** — no `timeout` override: default 300 s vs. an observed 147 s for 58 turns; a 100-turn backfill tail would time out, consume an attempt, and (with C-1) be abandoned after three tries — the bigger the task, the likelier the permanent abandonment. Same fix as C-2 (cap turns per pass) plus an explicit timeout scaled to tail size.

**C-5 · S2 · `src/capture/codex.py:1024-1045`, `src/validation.py:19-27`, `src/mcp_server.py:144, 341`** — derived memories are written with `type = 'correction_episode'`, a value absent from `MEMORY_TYPES`/`FILTERABLE_TYPES`. **Live: 16 rows.** `memory_search(type="correction_episode")` and `memory_list(type=…)` raise `ValidationError`, so the one memory kind the whole prompt is engineered to produce is unreachable by type filter through MCP. PLAN-refactor 4e (vocabulary/CHECK constraint) would make this an insert-time failure instead — worse, until the value is added. Fix: add `correction_episode` to `MEMORY_TYPES` (and to `classify.py`'s subset decision) before 4e lands.

**C-6 · S3 · `src/capture/codex.py:181-187, 189-191`** — `default_resolver()` is called in `contract_id` for every task and again in `run` for every model call, each time re-reading/validating the TOML and constructing a new `Resolver`/invoker (new boto3 client). Fix: resolve once per run in `_default_services`.

**C-7 · S3 · `src/capture/codex.py:425, 887-896`** — `_render_turn` emits `Turn ID: msg_…` into the stored segment content that is then **embedded**; opaque IDs are noise in the vector and duplicate `metadata.turn_ids`. The parent task additionally stores every turn a third time in `metadata.turns` and a second time in `content` (the 58-turn parent is 76 KB with `embedding = NULL`). Fix: render IDs only in the model payload; keep `content` on segments and `metadata.turns` on the parent, drop the parent `content` or make it a short manifest.

**C-8 · S3 · `src/capture/codex.py:1027-1029`** — `distilled_agent_task` rows share `source_url` with their parent task (`count = 102, distinct source_url = 22` at query time). Any consumer that keys on `source_url` (e.g. `get_processed_source_urls()` without a `source_type`, `distill_sessions.py:222`'s `rsplit("#")`) sees one URL for many rows. Fix: `codex://<id>#memory-<segment>-<n>`.

Live rows (queried 2026-09-24 12:5x UTC; a run at 05:51 PDT added rows mid-audit, so counts moved from 24→28 parents): `captured_task` 24-28 (embedding NULL, parent NULL), `topic_segment` 104+ (all embedded, all with parent), `task_memory` 102+ (`decision` 42, `insight` 47, `correction_episode` 13-16). **Consistency checks all pass**: every child has a parent; segment `source_url` = `<parent url>#segment-<index>`; `segment_index` contiguous from 0 per parent; no turn ID appears in two segments of one parent; `semantic_cursor` is present in `turns` and equals the last turn for all parents; segment `turn_ids` union = parent `turns` (count equality per parent); every `task_memory` has a `derived_from` relationship to a segment of its own task; all `supporting_turn_ids` ⊆ their segment; all 13 correction episodes match the `Misalignment:` format; `project` column NULL for all 231 rows (deliberate — the test fixture memory literally says so). Zero `semantic_attempts`/`semantic_abandoned` marks exist, i.e. the retry path has never fired in production since `7abada7`.

---

## 5. Port from `origin/main` (`b6d4484`, then `778a9e5`, `7abada7`) — every divergence

`git diff origin/main HEAD --stat` over the ported set: `src/capture/sources/codex.py`, `src/capture/agent_tasks.py`, `scripts/capture_codex.py`, all 7 fixtures, `tests/test_capture_codex_cli.py`, `scripts/jobs/codex_capture_canary.sh`, `scheduling/com.second-brain.codex-canary.plist` — **byte-identical**. Divergent: `src/capture/codex.py` (+207/-0), `tests/test_codex_capture.py` (+199/-0), `scripts/jobs/codex_capture.sh` (+41/-24), `scheduling/com.second-brain.codex-capture.plist`, `tests/test_codex_capture_job.py`.

| Divergence | Introduced/fixed |
|---|---|
| `codex.py` +207: `SEMANTIC_MAX_ATTEMPTS`, `_attempt_key`, `_record_attempt`, `_record_abandonment`, `_update_task_metadata`, `contract_id`, budget copy in `_metadata_for_snapshot`, budget retirement in `_store_semantic_result` (all from `7abada7`) | **Fixed** origin's unbounded retry (origin re-sent a failing tail every run forever). **Introduced** C-1 (infra failures count), T-1's new unguarded DB write (`_record_attempt` outside `try`), C-6's per-task resolver construction. The transaction/lock code is unchanged from origin. |
| `codex_capture.sh`: `SECOND_BRAIN_PROFILE codex_local → controlled`; removed `CODEX_CLI`, `EMBEDDING_PROVIDER=ollama`, `OLLAMA_*` and the Ollama gate; added `AWS_PROFILE`, `CODEX_HOME`, `pg_isready`, `CODEX_HOME` existence check, `"$@"` passthrough, a start line | **Fixed** the silent `exit 0` when Ollama was absent. **Inverted the upstream design**: `origin/main:config/backends.toml:42-48` defines `codex_local` with **every role on `backend = "codex"` (`gpt-5.6-sol`)** and `origin/main:src/embeddings.py:20-21` defaults to **local Ollama `bge-m3`** — i.e. upstream ran the semantic pass on the user's ChatGPT-subscription Codex CLI and embedded locally, so **Codex corpus content never left the machine and cost $0 marginal**. The port moved it to metered Bedrock Opus 5 + Titan. This is a deliberate, documented choice (`PLAN-capture-repair.md:91-97`), but its consequence for PLAN-refactor decision 0.3 is not recorded: `CodexInvoker` (`src/backends/codex.py`, 620 lines) is "reachable only from tests" *because the port removed its only caller*. Deleting it forecloses the zero-cost local path. |
| Plist: hourly → 21 600 s, absolute paths, `controlled`, `AWS_PROFILE`, `CODEX_HOME` | Fixed placeholder paths; fine. |
| `test_codex_capture_job.py`: Ollama-wait test replaced by "runs on Titan, no `OLLAMA` in script" test | Consistent with the script. |
| Canary script + plist **unchanged** | Still `codex_local` + Ollama gate + `/path/to/second-brain`; inconsistent with its sibling (prior audit §7 covers). |

**P-1 · S2** — PLAN-refactor 0.3 should be re-decided with the fact above; the "experimental" label proposed there is the wrong frame — `CodexInvoker` is the upstream-intended Thinker for this very job.

**P-2 · S3 · `docs/PLAN-capture-repair.md:181`** — decision "Codex is `codex_cli_chat`, own-activity" names a `source_type` that appears nowhere in code; the code writes `codex_task` / `distilled_agent_task` (and `config/skim.toml:60-61` lists those). Fix the decision text.

**P-3 · S3 · `docs/CAPTURE-COMPONENTS.md:19, 23`** — still says "not automatic live Codex" / "no native automatic Codex … collector" (prior audit noted; still unfixed at this HEAD). `docs/INTERFACES.md` documents `CODEX_CLI` but not `CODEX_HOME`, `SECOND_BRAIN_CODEX_SEMANTIC_MAX_ATTEMPTS`, or the two Codex `source_type`s.

---

## 6. Tests — what they prove and what they don't

- 20 of the 29 tests in `tests/test_codex_capture.py` — including **every** DB/transaction/retry/refresh/drift test — drive the **legacy** fixtures `real_task.jsonl` (3× `event_msg.user_message`) and `real_correction_task.jsonl` (2×). Only 9 tests use `real_current_task.jsonl`, and exactly one of them (`test_current_capture_stores_source_and_repeated_run_is_unchanged`, :1130) reaches the database. The flow that produces 100 % of production rows has one end-to-end test. **S3** (test-reality drift).
- `_BackendSemanticPass.run` (the only code that touches a paid model) is never exercised; `_SemanticScript` replaces it. No test asserts the resolved backend family, the payload shape, timeout, or that `usage` is recorded. **S2** (rule unenforced — matches prior audit §9 but now with the specific gap named).
- No test for: T-1 crash surface, C-1 infra-vs-contract distinction, F-1/F-2 silent drops (there *is* a test asserting the drop happens — `test_current_unsupported_followup_does_not_capture_partial_prompt` :1156 — but none that it is counted), F-4 missing SQLite, D-2 duplicate-ID task, `CodexSourceChangedDuringRead`.
- `tests/test_codex.py` (742 lines) tests `CodexInvoker` only and is orthogonal to capture; it is the sole live consumer of that backend.

---

## 7. Decomposition of `CodexDesktopSource.fetch_task` (CC 79 → ≤10 per unit)

Extract a pure state machine so the SQL/filesystem/project lookups stop living in the same 210-line method:

```
sources/codex.py
├── _RolloutTurnAssembler            # pure; fed one record at a time
│   ├── feed(record) -> None         # dispatch on (type, payload.type, role, phase)  CC≈6
│   ├── _on_session_meta(payload)    # workspaces + git                              CC≈6
│   ├── _on_turn_event(payload)      # task_started / task_complete / turn_aborted    CC≈5
│   ├── _on_user_item(payload)       # current format; returns Dropped(reason)|None   CC≈8
│   ├── _on_final_answer(payload,ts) # pair + emit AgentTurn                          CC≈7
│   ├── turns: list[AgentTurn]; drops: Counter[str]; last_ts; workspaces; git
│   └── (legacy) _on_user_event(...) # DELETE, or keep behind `legacy=True` with the
│                                    #  fixture converted; 0 % of live data
├── _final_answer_text(payload) -> str | None          # CC≈4
├── _RolloutFingerprint / read_rollout(path) -> Iterator[dict]   # streaming, fingerprint returned
├── _git_provenance(state_path, task_id)               # keep, but call once per run (all NULL live)
├── _project_for(task_id)                              # load .codex-global-state.json ONCE per run
└── CodexDesktopSource.fetch_task(ref)                 # ≈25 lines: guard, assemble, snapshot   CC≈4
```

Rules for the split: (1) the assembler never touches `sqlite3`, `Path.stat`, or `mimetypes`; (2) every `continue` in today's loop becomes a named `drops[reason] += 1` so F-1/F-2 become report fields for free; (3) `AgentTaskRef` carries title provenance so `_title_provenance` and `_observed_at` instance state disappear (F-5); (4) `_rollout_records` returns `(records_iter, fingerprint)` and `_capture_snapshot` persists the fingerprint (F-3); (5) delete `_local_image_descriptors`/`_fallback_turn_key` with the legacy path, re-add image descriptors on the current format's `user.image` items (F-1). Estimated: −180 lines in `sources/codex.py`, MI from 6 to >40, and the module becomes testable without fixtures.

For `src/capture/codex.py`: split into `codex/run.py` (orchestration, ≈120 lines), `codex/store.py` (the three transactional writers behind one `TaskStore` with `connect` injected — this is the piece to lift into the capture framework), `codex/semantic.py` (`_BackendSemanticPass`, prompt, parse/validate, budget), and let `_run` take `services` from a single `resolve_services()` that builds the resolver once (C-6) and pins the role (C-3).

---

## 8. Finding index

| ID | Sev | Location | One line |
|---|---|---|---|
| C-1 | S1 | `codex.py:356-375` | infra failures consume the attempt budget → permanent silent abandonment after an 18 h outage, exit 0 |
| C-2 | S1 | `codex.py:189-212, 692-702` | no cost ceiling; all-or-nothing tail re-sent per attempt, O(n²) over a task; `SpendTracker` exists unused |
| C-3 | S2 | `scripts/capture_codex.py`, `backends.toml:33-37` | no-Kiro/Fable rule pinned only in the shell wrapper; direct invocation → Kiro |
| C-4 | S2 | `codex.py:205-211` | default 300 s timeout vs 147 s observed at 58 turns; size → timeout → abandonment |
| C-5 | S2 | `codex.py:1024-1045`, `validation.py:19-27` | `correction_episode` (16 live rows) not in `MEMORY_TYPES`; unfilterable via MCP |
| F-1 | S2 | `sources/codex.py:138, 569-574` | image-bearing / multi-part authored prompts dropped with their answers, uncounted (9 live) |
| F-2 | S2 | `sources/codex.py:552-553, 699-702` | turns ending without `final_answer` cleared before EOF; `incomplete_turn_count` always 0 (16 live) |
| D-1 | S2 | `sources/codex.py:503-505, 626` | identity depends on emitting format; legacy preferred; format change ⇒ duplicated turns/segments/memories |
| P-1 | S2 | `PLAN-refactor.md:33`, origin `backends.toml:42-48` | port replaced the local `codex_local`+Ollama design with metered Bedrock; 0.3 must weigh that |
| §6 | S2 | `tests/test_codex_capture.py` | model-touching code untested; DB flow tested almost only on a dead format |
| T-1 | S3 | `codex.py:323-354` | budget bookkeeping outside `try`; one bad row aborts the run |
| T-2 | S3 | `codex.py:947` | extend-segment UPDATE rowcount unchecked |
| T-3 | S3 | `codex.py:818` vs `:922` | segment read outside the write transaction; only cursor re-checked |
| T-4 | S3 | `codex.py:500` vs `:587` | `captured_at` preserved on one refresh path, overwritten on the other (4 live rows) |
| T-5 | S3 | `codex.py:352` | `semantic_retried` undercounts |
| T-6 | S3 | `codex.py:221` | `$TMPDIR`-dependent singleton lock |
| D-2 | S3 | `agent_tasks.py:107-110` | duplicate message ID ⇒ task fails every run with only a type name |
| D-3 | S3 | `sources/codex.py:61` | "must be retried" — nothing retries |
| F-3 | S3 | `sources/codex.py:219-257` | 835 MB re-parsed every 6 h; fingerprint computed and discarded |
| F-4 | S3 | `sources/codex.py:374` | `state_5.sqlite` hard-coded; missing file is an untyped traceback |
| F-5 | S3 | `sources/codex.py:373-377, 670, 704` | stateful adapter; enumerate→fetch hidden coupling |
| F-6 | S3 | `sources/codex.py:338-364, 293-335` | git provenance from SQLite always NULL live; project read from 2 MB JSON per task |
| C-6 | S3 | `codex.py:181-191` | resolver rebuilt per task and per call |
| C-7 | S3 | `codex.py:425, 887` | turn IDs embedded; turns stored three times |
| C-8 | S3 | `codex.py:1027-1029` | derived memories share the parent's `source_url` |
| P-2 | S3 | `PLAN-capture-repair.md:181` | decision names `codex_cli_chat`, code writes `codex_task` |
| P-3 | S3 | `CAPTURE-COMPONENTS.md:19,23`, `INTERFACES.md` | Codex collector denied; env vars/source types undocumented |

Totals: **S1 2 · S2 8 · S3 17** (27).

---

## Could not verify

- Whether Codex Desktop's rollout migrations (`rollout_migration_state`, 1 row) ever rewrite message `id`s — D-1 is a mechanism, not an observed event.
- Whether launchd's `TMPDIR` will always equal the interactive one (T-6): inferred from the single lock file at `/var/folders/.../T/` and none under `/tmp`.
- Real Bedrock list prices; the $1.77 figure uses $5/$25 per 1M and the 11 metrics lines in `adhoc.jsonl` (which the concurrently running test suite is also appending to).
- Where the 22:02 UTC Sep 22 Opus call came from: the launchd run at 15:01 PDT reported `semantic_processed: 0`, so that call was a separate (manual) process — consistent with the lock file's 12:05 timestamp but not provable.
- What the 31 "final answer without a user prompt" turns are (app/automation-initiated is my inference from the `additional_content.codex_apps_*` metadata kinds and the `~/.codex/automations` directory).
- No pytest run, no capture script executed, no model invoked, no git write. One deviation to disclose: I ran `launchctl print gui/<uid>/com.second-brain.codex-capture` once (read-only, to read the job's environment) before recalling that `launchctl` was excluded outright; nothing was started, stopped, or modified.
