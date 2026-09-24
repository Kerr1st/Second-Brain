# Track 04 — Express, briefing, digests and the feedback loop (delivery half)

**Scope:** `src/express.py`, `scripts/express_push.py`, `scripts/brief.py`, `src/dream_cycle/feedback.py`, `src/dream_cycle/digest.py`, `scripts/weekly_digest.py`, `migrations/010_express_feedback.sql`, `tests/test_express.py`, `tests/test_weekly_digest_backend.py`, `memory_brief` / `memory_learn` in `src/mcp_server.py`, plus the job wrapper that chains them (`scripts/jobs/dream_cycle_scheduled.sh`, `weekly_digest.sh`). HEAD `6b8c2d2`. Read-only: code read in place, `logs/` read, `memory_bank` queried with SELECT only, Kiro session transcripts grepped for tool-call records. No script, model, job or test was executed. Cyclomatic numbers were recomputed with radon (`uvx --from radon`).

**Severity key:** S1 corrupts/hides data or spends unmetered · S2 stated rule unenforced or failure invisible · S3 duplication / dead weight / drift.

**Counts:** S1 ×4 · S2 ×11 · S3 ×14 (29 findings). Prior-audit claims checked: 7 confirmed with new data, 1 refuted (§F11), 3 extended.

---

## 0. The answer in one paragraph

The delivery half is built, tested in isolation, and **not delivering**. In 110 days since Express was declared built (2026-06-06) it has sent zero emails (no credentials anywhere; `logs/.express_last_push` does not exist), the on-demand `brief` CLI has never been run (`~/.zsh_history`: 0 hits), `memory_brief` was called three times (09-16, 09-17 ×2) and returned a byte-identical lead each time, and every scheduled weekly digest on record failed (4/4 logs, Kiro timeout). Meanwhile the parts that *do* run are actively harmful: the scheduled push composes a briefing from the corpus and hands it to Kiro — whose server-selected model the 09-23 log names as `claude-fable-5.1` — three times for ~2.5 minutes each, then throws the result away because email is unconfigured (§F1). The two signals the whole design is gated on are broken at the source: every `contradicts` edge the dream cycle produced in September was expired within hours by the dream cycle's own post-run step, so Express can only ever show June's contradictions (§F2); and "cross-project" is computed from `memories.project`, 99.85 % of whose values are chat-session UUIDs, so all three "cross-project" pushes were really "two different chat sessions" (§F3). The digest file, the only artefact every run produces, states "No feedback was injected" in 16 of 16 files while 16 runs carried non-empty feedback (§F5).

---

## 1. How an ACCEPTED insight reaches the user (question 1)

Traced from `orchestrator.py:522-590` outward. Five paths exist on paper; here is each one's actual state.

| Path | Wired | Scheduled | Exercised | Evidence |
|---|---|---|---|---|
| **A. Memory row** — `store_accepted` writes a `memories` row tagged `dream-cycle` | yes | every run | yes | 88 rows tagged `dream-cycle`, 83 active; 17 in the last 14 days. Reachable only by pull (`memory_search`, `memory_brief`). |
| **B. Digest file** — `generate_digest` → `logs/dream-cycle-digest-{UTC date}.md` | yes | every run | written, never delivered | 16 files. Nobody reads them to the user; `dream_cycle_runs.digest` stores the *path* (`digest.py:179`). Same-day runs overwrite (§F13). Not a memory, not searchable. |
| **C. `memory_brief` MCP tool** — `compose_briefing` → deterministic edit → markdown | yes | on demand | 3 calls ever | `~/.kiro/sessions/cli/*.jsonl`: 3 tool-call records (09-16, 09-17, 09-17), all `{"window_days":14}`, all returning the same lead (a June contradiction). Not in `autoApprove` (`~/.kiro/settings/mcp.json:38-43`), so each call needs a click. |
| **D. `brief` CLI** — `scripts/brief.py` | yes | never | 0 runs | `~/.zsh_history` (2,186 lines, 220 mentioning second-brain): 0 hits for `brief.py`. |
| **E. Express push email** — `express_push.py` chained in `dream_cycle_scheduled.sh:44` | half | Mon/Wed/Fri 02:00 + every manual run | composes, never sends | 4 "Push triggered" lines in `logs/dream-cycle-2026092{1,3}.log`, `dream-cycle-20260918.log`; every one followed by `Email not configured yet`. No `EXPRESS_EMAIL_*`/`GMAIL_APP_PASSWORD` in shell env, in `.env` (only `CAPTURE_API_TOKEN`), in the dream-cycle plist (no `EnvironmentVariables`), or in `~/.config/second-brain/express.env` (directory does not exist); `dream_cycle_scheduled.sh` has no source line. The June-13 activation runbook (`docs/archive/2026-09-10/docs/EXPRESS-PLAN.md:109-150`) was never executed. |
| **F. Weekly digest** — `weekly_digest.py` → `synthesis` memory + `osascript` toast | yes | Mon 06:00 | 0 scheduled successes | §F11. |
| **G. `session_start` briefing** — `should_run_briefing()` (`dream_cycle_db.py:299`) | scaffold only | never | 0 runs | `dream_cycle_runs.run_type`: scheduled 33, user_triggered 21, session_start 0. EXPRESS-PLAN called this "the dormant scaffolding to finally wire"; `memory_brief` does not use it (no frequency gate — hence three identical briefings in two days). |

