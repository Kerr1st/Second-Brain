## review_ops

=== Context Gathering Summary ===
FOUND: All 18 repo plists (labels, schedules, ProgramArguments, WorkingDirectory, EnvironmentVariables, log paths); byte-level comparison of the 2 non-symlinked installed copies (identical, md5-confirmed); symlink-vs-copy install topology; all 20 scripts/jobs/*.sh read in full; job_wrapper.sh delegation map; launchctl list load state and last exit codes (weekly-digest=1); the /path/to/ placeholder bug in codex-canary.plist; the /usr/local/bin PATH fix applied to only 2 of 17 scripts; docs/OPERATIONS.md §Schedule templates (12-vs-18 count, dream-cycle cadence, weekly-digest entrypoint, mcp_serve.sh interpreter claim); DIGEST_TIMEOUT / DIGEST_CAP_ENV env contracts against scripts/weekly_digest.py; the Sep 21 weekly-digest KiroInvoker timeout traceback and the subsequent resolver rewrite; git check-ignore status for every listed directory; scripts/backups (62 MB) provenance and ignore status; scripts/logs orphan status (zero code references); log-freshness survey of logs/ including the stale qd-sync-launchd.log and duplicate chat_extract/chat-extract streams.
NOT FOUND: Runtime success/failure of any job (execution out of scope); actual delivery of osascript notifications; current byte sizes of the two unprunable append-mode logs; launchctl print-disabled output; whether the two copied plists drifted historically (no VCS on ~/Library/LaunchAgents); explanation for scripts/backups/config being 42 MB; contents of .env and .backup-key (deliberately not read).
====================================

=== CONTEXT GATHERING COMPLETE ===

ANSWER:
# Operations Surface Review — second-brain @ 6b8c2d2 (read-only)

No files were modified, no jobs were started/stopped, no capture/ingest/digest/dream script was executed.

---

## 1. PLIST vs SCRIPT vs REPO CONSISTENCY

### 1.1 Inventory (18 repo plists)

| Label suffix | Schedule | Entry (after `/bin/bash`) | WorkingDir | Env | stdout/stderr |
|---|---|---|---|---|---|
| `backup` | Cal 02:00 daily | `job_wrapper.sh backup.sh backup` | — | — | — (wrapper) |
| `bookmarks` | Cal Wd6 01:00 (Sat) | `job_wrapper.sh scripts/scrape_bookmarks.py bookmarks` | — | — | — |
| `capture-api` | `KeepAlive=true` | `capture_api.sh` (no wrapper) | — | — | `~/Library/Logs/second-brain/capture-api-{stdout,stderr}.log` |
| `capture-browser` | Cal 01:15 daily | `job_wrapper.sh capture_browser.sh capture-browser` | — | — | — |
| `capture-outlook` | Cal 00:45 daily | `job_wrapper.sh capture_outlook.sh capture-outlook` | — | — | — |
| `capture-slack` | Cal 01:00 daily | `job_wrapper.sh capture_slack.sh capture-slack` | — | — | — |
| `chat-extract` | Cal 02:30 daily | `job_wrapper.sh scripts/chat_extract.py chat-extract` | — | — | — |
| **`codex-canary`** | `StartInterval` 3600 + `RunAtLoad` | `/path/to/second-brain/.../codex_capture_canary.sh` | — | 3 vars | `/path/to/second-brain/logs/codex-canary.log` |
| `codex-capture` | `StartInterval` 21600 | `codex_capture.sh` (no wrapper) | `/Users/kerrfir/second-brain` | `SECOND_BRAIN_PROFILE=controlled`, `AWS_PROFILE=second-brain-jobs`, `CODEX_HOME` | `logs/codex-capture.log` (both) |
| `credentials` | Cal 07:00 daily | `job_wrapper.sh verify_credentials.sh credentials` | — | — | — |
| `distill` | Cal 05:00 daily | `job_wrapper.sh scripts/distill_sessions.py distill` | — | — | — |
| `dream-cycle` | Cal **array** Wd1/3/5 @02:00 | `job_wrapper.sh dream_cycle_scheduled.sh dream-cycle` | `/Users/kerrfir/second-brain` | — | — |
| `ingest` | Cal 03:00 daily | `job_wrapper.sh ingest_staged.sh ingest` | — | — | — |
| `liveness` | Cal 09:00 daily | `job_wrapper.sh verify_liveness.sh liveness` | — | — | — |
| `qd-sync` | `StartInterval` 3600, `RunAtLoad=false`, `Nice=10` | `qd_sync.sh` **directly, no `/bin/bash`, no wrapper** | — | — | `logs/qd-sync-launchd.log` (both) |
| `verify` | Cal Wd0 03:00 (Sun) | `job_wrapper.sh verify_backup.sh verify` | — | — | — |
| `weekly-digest` | Cal Wd1 06:00 (Mon) | `job_wrapper.sh weekly_digest.sh weekly-digest` | `/Users/kerrfir/second-brain` | — | — |
| `youtube` | Cal 01:30 daily | `job_wrapper.sh youtube_scrape.sh youtube` | — | — | — |

Every script path referenced by a plist exists (18/18 resolved OK). Both log directories referenced (`~/Library/Logs/second-brain`, `<repo>/logs`) exist.

### 1.2 Repo → installed diff

**Finding 1.2a — CRITICAL: `codex-canary.plist` is an unsubstituted template.**
`scheduling/com.second-brain.codex-canary.plist:10,26,28` contain the literal placeholder `/path/to/second-brain/...`. It is the only plist in the set with a non-absolute-real path, and it is the only one not installed — consistent with it never having worked. If it were installed as-is, launchd would fail to spawn it (bad program path) and write nothing to a log, because the log path is equally fake.
*Fix:* either substitute `/Users/kerrfir/second-brain` and install, or delete the plist and `codex_capture_canary.sh` together (see 4.1 — the canary is Ollama-gated and Ollama does not exist here).

**Finding 1.2b — MEDIUM: two installed plists are real copies, not symlinks.**
15 of 17 installed files are symlinks into `scheduling/`. The exceptions are `~/Library/LaunchAgents/com.second-brain.codex-capture.plist` (regular file, Sep 22 15:01) and `com.second-brain.qd-sync.plist` (regular file, Apr 21 21:06). Both are **byte-identical to the repo copies right now** (md5 `dc6d20bb…` and `e6ddd235…` match on both sides; `diff` is empty). So there is no drift today, but two of eighteen jobs silently opt out of the "edit repo → launchd sees it" contract that the other fifteen rely on. `qd-sync`'s copy is five months older than its repo twin, so the match is luck, not discipline.
*Fix:* replace both with symlinks so the installed set has one uniform mechanism.

**Finding 1.2c — Orphans.** Repo-with-no-install: `codex-canary` only (1). Installed-with-no-repo-copy: **none**. `launchctl list` shows exactly the 17 expected labels and no extras. Your ground truth holds.

**Finding 1.2d — LOW: `qd-sync` bypasses `/bin/bash`.**
`com.second-brain.qd-sync.plist:9` makes the script itself argv[0], relying on the shebang + the executable bit. It is currently `-rwxr-xr-x`, so it works — but it is the only plist with this shape, and a `chmod` slip turns it into a silent non-start. Every other plist is interpreter-explicit.

---

## 2. WRAPPER DUPLICATION

`scripts/jobs/` has 20 `.sh` files. Only 11 of 18 plists route through `job_wrapper.sh`; 7 scripts reimplement parts of it.

| Script | shebang/`set` | PATH built | venv | cd repo | AWS_PROFILE | log + rotation | lock | timeout | notify | exit codes | JSON |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `job_wrapper.sh` | `set -uo pipefail` :6 | `/opt/homebrew/bin` :9 | `.venv/bin/python` for `*.py` :32 | **no** | `:-second-brain-jobs` :13 | `logs/<job>-<date>.log` :22; prune +30d :46 | none | none | `osascript` :39 | passthrough :48 | no |
| `backup.sh` | `-uo pipefail` :5 | pg17+brew :6 | — | `:7` | none | own `log()` :18 | none | none | none | 1 on missing prereq/verify fail | no |
| `verify_backup.sh` | `-uo pipefail` :5 | pg17+brew :6 | — | `:7` | none | own `log()` :14 | none | none | none | 1 | no |
| `ingest_staged.sh` | `-uo pipefail` :9 | pg17+brew :10 | `.venv/bin/python` :35,46 | `:11` | none | **own** `logs/ingest-$DATE.log` :13 (collides w/ wrapper) | none | none | none | **always `exit 0` :70** | **yes** `logs/last_ingest_status.json` :56 |
| `qd_sync.sh` | **`-euo pipefail`** :5 | pg17+brew :12 | `.venv/bin/python` ×6 | `:13` | `:9` | own `logs/qd-sync-$DATE.log` :16, **no prune** | none | none | none | dies at first failing step | no |
| `youtube_scrape.sh` | `-uo pipefail` :9 | none | `.venv/bin/python` :11 | `:10` | none | none (wrapper's) | none | none | none | passthrough | no |
| `capture_browser.sh` | `-uo pipefail` :5 | brew :6 | :8 | `:7` | none | none | none | none | none | passthrough | no |
| `capture_slack.sh` | `-uo pipefail` :13 | toolbox+brew :15 | :18 | `:16` | none | none | none | none | none | **0/3/4/1 taxonomy** :21-27 | no |
| `capture_outlook.sh` | `-uo pipefail` :12 | toolbox+brew :14 | :17 | `:15` | none | none | none | none | none | **0/3/4/1 taxonomy** :21-26 | no |
| `capture_api.sh` | **`-euo pipefail`** :9 | none | :22 | **hardcoded** `:10` | none | plist-owned | none | n/a (daemon) | none | 78 EX_CONFIG :14,19 | no |
| `codex_capture.sh` | **`-euo pipefail`** :26 | pg17+brew :35 | `$PYTHON` :29 | `:41` | `:37` | plist-owned | none | none | none | 1 abort / 2 no-python | no |
| `codex_capture_canary.sh` | **`-euo pipefail`** :4 | pg17+brew :24 | `$PYTHON` :17 | `:48` | **none** | plist-owned | none | none | none | 2 | **yes** :44 |
| `dream_cycle_scheduled.sh` | **no `set` at all** | toolbox+pg17+brew :2 | hardcoded `$PY` :14 | **no** | **none** | wrapper's | none | none | none | dc_status :46 | no |
| `verify_credentials.sh` | `-uo pipefail` :40 | toolbox+brew+**`/usr/local/bin`** :42 | `$PYTHON_BIN` :46 | `:43` | none (uses `--profile` literal) | wrapper's | none | **`timeout 90`** :62 | own `notify()` :49 | 1 | no |
| `verify_liveness.sh` | `-uo pipefail` :24 | pg17+brew+**`/usr/local/bin`** :25 | `$PYTHON_BIN` :33 | `:26` | none | wrapper's | none | none | own `notify()` :36 | 1 | no |
| `weekly_digest.sh` | `-uo pipefail` :25 | toolbox+pg17+brew :34 | `$PYTHON` :28 | `:40` | `:35` | wrapper's | none | none | none | 1/2 (+doc claims 3,4) | no |
| `mcp_serve.sh` | **no `set`** | none | `.venv/bin/python` :8 | hardcoded `:4` | `:7` | n/a | n/a | n/a | n/a | exec | no |

**Finding 2a — HIGH: the `/usr/local/bin` PATH fix was applied to two scripts, not to the shared wrapper.**
`job_wrapper.sh:9` still exports only `/opt/homebrew/bin:$PATH`. The very bug documented at length in `verify_credentials.sh:16-24` ("neither this script's old PATH line **nor job_wrapper.sh** included `/usr/local/bin`") was fixed only locally, in `verify_credentials.sh:42` and `verify_liveness.sh:25`. The other 15 job scripts — including `backup.sh`, `verify_backup.sh`, `ingest_staged.sh`, `codex_capture.sh`, `weekly_digest.sh` — still cannot see `/usr/local/bin`. Any of them that ever needs `aws` (or anything else installed there) will reproduce the exact 127-misread-as-real-failure pattern. The script's own comment names `job_wrapper.sh` as a root cause and the root cause was left in place.
*Fix:* move the canonical PATH (`$HOME/.toolbox/bin:/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH`) into `job_wrapper.sh` and delete the per-script PATH lines. Note `job_wrapper.sh` currently omits the postgres@17 dir too, which is why seven scripts each re-add it.

**Finding 2b — MEDIUM: `set -e` is inconsistent and the inconsistency is load-bearing in one place.**
Five scripts use `-euo pipefail`, eleven use `-uo pipefail`, and `dream_cycle_scheduled.sh` / `mcp_serve.sh` set nothing. `job_wrapper.sh:6` omitting `-e` is *correct* — it must survive the child's failure to reach line 38 — but nothing says so, so the next editor may "fix" it and break all failure notification. Conversely `qd_sync.sh:5` uses `-e`, which means step 3 of 6 failing skips steps 4-6 and the summary line at :32 never prints; the log just stops.

**Finding 2c — MEDIUM: `ingest_staged.sh` writes to the same filename the wrapper writes.**
`job_wrapper.sh:22` opens `logs/ingest-$(date +%Y%m%d).log` and appends the child's stdout; `ingest_staged.sh:13` independently opens `logs/ingest-$(date +%Y%m%d).log` and `tee -a`s to it (:20) while that stdout is *also* being appended. Two writers, one file, interleaved — plus every `log()` line lands twice. The same double-write shape exists for chat-extract: `logs/chat-extract-20260923.log` (wrapper) and `logs/chat_extract-20260923.log` (script's own, underscore) both exist and both were written at 02:30, with near-identical content.

**Finding 2d — `dream_cycle_scheduled.sh` is the least conformant script.** No `set` line at all (:1), PATH export placed *above* the file's own header comment (:2), hardcoded `$PY` (:14) and hardcoded script paths (:38,:44) despite `job_wrapper.sh` already resolving the venv, and no `AWS_PROFILE` export — it inherits the wrapper's. Its comment block at :17-19 says Bedrock auth uses "the CLI `default` profile (kiro-admin-session SSO)", which directly contradicts `job_wrapper.sh:10-13`, which forces `AWS_PROFILE=second-brain-jobs` precisely because the SSO session is what killed the jobs on 2026-09-08. The comment describes the pre-fix world.

**What a single shared wrapper should own:** PATH (incl. `/usr/local/bin` + pg17), `AWS_PROFILE` default, repo `cd`, venv resolution, `caffeinate`, log path + rotation, failure notification, exit-code passthrough, and two things nothing owns today — **a lock/singleflight** (no script has one; `qd-sync` at 3600s and `codex-capture` at 21600s can overlap a slow predecessor, and `ingest` at 03:00 overlaps a long `backup` at 02:00) and **a per-job timeout** (only `verify_credentials.sh:62` bounds anything; `weekly-digest` hung 600s on Sep 21 with nothing to stop it). Per-job scripts should own only: the command, the exit-code taxonomy, and job-specific env.

**Already delegating** (11): backup, bookmarks, capture-browser, capture-outlook, capture-slack, chat-extract, credentials, distill, dream-cycle, ingest, liveness, verify, weekly-digest, youtube. **Reimplementing / bypassing** (4 installed + 1 not): capture-api, qd-sync, codex-capture, (codex-canary), plus `mcp_serve.sh` which is not a launchd job.

---

## 3. CONTRADICTIONS AND INACCURACIES

**Finding 3a — HIGH: documented dream-cycle cadence is wrong.**
`docs/OPERATIONS.md:70` — which `scheduling/README.md:3` names as the single authoritative source — states `dream-cycle | Daily 12:00`. The plist is `StartCalendarInterval` as an **array** of Weekday 1/3/5 at **02:00** (`com.second-brain.dream-cycle.plist:14-40`), i.e. Mon/Wed/Fri 02:00. `dream_cycle_scheduled.sh:22-23` independently confirms Mon/Wed/Fri ("runs Mon/Wed/Fri only, see the plist"). The runbook overstates frequency by ~2.3× and is 10 hours off on time — material, because the doc's own cost math depends on run count.

**Finding 3b — HIGH: documented plist count is wrong.**
`docs/OPERATIONS.md:56` claims "There are **12 committed plist templates**" and its table lists 12. There are 18. Six installed-or-committed jobs are absent from the authoritative table: `credentials`, `capture-browser`, `capture-outlook`, `capture-slack`, `codex-capture`, `codex-canary`. Five of those six are loaded and running. `scheduling/README.md:5` repeats the error ("Consolidated all12 template schedules"). A runbook that omits the Slack/Outlook/credentials jobs is missing exactly the channels that went dark for days.

**Finding 3c — MEDIUM: documented weekly-digest entrypoint is stale.**
`docs/OPERATIONS.md:68` lists the entrypoint as `scripts/weekly_digest.py`. The plist calls `scripts/jobs/weekly_digest.sh` (`com.second-brain.weekly-digest.plist:11`), added 2026-09-23 per that script's header (:1-8). Following the doc would skip `SECOND_BRAIN_PROFILE=controlled` (:36), the spend cap (:37), and the Postgres pre-flight (:43-50).

**Finding 3d — MEDIUM: `docs/OPERATIONS.md:52` misstates the MCP interpreter.**
It says "`scripts/jobs/mcp_serve.sh` uses system `python3`". `mcp_serve.sh:8` is `exec .venv/bin/python -m src.mcp_server`, and its own comment at :3 says the opposite of the doc ("system python3 does not have the pinned mcp/psycopg2"). The doc's warning is inverted relative to the code.

**Finding 3e — MEDIUM: `weekly_digest.sh`'s documented exit codes do not exist.**
`weekly_digest.sh:24` promises "backend refusal 2, spend cap 3, timeout 4". In `scripts/weekly_digest.py` the timeout path (`:284-285`) and spend-cap path (`:281`) both fall through to a generic `sys.exit(main())` (:291); there is no `exit 3` or `exit 4` anywhere in either file. The shell's own `exit 2` (:31) is "python not executable", not "backend refusal". So the documented taxonomy is unusable for triage, and `verify_liveness.sh` cannot distinguish the cases as the comment implies.

**Finding 3f — MEDIUM: `DIGEST_TIMEOUT` is a real knob that the wrapper never sets.**
`scripts/weekly_digest.py:72` reads `DIGEST_TIMEOUT` (default 180). `weekly_digest.sh:34-38` exports `AWS_PROFILE`, `SECOND_BRAIN_PROFILE`, `SECOND_BRAIN_DIGEST_CAP_USD`, `BEDROCK_REGION` — but not `DIGEST_TIMEOUT`, and the header block at :10-21 documents only the first three. The one setting that caused the observed Sep 21 outage (a 600s timeout, logged at `logs/weekly-digest-20260921.log`) is the one the wrapper does not pin. `SECOND_BRAIN_DIGEST_CAP_USD` *does* match (`weekly_digest.py:81` `DIGEST_CAP_ENV = "SECOND_BRAIN_DIGEST_CAP_USD"`), and `SECOND_BRAIN_PYTHON` is honoured consistently across `weekly_digest.sh:28`, `codex_capture.sh:29`, `verify_credentials.sh:46`, `verify_liveness.sh:33` — so the convention exists; `DIGEST_TIMEOUT` is the gap.

**Finding 3g — MEDIUM: `verify_liveness.sh` carries a dead Docker variable.**
`verify_liveness.sh:28` sets `CONTAINER="${DB_CONTAINER:-second-brain-db}"` and never uses it again — every query goes to `psql -h 127.0.0.1` (:102). Same vestige in `backup.sh:12`. `job_wrapper.sh:16-18` states the Docker path was removed at the native-Postgres migration. Also `verify_backup.sh:48` comments "Validate dump structure via docker" immediately above a plain local `pg_restore` (:49).

**Finding 3h — MEDIUM: hardcoded absolute paths that break a repo move (Mac mini).**
`$HOME`-relative-but-path-assuming: `backup.sh:11` and `verify_backup.sh:9` both compute `KEY_FILE="$HOME/second-brain/.backup-key"` **after** having already `cd`'d to the real repo root (:7 in both). If the checkout is not literally `~/second-brain`, both abort with "missing prerequisites: encryption-key" — backup silently stops producing and weekly verify fails. They should use `./.backup-key`.
Fully hardcoded `/Users/kerrfir/second-brain`: `job_wrapper.sh:22,32,46`; `dream_cycle_scheduled.sh:14,38,44`; `capture_api.sh:10`; `mcp_serve.sh:4`. Plus every one of the 18 plists (unavoidable for launchd, but it means an install-time substitution step is required — and `codex-canary.plist` is the proof that step is manual and gets forgotten).
Hardcoded tool locations: `/usr/bin/caffeinate` (`job_wrapper.sh:32,34`, `qd_sync.sh:11`) is fine; `/Applications/ChatGPT.app/Contents/Resources/codex` (`codex_capture_canary.sh:26`, `codex-canary.plist:19`) is not. `backup.sh:75` rsyncs from `~/Work/Tools/Crawlee/knowledge/sources/` with `|| true`, so a missing source dir on a new machine silently backs up nothing.

**Finding 3i — LOW: `capture_api.sh` claims `.env` but runs `set -euo pipefail` over `. ./.env`.** `:16` does `set -a; . ./.env; set +a` under `-e` (:9) — a syntax error or failing line in `.env` kills the daemon with no diagnostic beyond launchd's stderr file. Minor, but this is the one job with no wrapper and no notification.

**Finding 3j — LOW: schedules disagree with nothing but cluster tightly.** 00:45 outlook → 01:00 slack → 01:15 browser → 01:30 youtube → 02:00 backup+dream-cycle → 02:30 chat-extract → 03:00 ingest. `backup` and `dream-cycle` both fire at 02:00 on Mon/Wed/Fri; `backup.sh` does a `pg_dump` while the dream cycle is hitting the same DB. Nothing serialises them (no locks, per 2b).

---

## 4. DEAD OR ORPHANED OPS ARTIFACTS

**Finding 4a — HIGH: the codex canary is wholly orphaned and internally impossible.**
`grep` across `docs/`, `scheduling/`, `scripts/`, `README.md` finds `codex-canary`/`codex_capture_canary` referenced **only inside its own plist**. Zero documentation, zero references. Worse, `codex_capture_canary.sh:27-29` defaults to `EMBEDDING_PROVIDER=ollama` / `OLLAMA_EMBEDDING_MODEL=bge-m3` and :31-46 probes `http://127.0.0.1:11434`, printing `{"status":"waiting_for_local_embedding"}` and **`exit 0`** when absent. `codex_capture.sh:18-23` states explicitly that this Ollama gate was removed from the sibling script because "There is no Ollama here" and because the silent-success-on-absence behaviour is a bug. The canary is the original, unfixed copy of code whose defect is documented in the file next to it. It also defaults `SECOND_BRAIN_PROFILE=codex_local` (:25), a profile `codex_capture.sh:11-12` says "is not in config/backends.toml". *Fix: delete `codex_capture_canary.sh` + `com.second-brain.codex-canary.plist`.*

**Finding 4b — MEDIUM: two non-executable, unscheduled backfill scripts.**
`scripts/jobs/dedup_ide_backfill.sh` and `scripts/jobs/reindex_embedding.sh` are mode `-rw-r--r--` (no `+x`), have no plist, and are the only two files in `scripts/jobs/` that are not executable. They are one-time maintenance operations living in the scheduled-jobs directory. They *are* mentioned in `docs/OPERATIONS.md` and `docs/INTERFACES.md`, so they are not undocumented — but they do not belong in `jobs/`. *Fix: move to `scripts/maintenance/`.*

**Finding 4c — MEDIUM: `scripts/logs/` is written by nothing.**
66 files, `ingest-2026040[2-8].log` etc., newest mtime **2026-06-02**. A repo-wide grep for the string `scripts/logs` across all `.sh` and `.py` returns **zero matches**. Every current writer targets repo-root `logs/` (`job_wrapper.sh:22`, `ingest_staged.sh:13`, `qd_sync.sh:16`). This is a fossil from before the scripts `cd` to repo root. *Fix: delete the directory.*

**Finding 4d — MEDIUM: `scripts/backups/` — 62 MB of stale, misplaced backup output.**
Contents are `kb-sources/` (20 MB, 1318 files, newest Jun 6) and `config/` (42 MB, newest Apr 3). This is exactly the output shape of `backup.sh:74-86` (`$BACKUP_DIR/kb-sources`, `$BACKUP_DIR/config`), but `backup.sh:9` sets `BACKUP_DIR="backups"` relative to the repo root it `cd`s to at :7 — so the *live* target is `/Users/kerrfir/second-brain/backups/` (mtime Sep 23, active). `scripts/backups/` is orphaned output from an older revision that ran with `scripts/` as cwd. Nothing writes it, nothing reads it, and `verify_backup.sh` verifies the Google Drive copy, not this. It is **not tracked** in git (0 tracked files) and it **is ignored** — `git check-ignore -v` resolves it to `.gitignore:32:backups/`, because the pattern has no leading slash and therefore matches at any depth. So it costs the repo nothing in git terms; it is 62 MB of disk litter. *Fix: delete. It is neither a backup of record nor reachable by any job.*

**Finding 4e — LOW: `logs/qd-sync-launchd.log` is effectively dead.**
The plist points both stdout and stderr there (`com.second-brain.qd-sync.plist:14-16`), but `qd_sync.sh` redirects every step's output into `logs/qd-sync-$DATE.log` itself (:21-32). Newest mtime on the launchd file is **2026-08-24** — a month of hourly runs with nothing escaping. Weak evidence of health, but it means the file the plist designates as the job's output is not the file a human should read, and nobody would notice if it started filling.

**Finding 4f — LOW: duplicated chat-extract log stream.** `logs/chat-extract-20260923.log` (wrapper) and `logs/chat_extract-20260923.log` (script) are both live and near-identical — see 2c. One is redundant.

**Finding 4g — LOW: retired-channel scaffolding retained on purpose.** `verify_liveness.sh:43-53` keeps `quick_desktop_doc` listed with `severity=retired`, and :58-60 notes `quick_desktop_session_event` was retired. This is deliberate and well-commented (dormancy stays visible), so I flag it as intentional, not dead — but it is the kind of entry that accretes.

---

## 5. OBSERVABILITY GAPS

Failure visibility by job:

| Job | How a failure surfaces | Silent? |
|---|---|---|
| backup, bookmarks, capture-browser, capture-outlook, capture-slack, chat-extract, distill, dream-cycle, ingest, verify, weekly-digest, youtube | `job_wrapper.sh:39` macOS notification + `FAILED (exit N)` in `logs/<job>-<date>.log` | No |
| credentials | wrapper notification **and** own `notify()` (`verify_credentials.sh:49,136`) | No — double-notified |
| liveness | wrapper notification **and** own `notify()` (`verify_liveness.sh:36,148`) | No — double-notified |
| **ingest** | wrapper never fires: `ingest_staged.sh:70` is `exit 0` unconditionally. A Crawlee failure is logged at :49 and discarded | **YES** |
| **qd-sync** | no wrapper, no notification. `set -e` (:5) aborts mid-sequence; output goes to `logs/qd-sync-$DATE.log` with the summary line (:32) simply missing | **YES** |
| **codex-capture** | no wrapper, no notification. Appends to `logs/codex-capture.log` | **YES** |
| **capture-api** | no wrapper, no notification. `KeepAlive=true` respawns on crash forever; evidence only in `~/Library/Logs/second-brain/capture-api-stderr.log`, whose newest write is **2026-09-14** | **YES** |
| codex-canary | not installed; would write to a nonexistent `/path/to/...` | n/a |

**Finding 5a — HIGH: four scheduled jobs have no failure notification path.** `ingest`, `qd-sync`, `codex-capture`, `capture-api` — and `ingest` is the worst case because it *is* wrapped but defeats the wrapper with `exit 0`. `ingest_staged.sh:46-50` catches the Crawlee failure, logs `"Crawlee ingest FAILED (exit $?)"`, then proceeds to exit 0. (Aside: `$?` on line 49 is already clobbered by the `if`, so the code printed there is not the failure code.)

**Finding 5b — HIGH: `weekly-digest` is currently in a failed state and only `launchctl list` knows.**
`launchctl list` shows `- 1 com.second-brain.weekly-digest` — last exit status 1. `logs/weekly-digest-20260921.log` ends with `TimeoutError: Agent subprocess timed out after 600s` inside `KiroInvoker` (`weekly_digest.py:95` at that revision) and `[07:43:05] FAILED (exit 1)`. The notification fired on Sep 21 and then vanished; the persistent red status is invisible unless someone runs `launchctl list`. Note the *code* has since been fixed — `scripts/weekly_digest.py` at HEAD imports `default_resolver`/`SKIM_ROLE` (:42) and no longer constructs `KiroInvoker` (header :9-23, commit `e998f3b`) — but no successful run has occurred since, so the job's observable state is still "failing". Nothing in the system reports "job X's last exit was non-zero N days ago".

**Finding 5c — MEDIUM: machine-readable status is the exception.**
Only two artifacts: `logs/last_ingest_status.json` (`ingest_staged.sh:56-65`, with `timestamp`/`processed`/`failed`/`crawlee_captures`/`pending`/`failed_files`) and the canary's single JSON line (`codex_capture_canary.sh:44`, dead code per 4a). Every other job — including all three monitors — emits only prose with emoji. `verify_liveness.sh` computes exactly the structured data a dashboard would want (label, age, threshold, severity, :102-113) and then throws the structure away into `log()`. Consequently the only machine-readable signals in the whole surface are launchd's exit codes, and nothing aggregates those.

**Finding 5d — MEDIUM: notifications are the sole channel, and they are ephemeral + unverifiable.**
`osascript -e 'display notification'` (`job_wrapper.sh:39`, `verify_credentials.sh:49`, `verify_liveness.sh:36`) requires an active GUI session and Notification Center permission for `osascript`; both monitor scripts append `|| true` so a failed notification is swallowed. For a headless Mac mini (per `docs/MAC-MINI-MIGRATION.md`) with no one logged in, this channel delivers nothing and reports success doing so. There is no email, no file-based alert state, no exit-status sweep.

**Finding 5e — MEDIUM: unbounded log growth for the two non-wrapper jobs.**
`job_wrapper.sh:46` prunes `logs/*.log` with `-mtime +30`. `logs/codex-capture.log` and `logs/qd-sync-launchd.log` are *appended in place* by launchd rather than date-stamped, so their mtime is always fresh and they can never be pruned. Every date-stamped log rotates; these two grow forever. (I did not measure current sizes.)

**Finding 5f — LOW: `credentials` and `liveness` each notify twice on failure** (own `notify()` then the wrapper's). Given `verify_credentials.sh:11-14` identifies alert fatigue as the actual prior outage, double-notifying is a small step backwards.

---

## 6. REPO HYGIENE

`git check-ignore -v` and `git ls-files` results:

| Path | Tracked | Ignored | Rule |
|---|---|---|---|
| `scripts/backups/` (62 MB) | No (0 files) | **Yes** | `.gitignore:32` `backups/` — matches at any depth |
| `scripts/__pycache__/` | No | **Yes** | `.gitignore:2` `__pycache__/` |
| `scripts/logs/` (66 files) | No | **Yes** | `.gitignore:33` `logs/` |
| `logs/` (310 entries) | No | **Yes** | `.gitignore:33` |
| `staging/` | No | **Yes** | `.gitignore:13` |
| `scripts/staging/` | No | **Yes** | `.gitignore:13` |
| `backups/` (repo root, live) | No | **Yes** | `.gitignore:32` |
| `evaluations/` | **1 file**: `evaluations/query_sets/seed.json` | **No** (dir not ignored; `.gitignore:28` ignores only `evaluations/results/`) | intentional |
| `.dream/` | No (0 files) | **No — untracked and unignored** | — |
| `.DS_Store` | No | Yes | `.gitignore:24` |

**Finding 6a — GOOD: no large/generated/machine-specific artifact is committed.** Every directory on your list is either ignored or (for `evaluations/`) deliberately tracking one small committed query set, which matches the `.gitignore:27` comment "query sets are committed". `.DS_Store` files exist on disk at repo root and in `scripts/` but are ignored and untracked. I found no tracked binary, dump, or log.

**Finding 6b — MEDIUM: `.dream/` is neither tracked nor ignored.**
It exists (mtime Sep 16), has 0 tracked files, and `git check-ignore` does not match it — so it shows as untracked and will keep appearing in `git status` noise, and could be accidentally committed by a `git add .`. Every sibling state directory is explicitly ignored. *Fix: add `.dream/` to `.gitignore`.*

**Finding 6c — LOW: `.gitignore:32-33` (`backups/`, `logs/`) are under a `# Secrets` heading** alongside `.backup-key` and `.env`. They are bulk/generated artifacts, not secrets (though `backups/` does hold corpus dumps). The unanchored patterns are also why `scripts/backups/` and `scripts/logs/` are invisibly ignored — which is how 62 MB of dead output survived unnoticed. Anchoring them (`/backups/`, `/logs/`) would have surfaced both. Recommend anchoring **only after** deleting the two stale directories, since anchoring first would expose them as untracked.

**Finding 6d — LOW: uncommitted edit inside the ops surface.** `git status` shows ` M scheduling/README.md`. The rest of `scheduling/` and all of `scripts/` is clean. I did not modify it.

---

## Severity roll-up

**CRITICAL** — 1.2a codex-canary `/path/to/` placeholders.
**HIGH** — 2a `/usr/local/bin` fix not applied to `job_wrapper.sh`; 3a dream-cycle cadence documented as Daily 12:00 vs actual Mon/Wed/Fri 02:00; 3b "12 plist templates" vs 18, six jobs undocumented; 4a canary orphaned + Ollama-gated silent success; 5a four jobs with no failure notification (`ingest` defeats its own wrapper); 5b weekly-digest sitting at exit 1 with no visibility.
**MEDIUM** — 1.2b two installed plists are copies not symlinks; 2b inconsistent `set -e`; 2c duplicate log writers; 2d dream_cycle_scheduled.sh nonconformance + stale SSO comment; 3c/3d/3e/3f doc & exit-code & env-var drift; 3g dead Docker vars; 3h `$HOME/second-brain` backup-key assumption; 4b non-exec backfill scripts in `jobs/`; 4c `scripts/logs/` written by nothing; 4d 62 MB `scripts/backups/`; 5c prose-only status; 5d osascript-only alerting on a headless host; 5e unprunable logs; 6b `.dream/` unignored.
**LOW** — 1.2d qd-sync no `/bin/bash`; 3i/3j; 4e/4f/4g; 5f double notify; 6c/6d.

---

## Could NOT verify, and why

- **Whether any job actually succeeds.** I did not execute any capture/ingest/digest/dream/backup script, per scope. All behavioural claims are from source reading plus existing logs and `launchctl list` exit codes.
- **Runtime effect of the `/usr/local/bin` gap in the other 15 scripts.** Confirming it would require running them under `env -i PATH=/usr/bin:/bin` as `verify_credentials.sh:38` suggests — that invokes real work. I verified only that the PATH line is absent (`grep -L`), and that `/usr/local/bin` is present in exactly two scripts.
- **Current sizes of `logs/codex-capture.log` and `logs/qd-sync-launchd.log`** (Finding 5e). I established the structural reason they can never be pruned but did not stat them.
- **Whether `osascript` notifications are actually delivered.** Requires checking Notification Center authorisation state / triggering a notification. The `|| true` suffixes mean logs cannot tell us either.
- **Whether `weekly-digest` now succeeds post-`e998f3b`.** The fix is present in `scripts/weekly_digest.py` at HEAD, but the next scheduled run is Monday and I must not trigger it. Its `launchctl` status will stay `1` until then.
- **`launchctl print-disabled`** — `docs/OPERATIONS.md:16` notes a disabled-override is a distinct fact from loaded. I did not run it; it is read-only and worth checking, but `launchctl list` already showed all 17 present.
- **Whether the two non-symlink installed plists have *ever* drifted.** They match today; `~/Library/LaunchAgents` is not version-controlled, so history is unavailable.
- **`scripts/backups/config/` at 42 MB** is larger than `backup.sh:80-86` obviously accounts for; I confirmed it is orphaned and ignored but did not enumerate its contents to explain the size.
- **Contents of `.env` / `.backup-key`** — not read, per secret-handling policy. I relied on `capture_api.sh:12-20` for the `CAPTURE_API_TOKEN` contract.

RELEVANT FILES (ranked by importance):
  1. scripts/jobs/job_wrapper.sh — The shared wrapper 11 of 18 jobs route through; still omits /usr/local/bin and postgresql@17 from PATH (the documented root cause of the credential false-alarm), hardcodes /Users/kerrfir/second-brain in three places, owns the only notification path, and its -mtime +30 prune cannot reach append-mode logs. Any wrapper consolidation starts here.
  2. scheduling/com.second-brain.codex-canary.plist — The single critical finding: unsubstituted /path/to/second-brain placeholders at lines 10, 26, 28, explaining why it is the one repo plist not installed. Referenced nowhere else in the repo, and its script is the unfixed Ollama-gated ancestor of codex_capture.sh.
  3. docs/OPERATIONS.md — Named by scheduling/README.md as the single authoritative schedule source, and materially wrong: 12 templates vs 18 actual, dream-cycle as "Daily 12:00" vs Mon/Wed/Fri 02:00, stale weekly-digest entrypoint, inverted mcp_serve.sh interpreter claim, and six running jobs missing from the table.
  4. scripts/jobs/ingest_staged.sh — Best single example of wrapper duplication plus the worst observability gap: reimplements logging into the same filename job_wrapper.sh opens, and ends in unconditional exit 0 so its wrapper can never notify on a Crawlee failure. Also holds the only real machine-readable status artifact in the repo.
  5. scripts/jobs/verify_credentials.sh — Documents the PATH/exit-127 bug in detail at lines 11-39 and fixes it only locally at line 42, while naming job_wrapper.sh as a root cause that was left unfixed. Establishes the TOOL-MISSING severity model and the SECOND_BRAIN_PYTHON/AWS_BIN override convention a shared wrapper should adopt.

NOTE: Line ranges have been extended by ~50 lines to provide additional surrounding context.

---

