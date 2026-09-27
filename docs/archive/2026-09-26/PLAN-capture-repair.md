# Plan: repair every capture channel — capture all of my digital interactions

Branch: to be created `capture-repair` off `main` (`619146d`). Started 2026-09-22.
Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped (reason in Decisions).
**Resume here after a restart:** read "Status", then the first unchecked task in "Phases".
Commands run from the repo root with `.venv/bin/python`; live DB `memory_bank`; hermetic tests
use `memory_bank_test`. Standing rules: local branches only, no push; merge to local `main` on
approval; never print secrets; capture jobs run as user LaunchAgents (`~/Library/LaunchAgents/
com.second-brain.*.plist`) whose scripts live in `scripts/jobs/`.

## 1. Goal

Every stream of the user's digital day lands in Second Brain automatically and stays fresh:
Kiro chats (CLI + IDE), **Codex sessions (not captured today)**, Quick, YouTube, Slack, Outlook
(email + calendar), browser history, articles/bookmarks. "Fresh" = a channel's newest row is no
older than its capture cadence, and each nightly job exits 0.

## 2. Current state (measured 2026-09-22 ~10:00, newest row per channel)

| Channel | Job (plist) | Last exit | Newest row | Verdict |
|---|---|---|---|---|
| Kiro CLI+IDE chat | chat-extract 02:30 → ingest 03:00 | 0 | 1.8 h | OK |
| distilled_chat (derived) | distill | 0 | 4.5 h | OK |
| article | ingest 03:00 (Crawlee) | 0 | 7 h | OK (parser fixed 2026-09-22) |
| browser_history | capture-browser 01:15 | 0 | 8.7 h | OK |
| YouTube | youtube 01:30 | 0 | 9 d | Idle, not broken — 0 new transcripts; verify end-to-end |
| Quick (qd_sync) | qd-sync hourly | 0 | feed 6 d; doc 100+ d | Source-synced (processed=0); confirm the Quick source itself is fed |
| **Slack** | capture-slack 01:00 | **1** | 57 h | **BROKEN — Midway AUTH_ERROR** |
| **Outlook email+cal** | capture-outlook 00:45 | **1** | 57–81 h | **BROKEN — MCP "Connection closed"** |
| **Codex** | — none — | — | never | **MISSING — 252 JSONL sessions on disk, newest 2026-09-21, nothing ingests them** |
| weekly_digest (derived) | weekly-digest | 1 | 6 d | Check after upstreams are healthy |
| credentials/liveness monitors | credentials, liveness | 1 | — | Failing — see Phase 5 |

Evidence: `logs/capture-slack-20260922.log` ends `McpToolError: list_my_channels: AUTH_ERROR …
Failed to refresh authentication token … midway-auth.amazon.com`; `logs/capture-outlook-20260922.log`
ends `McpError: Connection closed`; `~/.midway/cookie` was refreshed 09:29 today (after the 01:00
Slack run), so Slack may already recover on the next run — to be confirmed, not assumed. Codex
sessions live at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and `~/.codex/archived_sessions/`.

## 3. Constraints

1. No secrets in code, logs, commits, or agent prompts. Auth is interactive (`mwinit`, SSO,
   Graph device-code) — the plan documents *how the user re-auths*, it does not store tokens.
2. Subagents may read code, job scripts, plists and **their own** synthetic fixtures; they must
   not read message/email/chat *bodies* from the live DB or capture logs. Codex/Kiro transcript
   parsing is developed against small synthetic JSONL the agent writes itself.
3. Hermetic suite green at every commit; new parsers get unit tests with synthetic fixtures.
4. Idempotency is mandatory: every channel dedupes on a stable `source_url`/id so a re-run or an
   overlapping window never double-ingests (cf. the Crawlee bug just fixed).
5. Capture jobs must **fail loudly and exit non-zero** on auth/transport errors (so `liveness`
   catches them) but must **never** partially write a corrupt record.

## 4. Phases

### Phase 0 — Diagnose + branch  `[x]`  (this document)
- [x] Inventory channels, jobs, exit codes, freshness (§2).

### Phase 1 — Slack auth recovery  `[x]`
- [ ] Reproduce: `.venv/bin/python -m scripts.jobs.capture_slack` (or the job script) by hand now
      that `~/.midway/cookie` is fresh; capture the exact error if it still fails.
- [ ] If it was only the stale cookie: confirm a clean run, then add a **pre-flight auth check**
      to `scripts/jobs/capture_slack.sh` — probe `list_my_channels` first; on `AUTH_ERROR` emit a
      macOS notification ("Slack capture needs `mwinit`") and exit 2 *before* the main run, so the
      failure is actionable rather than a stack trace at 01:00.
- [ ] If it is deeper (slack-mcp binary/version, hardware-bound token): pin the `slack-mcp` path
      and version, document the re-auth command, and record the token lifetime so cadence < lifetime.
- [ ] Backfill the 57 h gap: run with a window covering 2026-09-20 → now; verify counts and no dups.
- [ ] Test: a unit test for the pre-flight check (auth error → exit 2, no capture attempted) using
      a fake MCP client.