**Net:** the only working delivery is *pull*: an agent calling `memory_brief`/`memory_search`. Every *push* path (B, E, F, G) is either unread, unconfigured, failing, or dead. The EXPRESS-PLAN success criterion "accepted insights reach you **without you querying**" is unmet.

---

## 2. Is the feedback loop closed? (question 2)

There are three loops that share the word "feedback". Measured state:

| Loop | Writer | Reader | Live rows | State |
|---|---|---|---|---|
| Evaluator rejections → Explorer prompt | `complete_run(explorer_feedback_injected=…)` | `feedback.build_feedback_injection` → `get_recent_rejections`/`get_accepted_dissents` | 16 of 51 completed runs have non-empty text (0 → 82,843 chars) | **Closed**, but the digest denies it (§F5), the window is diluted by aborted runs (§F6), and nothing measures whether it changed Explorer behaviour. |
| User rejections → Explorer prompt | `dream_cycle_db.mark_user_rejected` | `get_user_rejections` → feedback Part 3 ("the strongest signal there is; never trimmed", `feedback.py:100`) | **0 / 170** candidates have `user_rejected_at`; **0** have a reason | **Open.** `mark_user_rejected` has zero non-test callers (`grep -rn mark_user_rejected src scripts` → definition only). No MCP tool, no `brief` flag. Part 3 has never executed in production. `ARCHITECTURE.md:105` already says so; confirmed. |
| Delivery preferences (`express_feedback`) → briefing ranking | `brief --useful/--less/--mute` | `load_prefs`/`_apply_prefs` | **0 rows** | **Open**, and by design it never reaches the dream cycle (migration 010 header: "not what the dream cycle synthesizes"). No MCP tool exists; the footer's own instructions mis-classify (§F7). |
| Relationship graph (`expired_at`) | `expire_stale_relationships` (step 9) | `_gather_contradictions`, `memory_graph` | 22,461 / 23,216 expired; **22,263 (99.1 %) expired within 7 days of creation** | Not a feedback loop at all — a shredder (§F2). |

`dream_cycle_candidates.user_rejected / feedback_injected` counts, as asked: `user_rejected_at IS NOT NULL` = 0; `user_rejection_reason IS NOT NULL` = 0; `explorer_feedback_injected` non-null = 22, non-empty = 16, of 51 completed runs; 29 completed runs have `candidates_generated = 0`.

---

## 3. Where express.py chooses a model or spends (question 3)

Exhaustive read of the 734 lines:

| Line | What | Model choice | Metered | Notes |
|---|---|---|---|---|
| `express.py:481-483` | `from src.agent_invoker import AgentInvoker; invoker = AgentInvoker()` | `KiroInvoker` (alias, `agent_invoker.py:20`) with `DEFAULT_MODEL = "claude-opus-4.8"` (`kiro.py:43`) — **hardcoded; ignores `SECOND_BRAIN_PROFILE`, bypasses `default_resolver`** | **No.** No `SpendTracker`, no `stage`, no `run_id` → records land in `logs/llm_metrics/adhoc.jsonl` with `stage: null, run_id: null` | Confirmed. This is the only model call in the module. |
| `express.py:484` | `invoker.invoke(_EDITOR_SYSTEM, _editor_user_message(...), timeout=180)` | as above; `tools` defaults False (no MCP) | no | Observed latencies 165 s, 159 s, 121 s — within 15–60 s of the timeout. |
| `express.py:494-496` | `except Exception` → WARNING → deterministic fallback | — | — | A routing/PII violation is indistinguishable from "kiro-cli not installed". |
| `express.py:694-728` | `send_email` — `smtplib.SMTP("smtp.gmail.com", 587)` | n/a (network, not model) | n/a | Never reached in production. |
| `_gather_*`, `render_*`, `should_push` | SQL / string building only | none | — | No embeddings are generated anywhere in the module. |

Callers that reach line 481 with `invoker=None`: `brief.py:107` (default, unless `--no-llm`), `express_push.py:73` (default, unless `--no-llm` — **the scheduled job passes neither**), `mcp_server.py:506` (`memory_brief(use_llm=True)`).

---

## 4. Actual delivery state (question 4)

- **Env / secrets (names only):** `.env` contains exactly one key, `CAPTURE_API_TOKEN`. `EXPRESS_EMAIL_TO`, `EXPRESS_EMAIL_FROM`, `GMAIL_APP_PASSWORD` are absent from the interactive shell, `.env`, every plist in `scheduling/`, `job_wrapper.sh`, `dream_cycle_scheduled.sh`, and the runbook's `~/.config/second-brain/express.env` (directory absent). `email_configured()` (`express.py:566-568`) is therefore False in every context that has ever run. State file `logs/.express_last_push`: does not exist → zero sends ever; the `--force`/re-push guard has never had anything to guard.
- **Plist:** there is no Express plist. `express_push.py` runs inside `dream_cycle_scheduled.sh:44` as `"$PY" .../express_push.py || true`, i.e. Mon/Wed/Fri 02:00 (`com.second-brain.dream-cycle.plist`, symlinked and installed) plus every manual dream cycle (7 express runs on 09-17 alone). The `|| true` is documented ("never mask the dream cycle's exit"), but it also means a broken delivery layer can never notify.
- **Ordering inside `express_push.main`** (`express_push.py:66-84`): `should_push` → `compose_briefing` → **`edit_briefing` (LLM)** → `render_email` → `--dry-run` check → **`email_configured()` check** → send. The LLM pass runs *before* the two gates that have short-circuited every real run.
- **Weekly digest:** plist Mon 06:00 → `job_wrapper.sh weekly_digest.sh`; `weekly_digest.sh` exports `SECOND_BRAIN_PROFILE=controlled`, `AWS_PROFILE=second-brain-jobs`, `SECOND_BRAIN_DIGEST_CAP_USD=0.25`. Scheduled history: see §F11.
- **MCP:** `second-brain` server registered via `scripts/jobs/mcp_serve.sh` (sets only `AWS_PROFILE`); `memory_brief` and `memory_learn` are **not** auto-approved.

---

## 5. Findings

### S1

**F1 — The scheduled Express editor sends corpus-derived content to Kiro's server-selected model (Fable 5.1), unmetered, and discards the result.**
`express.py:481-484` (above). Chained from `dream_cycle_scheduled.sh:44` *after* the script exports `SECOND_BRAIN_PROFILE=controlled` (`:33`), which Express ignores. Evidence, `logs/dream-cycle-*.log`:
- 09-18 02:20:40 → 02:23:25 `kiro backend did not honor model 'claude-opus-4.8' (server-selected model used)` → `Email not configured yet`
- 09-18 08:10:24 → 08:13:04 same
- 09-21 08:34:46 → 08:36:47 same
- 09-23 02:46:15 → 02:46:40 `Agent exited with code 1: [warn] failed to set model 'claude-opus-4.8': Method not found … The model 'claude-fable-5.1' is not available` → deterministic fallback.
`logs/llm_metrics/adhoc.jsonl` has the four matching records (`model: claude-opus-4.8, stage: null, run_id: null, success: true/true/true/false`, latency 165/159/121/25 s), no token counts. The user message is built from `title`, `detail` (600 chars of memory `content`) and `source_titles` of dream-cycle insights, contradictions and resurfaced memories (`express.py:421-440`) — corpus content by any definition. Three of three successful calls were wasted because `email_configured()` was checked afterwards. Prior audit M-5 confirmed; new: it has actually fired in production, it fired under `controlled`, and the served model is named in the log.
*Fix:* (a) in `express_push.main`, check `email_configured()` (and `--dry-run`) **before** `edit_briefing`; (b) resolve the editor through `default_resolver().invoker_for(SKIM_ROLE)` with a `SpendTracker`, copying `weekly_digest.resolve_digest_backend`; (c) narrow the `except` at `:494` so `DigestBackendError`-class refusals propagate; (d) add a test asserting no `KiroInvoker(`/`AgentInvoker(` in `express.py` (mirror `test_no_direct_kiro_invoker_remains`).

**F2 — The dream cycle's own post-run step expires every contradiction it just found, so Express structurally cannot surface a recent contradiction.**
`dream_cycle_db.expire_stale_relationships:835-861` expires any edge where *neither endpoint has `last_accessed_at` within 90 days* — with **no floor on `mr.created_at`**. `last_accessed_at` is written only by `search.increment_access_count` (`search.py:344`), i.e. only when a memory appears in `memory_search` results. Freshly created insights and freshly captured sources have `last_accessed_at IS NULL`, so their edges qualify immediately. `orchestrator.py:566` runs this every run. Measured:

```
contradicts edges created Sept 2026:  5   live: 0
  09-17 05:03 → expired 09-17 05:40   (0.6 h)
  09-18 05:01/05:07/05:36 → 09-20 18:23 (~61 h)
  09-23 02:28 → expired 09-23 02:46   (0.3 h — same run, step 9)
all expired edges: 22,461; expired < 7 days after creation: 22,263 (99.1 %)
live contradicts edges: 11, newest created 2026-06-17
Sept related_to: 1,888 created / 695 live; derived_from: 113 / 34
```

`_gather_contradictions` (`express.py:229-268`) filters `expired_at IS NULL`, so the ⚠️ Contradiction kind — priority 0 in `_KIND_PRIORITY`, "the most valuable" in `_EDITOR_SYSTEM` — is fed exclusively by June edges kept alive because someone once searched those memories. On 09-23 `should_push` fired on a contradiction (it reads `candidate_json`, `express.py:608-611`) at 02:46:15; the corresponding edge had been expired at 02:46 — the email body could not have contained the item that triggered it. `ARCHITECTURE.md:99` calls this divergence possible; the data makes it certain for every contradiction.
*Fix:* add `AND mr.created_at < now() - make_interval(days => %s)` to the expiry predicate (and probably exclude `contradicts`/`supersedes` from decay altogether); un-expire the 5 September `contradicts` edges; make `should_push` and `_gather_contradictions` read the same source (candidate relationships for the run, not the decayed graph).