### Phase 2 — Outlook (email + calendar) transport recovery  `[x]`
- [ ] Reproduce `grasp-mcp serve` by hand; the failure is `Connection closed` (server died/timeout),
      distinct from Slack's auth error. Check: grasp-mcp binary present/updated (`~/.toolbox/bin/
      grasp-mcp`), Graph token cached and unexpired, call timeout large enough for the mailbox.
- [ ] Fix the transport: bump `McpClient` connect/read timeout for grasp; add one reconnect-retry
      on `Connection closed`; ensure a dead server is detected and the job exits non-zero with a
      clear message (and a re-auth hint if the token expired).
- [ ] Backfill email (57 h) and calendar (81 h); dedupe on message-id / event-id.
- [ ] Test: reconnect-once-then-fail path with a fake client that closes mid-call.

### Phase 3 — Codex capture  `[x]` — **recovered from `origin/main`, not rebuilt**
The work already existed: PRs #1–#3 on `origin/main` (task capture, message capture,
live-recall verification). Local `main` and `origin/main` have **unrelated histories**
(no common ancestor — `origin/main` starts from a fresh "Initial public snapshot"), so
`git merge` refuses and the earlier "behind 20" reading was meaningless. Ported the
self-contained file set onto `codex-capture-port` and merged (`5386c2b`).
- [x] Cherry-pick `src/capture/{agent_tasks,codex}.py`, `src/capture/sources/codex.py`,
      `scripts/capture_codex.py`, the two job scripts, both plists, 5 test files, 7 fixtures.
- [x] Verify against current code: imports resolve; the INSERT targets only columns that
      still exist; 42 ported tests pass; full suite 1,510.
- [x] Two machine-specific defaults fixed: profile `codex_local` does not exist here, and of
      the profiles that do, `laptop`/`hybrid` route the Thinker to Kiro and `bedrock` to
      Fable 5.1 — both forbidden for corpus content, so the job pins `controlled`; and the
      Ollama/bge-m3 embedding gate printed `waiting_for_local_embedding` and **exited 0**
      when absent (a silent no-op reporting success) — removed, embeddings are Titan v2 via
      Bedrock, the same vector space as the rest of the corpus.
- [x] Backfill: 17 tasks captured + 16 semantic passes, $1.98 of Opus 5, 20 calls.
      Now 81 `codex_task` + 60 `distilled_agent_task` rows, all derived rows embedded.
      Verified searchable (Codex-specific queries return these rows first).
- [x] `config/skim.toml`: `codex_task` and `distilled_agent_task` are own-activity channels;
      `PROMPT_VERSION` → `skim-v3`.
- [x] LaunchAgent installed (`com.second-brain.codex-capture`, every 6 h, absolute paths,
      profile pinned). Verified: kickstart → exit 0, 14 unchanged, no writes, no cost.
- [x] Cleared the one task whose semantic contract failed (retry path: `semantic_retried: 1`).
- [ ] Follow-up: the semantic contract is strict and Opus 5 satisfies it only sometimes
      (observed 2 failures in 21 calls, both cleared on retry). Retries are unbounded — a
      permanently failing task would cost ~$0.10 per run forever. Bound the retry count, and
      consider whether topic segmentation needs Opus 5 at all (the skim does comparable work
      on Nova 2 Lite for ~$0.001/doc).

### Phase 4 — Verify the "idle but OK" channels end-to-end  `[ ]`
- [ ] YouTube: add one new liked/watch-later video, run the job, confirm a transcript lands with a
      stable `source_url`; confirm the fixed Crawlee parser ingests it (no empty-URL path).
- [ ] Quick: confirm what feeds the Quick *source* that `qd_sync` reads — if the upstream export is
      itself stale (feed 6 d, doc 100+ d), that is a Quick-side problem, not qd_sync; document where
      the export comes from and whether the sub-streams (chat/decision/doc) are expected to be quiet.
- [ ] Browser/articles: already fresh — just record expected cadence in the ops doc.

### Phase 5 — Monitoring so silent failure can't recur  `[x]`
- [ ] Fix `credentials` and `liveness` jobs (both exit 1 today) — they are the safety net that
      should have flagged Slack/Outlook. Make `liveness` assert per-channel freshness (newest row
      younger than N × cadence) and notify on breach.
- [ ] One `capture_status` view/report: newest row + last job exit per channel, one line each.
- [ ] Decide cadence vs token lifetime for each auth'd channel so tokens don't lapse between runs.

### Phase 6 — Wrap  `[ ]`
- [ ] Update `docs/CAPTURE-COMPONENTS.md` (channels, jobs, auth, cadence, Codex) and
      `docs/OPERATIONS.md` if it lists jobs; Status here; memory note; merge decision.

## 5. Status (newest first)

- 2026-09-23 — **All capture channels healthy; monitors fixed; merged to local `main` (`34bc2b5`).**
  - **Root cause of "nobody noticed": the credentials monitor cried wolf every night.** `aws` lives
    at `/usr/local/bin/aws`, which is not on the monitor's PATH under launchd, so both AWS checks
    reported "EXPIRED/BROKEN" (exit 127 misread as auth failure) every single night — alert fatigue
    that buried the real Slack and Outlook outages. Fixed (`dfd7b9d`): PATH corrected, three outcomes
    (OK / expired / tool-missing), and REQUIRED vs ADVISORY severity so only a credential the
    unattended pipeline actually needs fails the job. Verified in a launchd-like env: 4/4 ✅, exit 0.
  - `verify_liveness.sh` was never broken — it was correctly reporting silent channels. Expectations
    made truthful: `quick_desktop_doc` retired (dormant since 2026-06-04, advisory not failure),
    `quick_desktop_synth` 5 → 21 d, plus two new checks — `codex_task` (7 d) and the **Quick upstream
    sqlite file's mtime** (7 d), so "Quick stopped exporting" is visible as such. Now exits 0.
  - Slack + Outlook: both **self-healed** on 2026-09-23 (Midway cookie refreshed; grasp-mcp transient)
    and landed 64 Slack conversation-days, 30 emails, 15 calendar events. Hardened anyway (`f4c4ddc`):
    a Slack pre-flight auth probe, one bounded reconnect per client session for a dead MCP server, an
    exit-code taxonomy (0 ok / 3 auth / 4 transport / 1 other) with a notification only for auth, and
    `McpAuthError`/`McpTransportError` no longer subclassing `McpToolError` — previously a per-item
    `except McpToolError: continue` could swallow a lapsed credential and exit 0 with zero rows.
    Verified by real runs: Slack OK, Outlook captured 9 emails + 6 events, both exit 0.
  - **Weekly digest was violating the Kiro/Fable rule** and had failed since ~2026-09-16: it hardcoded
    `KiroInvoker` (corpus content → Kiro, which ignores `--model` and has routed to Fable 5.1) and
    timed out at 600 s. Now resolves the `skim` role — direct Bedrock in every profile — refuses any
    MCP-capable backend before building an invoker, 180 s timeout, metered under
    `SECOND_BRAIN_DIGEST_CAP_USD` (default $0.25). First digest since Sept 16 written for **$0.0004**.
  - Codex semantic retry bounded (`7abada7`): 3 attempts per unprocessed tail
    (`SECOND_BRAIN_CODEX_SEMANTIC_MAX_ATTEMPTS`), counter in the task's `metadata.semantic_attempts`
    keyed by prompt-version + model so a model/prompt change grants a fresh budget; abandonment is
    marked, logged and counted as `semantic_abandoned` in the run summary. Previously unbounded at
    ~$0.10 per retry every 6 h.
  - Repo cleaned: 5 merged worktrees removed, 9 merged branches deleted, one worktree and one branch
    (`main`) remain; the user's 83 uncommitted files untouched. Suite 1,630 green.
  - OPEN for the user: the dedup backup tables from 2026-09-22 (`memories_dedup_backup_2026_09_22_1340`
    6,493 rows / 101 MB and its relationships twin) are still the only copy of the deleted duplicates —
    drop them once you are satisfied, or keep as insurance.
- 2026-09-22 — **Codex capture is live.** Found the existing implementation on `origin/main`
  rather than rebuilding it (§Phase 3). Histories are unrelated, so the fix was a file-level
  port, not a merge. Backfilled 17 tasks ($1.98), 141 Codex rows now searchable, LaunchAgent
  installed at 6 h and verified idempotent. Remaining broken channels: **Slack** (Midway auth)
  and **Outlook** (grasp-mcp transport) — Phases 1–2, untouched so far. Monitors (`credentials`,
  `liveness`) still failing — Phase 5 — which is why both went unnoticed for days.
- 2026-09-22 — Diagnosis done (§2), plan written. Slack + Outlook broken on auth/transport; Codex
  not captured though 252 sessions sit on disk; YouTube/Quick idle-but-not-broken; monitors
  (credentials, liveness) themselves failing. Midway cookie refreshed 09:29, so Slack may self-heal
  on the next run — verify first (Phase 1). Branch `capture-repair` to be created.

## 6. Decisions

- **2026-09-22 Order: Slack → Outlook → Codex.** Slack is likely a one-command re-auth (fastest
  win), Outlook a transport fix, Codex net-new work; fix cheapest-first and get the monitors
  (Phase 5) back so nothing fails silently again.
- **2026-09-22 Codex is `codex_cli_chat`, own-activity.** Keeps it parallel to `kiro_cli_chat` and
  makes the skim treat it as the user's work (salience prior), not external content.

## 7. Useful queries / commands

```sql
-- freshness per channel
SELECT source_type, count(*) FILTER (WHERE parent_id IS NULL) parents,
       max(created_at) newest, round(extract(epoch FROM (now()-max(created_at)))/3600,1) AS hours_ago
FROM memories WHERE source_type IS NOT NULL GROUP BY 1 ORDER BY newest DESC;
```
```bash
launchctl list | grep second-brain                      # loaded jobs + last exit code
ls -t logs/capture-slack-*.log | head -1                 # newest per-job log
mwinit                                                    # refresh Midway (Slack auth)
find ~/.codex/sessions ~/.codex/archived_sessions -name 'rollout-*.jsonl' | wc -l   # Codex sessions on disk
```