**F3 — The "cross-project" trigger is computed from chat-session UUIDs.**
`memories.project` live: 2,679 non-empty values, **2,675 match `^[0-9a-f-]{36}$`**, all `kiro_cli_chat`; 4 named values across 2 projects. Dream-cycle insight memories themselves have `project` NULL (0/17 in window). `express.py:200-201` (`len(projects) >= 2`) and `express.py:614-616` (`_distinct_projects(...) >= 2`) therefore mean "two source memories came from two different CLI sessions". All three `cross_project` push triggers (09-18 ×2, 09-21) were UUID pairs; their `strategy_that_found_it` was `stale_synthesis_check` / `elaborative_reinterrogation`, not `cross_project_collision`. `weekly_digest.py:198` already knows this and filters `project !~ '^[0-9a-f-]{36}$'`; Express does not. Consequence: the "rare + high-signal" bar (locked decision #6) is effectively "any synthesis with ≥2 sources", and `topics` for insights (`express.py:222`) become UUID lists, so `brief --less <topic>` can never target an insight.
*Fix:* one `is_project_label()` predicate (or fix the writer — `normalize_project_tag` taking the final path component of a session directory; root cause not verified here) used by Express, weekly digest and the Explorer; count `cross_project` only on `strategy == cross_project_collision` or on ≥2 *named* projects.

**F4 — `should_push` treats a zero-stats aborted run as "the latest completed run" and would have emailed about it.**
`express.py:595-604` selects `ORDER BY completed_at DESC LIMIT 1` with no check on stats/digest. Run `3a1263a6` (09-18 07:32→08:10): `candidates_accepted = 0`, `digest = ''`, wrapper line `FAILED (exit 2)` — yet 4 `ACCEPTED` + 2 `REJECTED` candidate rows are committed, and `logs/dream-cycle-20260918.log:199` shows `Push triggered for run 3a1263a6`. A second such run exists (`bfe245b1`: column 0, 3 accepted committed). Extends prior-audit §2 (zeroed stats) with the delivery consequence: the user would be emailed connections from a run the system records as having done nothing and for which no digest exists.
*Fix:* `should_push` requires `candidates_generated > 0 AND digest <> ''` (or a `status` column); better, have the orchestrator hand `express_push` the run id and its accepted candidates instead of re-deriving "latest".

### S2

**F5 — Every digest says "No feedback was injected"; 16 runs injected feedback.**
`digest.py:57-62` calls `was_feedback_injected(run_id)`, which reads `dream_cycle_runs.explorer_feedback_injected` (`dream_cycle_db.py:754-772`). That column is written only by `complete_run` (`dream_cycle_db.py:69`), which the orchestrator calls at `:578` — **after** `generate_digest` at `:563`. The read is always NULL at digest time. Evidence: `grep -l "No feedback was injected" logs/dream-cycle-digest-*.md` → 16/16; `grep -l "Feedback from previous cycles"` → 0; DB: 16 runs with non-empty text, e.g. `6a4cf5ed` 82,843 chars whose digest (`dream-cycle-digest-2026-09-17.md`) says none. Every digest test mocks `was_feedback_injected.return_value = False` (`test_digest_properties.py:114,152,192`, `test_dream_cycle.py:951,1011,1045,1080`), so the suite cannot see it. The prior audit flagged the silent `except: pass` at `:61`; the real cause is ordering — the call succeeds and returns the wrong answer.
*Fix:* pass `feedback_injected=bool(feedback)` into `generate_digest` from the orchestrator (the text is in scope at `:366`); delete `was_feedback_injected`.

**F6 — The feedback window counts aborted, candidate-less runs as "cycles".**
`get_recent_rejections` / `get_user_rejections` / `get_accepted_dissents` (`dream_cycle_db.py:176-181, 210-215, ~250`) define a cycle as any run with `completed_at IS NOT NULL`, `LIMIT n`. 29 of 51 completed runs have 0 candidates. With `FEEDBACK_CYCLES = 2` (`feedback.py:19`), run `47fffa94` (09-17 20:09) received **0 chars** of feedback although `6a4cf5ed` (7 rejections) completed 14 h earlier — its two "most recent cycles" were `8ee769f0` and `eecec9e2`, both operator-aborted. The feedback lesson block the docstring describes as essential (`feedback.py:30-36`) silently vanishes after any abort.
*Fix:* the subquery becomes `WHERE completed_at IS NOT NULL AND candidates_generated > 0`; hoist it into one `recent_completed_run_ids(n)` helper (it is copied verbatim three times).

**F7 — The user-facing feedback controls cannot be reached from where the user is, and the instructions on screen produce the wrong record.**
(a) `render_markdown` footer (`express.py:556-557`) tells the user `brief --useful <#id>`; `_classify_target("#abcd1234")` → `core = "#abcd1234"`, fails the hex test, returns `("topic", "#abcd1234")` — a topic preference that matches nothing. `test_classify_target` (`test_express.py:344-350`) does not cover `#`. (b) `memory_brief` renders the same footer inside an MCP session where no `brief` binary and no feedback tool exist. (c) `render_email` (`express.py:638-691`) shows neither item ids nor the footer, so an emailed item can never be fed back on. (d) `_pref_hint` (`express.py:148-158`) passes only kind/topic weights to the LLM; item-level `useful`/`less` affect only the deterministic path. Net: `express_feedback` has 0 rows after 110 days and, given (a)–(c), would have stayed empty even with an engaged user.
*Fix:* strip a leading `#` in `_classify_target`; add `memory_feedback(target, signal)` and `memory_reject_insight(candidate_id, reason)` MCP tools (the latter finally giving `mark_user_rejected` a caller); include ids in the email; feed item weights to the editor too.

**F8 — "No repeats within ~14 days" is never enforced; the briefing has no notion of "already shown".**
`express.py:290-291` filters on `metadata->>'express_last_surfaced'`; **no code writes that key** (`grep -rn express_last_surfaced src scripts` → only the two read lines; DB: 0 rows carry it). `test_compose_resurface_suppresses_recently_surfaced` (`test_express.py:199-211`) pre-seeds the key by hand, so it passes vacuously. Nothing in `brief`, `memory_brief`, `express_push` or `memory_read` bumps `last_accessed_at` either (only `memory_search` does, `search.py:344`). `_gather_resurfaced`'s ORDER BY is deterministic, so the same three memories (`b5e1341a…` 26 accesses/last seen 05-30, `739da125…`, `28c86aad…`) are the "worth revisiting" set on every call, and the three `memory_brief` calls on record returned the identical lead. Locked decision #10 ("resurfacing won't repeat an item within ~14 days") is unimplemented.
*Fix:* after rendering, `UPDATE memories SET metadata = metadata || '{"express_last_surfaced": now}'` for surfaced ids (or a small `express_deliveries(item_id, surfaced_at, channel)` table that also gives the "immutable delivery ledger" `ARCHITECTURE.md:99` says is missing and replaces `logs/.express_last_push`).

**F9 — An intentional empty editor selection is overridden with padding.**
`express.py:491-493`: `if ordered: ranked = {...}`; an LLM reply of `{"items": []}` ("nothing worth the user's attention") leaves `ranked is None` and falls to `_deterministic_edit` — the top 5 by kind priority. The system prompt says "fewer is better than padding" (`:405`). `ARCHITECTURE.md:99` notes it; confirmed at the line.
*Fix:* distinguish parse failure from an empty, well-formed selection; return `editor: "llm-empty"` and render the quiet message.

**F10 — Failures of the delivery layer are invisible by construction.**
`dream_cycle_scheduled.sh:44` `|| true`; inside `express_push.main` only `RuntimeError` from `send_email` maps to exit 1 (`:86-89`) — a `psycopg2` error in `should_push`, a UUID cast failure in `_distinct_projects` (`express.py:577`, `ANY(%s::uuid[])` over model-emitted ids; data currently clean, 0/652 non-UUID) or a broken `render_email` is an unhandled traceback masked by `|| true`, no `osascript`, no liveness signal. The 09-18 08:13 run shows the wrapper printing `FAILED (exit 2)` *after* express had already run and been ignored. Also stale: `express_push.py:4` and the archived plan say "chained after the **noon** dream cycle"; the schedule has been 02:00 Mon/Wed/Fri since the plist was rewritten.
*Fix:* give express_push its own exit contract (`2` compose failure, `3` send failure) and let the wrapper record it separately from `dc_status` (e.g. write `logs/last_express_status.json` like `last_ingest_status.json`).

**F11 — Weekly digest: 0 scheduled successes on record; a Monday failure loses the week; one failure mode still exits 0. Prior-audit 3e refuted.**
`logs/weekly-digest-2026{0824,0831,0907,0921}.log`: all four end `TimeoutError: Agent subprocess timed out after 300s/600s` → `FAILED (exit 1)`; no log for 09-14. `memories WHERE source_type='weekly_digest'`: 6 rows — W23–W26 (June) then **W38 (created 09-16) and W39 (created 09-23)**, both without a matching job log, i.e. manual runs; **W27–W37, eleven weeks, have no digest**. Because the idempotency key is the ISO week (`weekly_digest.py:209-212`, `digest://{Y}-W{w}`), a failed Monday is never retried and a Tuesday manual run is the only recovery. The post-`e998f3b` code has not yet been exercised by launchd (next: Mon 09-28). Residual defect: `weekly_digest.py:244-246` — LLM returns no `summary` → `log.error` → `return None` → `main()` returns **0**, contradicting the comment at `:274` "Every failure mode below is loud and non-zero"; `test_missing_summary_writes_nothing` pins the silent path. **Refuted:** prior audit 05-ops 3e claimed exit codes 3 and 4 "do not exist"; at HEAD `main()` returns 2/3/4 at `:280/:283/:286` and `sys.exit(main())` propagates them (`test_main_exits_non_zero_on_cap/_refusal`, `test_timeout_is_a_loud_non_zero_exit` pin them). `weekly_digest.sh:24` is accurate.
*Fix:* return non-zero on empty summary; key idempotency on week **and** success (or allow `--week` re-runs); have `verify_liveness.sh` check `max(created_at)` of `weekly_digest` memories, not the job's exit code.

**F12 — `memory_brief(use_llm=True)` spawns kiro-cli from inside the MCP server.**
`mcp_server.py:501-506`: `invoker=None` → `express.py:481` → `KiroInvoker` subprocess with corpus content, from a process that is itself a Kiro MCP child, with the same routing bypass as F1 and no `try/except ValidationError` (prior audit, confirmed). `use_llm` should route through the resolver or be removed.

**F13 — Digest files collide and stray.**
`digest.py:172-176` writes `dream-cycle-digest-{UTC date}.md`. DB: runs `47fffa94` (09-17 20:09 local = 09-18 UTC) and `fc0e529d` (09-18 02:00) both record `.../dream-cycle-digest-2026-09-18.md`; the first (12 rejected, useful for feedback review) is gone. Run `05171b48` recorded `/Users/…/second-brain-worktrees/skim-stage/logs/…` — the path is `__file__`-relative, so a worktree run writes outside the repo's `logs/`. Confirms prior R10 with data.
*Fix:* `dream-cycle-digest-{date}-{run_id[:8]}.md`; resolve `logs/` from one `paths.py`.

**F14 — Trigger and content are computed from different stores.**
`should_push` reads `dream_cycle_candidates.candidate_json` (`relationships[].relation_type`, `strategy_that_found_it`, `source_memories`); `compose_briefing` reads `memories`/`memory_relationships` (`metadata.strategy`, `metadata.source_memories`, live edges). Two key vocabularies for the same facts (`strategy` vs `strategy_that_found_it`), two project resolvers (`_gather_insights:181-189` batch vs `_distinct_projects:571-581`), and — via F2 — a guaranteed mismatch for contradictions. `ARCHITECTURE.md:99` describes the risk; this is the mechanism.
*Fix:* one `classify_insight(candidate_or_memory) -> {cross_project, contradiction, projects}` used by both; have the push read the run's accepted memories, not the candidate JSON.

**F15 — The "value" and "seen" signals of the entire subsystem reduce to `memory_search` hits.**
`access_count`/`last_accessed_at` drive resurfacing (`express.py:280-294`), relationship survival (F2) and rerank; they are incremented only at `mcp_server.py:160`. Being surfaced in a briefing, read via `memory_read`, emailed, or traversed via `memory_graph` counts for nothing. The subsystem cannot learn from its own deliveries because it does not record them.

### S3

**F16 — Three identical sentinel invokers** whose only job is to raise: `brief._Deterministic:47-51`, `express_push._DeterministicInvoker:54-56`, `mcp_server.memory_brief._NoLLM:500-502` (+ `MagicMock(side_effect=RuntimeError)` in tests). Replace with `edit_briefing(briefing, use_llm: bool = True, invoker=None)`.

**F17 — Nine truncation policies in four modules:** `_truncate` 600/400 with `…` (`express.py:160-162, 439`), headline `[:120]` vs prompt "≤ 90 chars" (`:490` vs `:412`), subject `[:107]+"…"` (`:654`), `_first_sentence` 220 (`feedback.py:131-140`), `_trim_to_budget` `"(trimmed)"` (`:118-129`), `_candidate_title` `[:80]` (`:154-156`), weekly title `[:200]` (`weekly_digest.py:242`), toast `[:180]` (`:262`), `memory_learn` `[:300]` (`mcp_server.py:451`). One `clip(text, n, marker)` in a shared module.

**F18 — Two evaluator-role vocabularies for the same four seats:** `feedback.py:50-55` (`evaluator_b → "User Advocate"`) vs `dream_cycle_db.get_evaluator_verdicts_for_run:38-41` (`"advocate"`) + the tuple `("skeptic","advocate","epistemologist","methodologist")` written three times in `digest.py:139,148,153` and rendered with `.capitalize()`. `digest.py:145-159` also re-derives 3-of-4 consensus and the dissenter by hand although `consensus.tally_consensus` exists; a missing verdict (`N/A`) is reported as a dissent. One `EVALUATOR_SEATS` constant with `(column_prefix, key, display_name)`.

**F19 — Cross-project / contradiction rules implemented twice** (`express.py:196-201` vs `:608-616`) with different keys and sources — see F14.

**F20 — Eight "recent" windows, mixed units, no shared home:** insight 14 d, resurface age 30 d, suppress 14 d (`express.py:32-36`); feedback 2 *runs* (`feedback.py:19`) while the DB layer defaults to 3 (`dream_cycle_db.py:147,191,232`); weekly 7 d; expiry 90 d; `should_run_briefing` 24 h; `memory_recent_activity` 7 d. Days vs runs is the drift that produced F6.

**F21 — Two things called "digest", two called "feedback".** Dream-cycle digest = a markdown *file* in `logs/` (`digest.py`), never a memory; weekly digest = a `synthesis` *memory* (`weekly_digest.py`), never a file; `express` kind `digest` means only the latter; `dream_cycle_runs.digest` holds a path (`003:21` says markdown). `src/dream_cycle/feedback.py` (rejection lessons → Explorer) vs `express_feedback` (delivery preferences) share a name and nothing else; migration 010's header exists to disclaim the confusion.

**F22 — The "last N completed runs" subquery is copied verbatim three times** in `dream_cycle_db.py` (`:176-181`, `:210-215`, `~:250`), each opening its own connection; `build_feedback_injection` therefore runs three round-trips that could be one.

**F23 — Docstring/comment drift in the delivery path:** `express_push.py:4` "noon dream cycle" (02:00 Mon/Wed/Fri); `dream_cycle_scheduled.sh:17-19` describes profile `hybrid`/Kiro credits while `:33` exports `controlled`; `express.py:11,32` and `migrations/010` cite `EXPRESS-PLAN.md "Locked decisions"` — that file is now a 7-line redirect stub whose target section (`ARCHITECTURE.md#delivery-and-feedback`) contains no locked decisions; `express.py:341` "(currently none in the store)" — 1 `question` row; `memory_brief` docstring "spawns a subprocess" leaks the backend choice into the tool contract.

**F24 — Dead scaffolding:** `should_run_briefing`/`get_last_briefing_time`/`run_type='session_start'` (0 runs ever, only caller `orchestrator.py:301`); `express_push --force` (guards a file that has never been written); `express.py:_KIND_LABEL['question']` and `_gather_questions` (1 row, never surfaced in 3 recorded briefings).

**F25 — Test pollution of a production metrics file.** `logs/llm_metrics/adhoc.jsonl`: 26,087 records, **23,997 with `model: "m"`** (5,074 on 09-23 alone) — tests constructing invokers without setting `KIRO_LLM_METRICS_DIR`. The four real Express editor calls (F1) are buried in it. Set the env var in `conftest.py`.

**F26 — `digest_cap_usd` (`weekly_digest.py:104-123`) re-implements `SpendTracker.from_env` parsing** ("mirrors", by its own docstring). Expose a `parse_cap(env_name, default)` in `cost.py`.

**F27 — `test_express.py` DB tests are order-dependent:** `clean_tables` truncates *after* the test (`conftest.py:139-147`) and never touches `express_feedback` or `dream_cycle_*`; `test_compose_gathers_all_sources:150-175` asserts exact counts that hold only if the previous test cleaned up. Also no test covers: the default-invoker routing (F1), `#`-prefixed targets (F7), `express_last_surfaced` being written (F8), an aborted latest run (F4), the LLM-empty case (F9).

**F28 — `memory_learn` (`mcp_server.py:410-478`)** — one Titan embedding + one `hybrid_search` per topic with no cap on topic count (docstring says 2–4; prior audit confirmed), `content[:300]` truncation (F17), and it never records that STEP 2 happened, so "learning" is unmeasurable — same gap as F15.

**F29 — Complexity hot-spots recomputed (radon):** `generate_digest` **D-27**, `build_feedback_injection` **D-24**, `_gather_insights` **C-17** (not in the brief; highest in `express.py`), `should_push` C-15, `render_markdown` C-13, `edit_briefing` C-12, `render_email` C-12, `weekly_digest.run` C-12, `_apply_prefs` C-11.

---

## 6. Duplicated concepts across the four modules (question 5)

| Concept | `express.py` | `digest.py` | `feedback.py` | `weekly_digest.py` | Drift |
|---|---|---|---|---|---|
| Item ranking | `_deterministic_edit` (kind priority − pref_weight, recency) + LLM re-rank | group by strategy, alphabetical | newest cycle first, oldest trimmed | LLM writes one item | four orderings, none shared |
| Truncation | `_truncate` 600/400, `[:120]`, `[:107]` | none (full content) | `_first_sentence` 220, `_trim_to_budget`, `[:80]` | `[:200]`, `[:180]` | F17 |
| Headline generation | LLM JSON `{lead, items[{idx, headline}]}`, ≤90 chars | candidate title verbatim | title `[:80]` | LLM JSON `{title ≤80, summary}` | two "output ONLY a JSON object (no prose, no code fence)" prompts, different schemas |
| "Recent" window | 14 d / 30 d / 14 d | n/a | 2 runs | 7 d | F20 |
| Evaluator seat names | — | `skeptic/advocate/…` ×3 | `Skeptic/User Advocate/…` | — | F18 |
| Cross-project rule | twice (F19) | strategy label only | — | `project !~ uuid` filter | F3 |
| Model call | `AgentInvoker()` hardcoded Kiro, no tracker | none | none | resolver + `SpendTracker` + backend refusal | the correct pattern exists in the same subsystem |
| Env parsing | `ENV_TO/FROM/PASSWORD` raw | — | — | `digest_cap_usd` mirrors `SpendTracker.from_env` | F26 |
| DB access | own connection + `RealDictCursor` cursor injection; 2nd connection for `load_prefs` | `dream_cycle_db` × 3 calls + filesystem write | `dream_cycle_db` × 3 calls | raw tuple cursors + `get_processed_source_urls` | four styles |
| Delivery channel | Gmail SMTP (never configured) | file in `logs/` | Explorer prompt | memory + `osascript` toast (+ `job_wrapper` toast) | no shared ledger of what was delivered |
| "Last N runs" | latest completed run (F4) | — | last N completed (F6) ×3 copies | ISO week | four definitions of "recent run" |
| Sentinel "no LLM" | — | — | — | — | ×3 in callers (F16) |

---

## 7. Decompositions (question 6)

**`generate_digest` (D-27) → pure render + thin I/O.**
```
DigestInputs(run_id, date, accepted, rejected, verdicts_by_title, slice_rows, feedback_injected: bool)
load_digest_inputs(run_id, accepted, rejected, feedback_injected) -> DigestInputs   # the only DB reader
render_digest(inputs) -> str                                                         # pure
  _render_stats, _render_strategies, _render_attention (already in ledger),
  _render_feedback_summary, _render_accepted(candidate, verdict) -> list[str]
  _render_verdicts(verdict) uses consensus.tally_consensus + EVALUATOR_SEATS (kills the 3× role tuple)
write_digest(text, run_id, date) -> Path        # `{date}-{run_id[:8]}.md`, path from one paths module
```
The orchestrator passes `feedback_injected=bool(feedback)`; `was_feedback_injected` is deleted (F5). Complexity per function ≤ 8.

**`build_feedback_injection` (D-24) → fetch once, three pure formatters, one assembler.**
```
FeedbackRows = fetch_feedback_rows(n_cycles)  # ONE query over recent_completed_run_ids(n, require_candidates=True)
format_cycle_rejections(rows) -> list[str]     # per-cycle sections, newest first
format_dissents(rows) -> str | None
format_user_rejections(rows) -> str | None
assemble(header, cycle_sections, other_parts, budget) -> str   # existing _trim_to_budget
```
`EVALUATOR_SEATS` shared with digest (F18); `FEEDBACK_CYCLES` and the DB default unified (F20).

**`should_push` (C-15) → three small pure steps around one query.**
```
latest_delivered_run(cur) -> RunRef | None     # requires candidates_generated > 0 AND digest <> ''  (F4)
accepted_items(cur, run_id) -> list[Item]      # same Item shape compose_briefing uses
classify(item) -> Trigger | None               # ONE cross-project/contradiction rule shared with _gather_insights (F3, F14, F19)
decide(triggers, last_pushed) -> Decision
```
And in `express_push.main`: gates (`--dry-run`, `email_configured`) **before** `edit_briefing` (F1).

**Bonus — `_gather_insights` (C-17):** split the batched source lookup into `resolve_sources(cur, ids) -> dict` and the row→Item mapping into `insight_item(row, src_info)`; the cross-project rule moves to `classify` above.

---

## 8. Is the subsystem delivering value today?

No, and the evidence is unusually complete because every surface leaves a trace:

- **Push (email):** 0 sent in 110 days; 4 triggers, all stopped at `email_configured()`; 3 of them spent ~7.5 minutes of Kiro (Fable-served) time on corpus content for nothing (F1).
- **Pull (`memory_brief`):** 3 calls, identical lead each time, June contradiction as the star (F2, F8). `brief` CLI: 0 uses.
- **Digest file:** written every run, read by no delivery path, wrong about feedback 16/16 (F5), overwritten within a day at least once (F13).
- **Weekly digest:** 0/4 scheduled successes; 11 missing weeks; two manual rescues (F11).
- **Feedback:** 0 delivery preferences, 0 user rejections, `mark_user_rejected` uncallable; the only closed loop (evaluator → Explorer) is diluted by aborted runs and denied by the digest (F5–F7).
- **Signal quality:** the two gating signals are structurally broken — contradictions die within a run (F2) and "cross-project" means "two chat sessions" (F3).

What *is* worth keeping: the composition/render split is clean; `weekly_digest.resolve_digest_backend` + `SpendTracker` is exactly the pattern Express should copy; `render_email` is well-escaped; the `--dry-run` paths exist. The streamlining plan should (1) stop the unmetered Kiro call and reorder the gates (one afternoon), (2) fix `expire_stale_relationships` and the project predicate so the signals mean what the docs say, (3) collapse the three "no-LLM" sentinels, three role vocabularies and nine truncations, (4) record deliveries so resurfacing and learning become possible, and only then (5) decide whether email is still the channel — three months of "activation pending" suggests the user has voted with the MCP tool.

---

## Could not verify

- That `claude-fable-5.1` was the served model on 09-18 and 09-21 — the log says only "server-selected model used"; the 09-23 error names it explicitly, and `backends.toml:106-113` records the same behaviour. Not confirmed per call.
- Kiro credit cost of the three editor calls (no token usage recorded, `run_id: null`).
- Whether anyone reads the `logs/dream-cycle-digest-*.md` files by hand (no access log).
- Root cause of UUID `project` values on `kiro_cli_chat` rows (writer is `ingest.py:165` via `normalize_project_tag`; the upstream header is produced by chat extraction, not read here).
- Whether the post-`e998f3b` weekly digest succeeds under launchd — next scheduled run is Mon 09-28; `launchctl` was not run per the rules.
- Current `launchctl list` exit status of `com.second-brain.weekly-digest` (not run); the prior audit reported `1`.
- `memory_brief` usage outside `~/.kiro/sessions/cli` (other MCP clients were not searched).
- No script, model or test was executed; complexity figures are radon 6.x via `uvx`, not the tool that produced the numbers in the brief (they agree on the three named functions).
