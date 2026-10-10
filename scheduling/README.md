# Scheduling — macOS LaunchAgents

All plists live in this directory and are what launchd loads: install them as
**symlinks** into `~/Library/LaunchAgents/` so a checkout of the repo changes the
job definitions in place (effective at the next `launchctl bootout` +
`bootstrap`). Until R5 two installed files (`qd-sync`, `codex-capture`) were
*copies*, so checkouts silently left them on the old definition; the R5 deploy
runbook replaces both with symlinks. 17 plists are committed; 16 labels are
loaded on the Mac — `com.second-brain.distill` is `launchctl disable`d by the
operator and stays so (that state lives outside the plist).

Every job runs through one boundary, `scripts/jobs/job_wrapper.sh` →
`python -m src.operations.jobs <script> <job-name>`, which supplies PATH
(Homebrew, PostgreSQL 17, the venv, `~/.toolbox/bin`, `/usr/local/bin`),
`AWS_PROFILE=second-brain-jobs`, `SECOND_BRAIN_PROFILE=controlled`, `umask 077`,
`caffeinate -i`, one `flock` per job name, the deadline, the per-day log
`logs/<job-name>-YYYYMMDD.log` and the durable status `logs/jobs/<job-name>.json`
(read by the 09:00 liveness sweep). The child's exit code is the wrapper's exit.

## Jobs

| Label | Schedule | Program (via `job_wrapper.sh`) → job name | Deadline (`SECOND_BRAIN_JOB_TIMEOUT_<NAME>`) | Other `EnvironmentVariables` | Description |
|---|---|---|---|---|---|
| `com.second-brain.capture-outlook` | Daily 00:45 | `scripts/jobs/capture_outlook.sh` → `capture-outlook` | `3600` (observed ≤ 2 min) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Outlook mail + calendar via grasp-mcp |
| `com.second-brain.capture-slack` | Daily 01:00 | `scripts/jobs/capture_slack.sh` → `capture-slack` | `3600` (observed ≤ 2 min + ≤ 15 min Retry-After budget) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Slack conversation-days via slack-mcp |
| `com.second-brain.bookmarks` | Sat 01:00 | `scripts/scrape_bookmarks.py` → `bookmarks` | `10800` (observed 39–64 min; floor 3 h) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Chrome bookmarks scrape (Crawlee) |
| `com.second-brain.capture-browser` | Daily 01:15 | `scripts/jobs/capture_browser.sh` → `capture-browser` | `3600` (observed ≤ 2 min) | — | Chrome history + reading capture |
| `com.second-brain.youtube` | Daily 01:30 | `scripts/jobs/youtube_scrape.sh` → `youtube` | `3600` (observed ≤ 5 min) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Liked / Watch-Later transcripts (yt-dlp) |
| `com.second-brain.backup` | Daily 02:00 | `scripts/jobs/backup.sh` → `backup` | `7200` (observed 14–31 min; floor 2 h) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Encrypted bundle → Google Drive (`config/backup.toml`: KB sources included, retention 7 d local / 30 d remote) |
| `com.second-brain.dream-cycle` | **Mon/Wed/Fri 12:30** (`StartCalendarInterval` array; not nightly; moved from 02:00 on 2026-10-09, see below) | `scripts/jobs/dream_cycle_scheduled.sh` → `dream-cycle` | `21600` (observed 39–204 min; floor 6 h) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1`; `SECOND_BRAIN_JOB_GRACE_S=120`; `WorkingDirectory` | Dream cycle synthesis (Express delivery stays disabled) |
| `com.second-brain.chat-extract` | Daily 02:30 | `scripts/jobs/capture_chats.sh` → `chat-capture` | `3600` (observed < 1 min; R4 first nights ≈ 770 embeddings) | — | Kiro IDE (native + legacy) and CLI chat capture, bounded 50 sessions/run |
| `com.second-brain.ingest` | Daily 03:00 | `scripts/crawlee_ingest.py` → `crawlee-ingest` | `3600` (observed < 1 min) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Crawlee knowledge-store ingest, ≤ 25 never-indexed docs/run |
| `com.second-brain.verify` | Sun 03:00 | `scripts/jobs/verify_backup.sh` → `verify` | `7200` (1.1 GB download + full restore ≈ 4 min; floor 2 h) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Newest remote backup (either format) decrypted, freshness ≤ 36 h, bundles fully restored into a disposable cluster |
| `com.second-brain.distill` | Daily 05:00 — **disabled** | `scripts/distill_sessions.py` → `distill` | `7200` | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Session distillation (launchctl-disabled; liveness treats `distilled_chat` as advisory) |
| `com.second-brain.weekly-digest` | Mon 06:00 | `scripts/jobs/weekly_digest.sh` → `weekly-digest` | `7200` (observed 5–98 min; floor 2 h) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1`; `WorkingDirectory` | Weekly digest (Nova on Bedrock; failing since 09-07 → liveness advisory) |
| `com.second-brain.credentials` | Daily 07:00 | `scripts/jobs/verify_credentials.sh` → `credentials` | `3600` (observed ≤ 1 min) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` | Credential probe (required/advisory classification unchanged) |
| `com.second-brain.liveness` | Daily 09:00 | `scripts/jobs/verify_liveness.sh` → `liveness` | `3600` (observed seconds) | — | Capture liveness (`config/liveness.toml`), job status sweep, durable `logs/jobs/liveness-report.json` |
| `com.second-brain.qd-sync` | Every 3600 s (`StartInterval`) | `scripts/jobs/qd_sync.sh` → `qd-sync` | `3600` (observed seconds; R4 first run ≈ 270 embeddings) | `Nice=10`; launchd stdout → `logs/qd-sync-launchd.log` | Quick Desktop sync (facts, tags, feed, chats; the Slack knowledge-graph writer and its tables were retired in R7, migration 018). **Installed file was a copy → replace with a symlink** |
| `com.second-brain.codex-capture` | Every 21600 s (6 h) | `scripts/jobs/codex_capture.sh` → `codex-capture` | `7200` (≤ 5 metered Opus calls/run; floor 2 h) | `SECOND_BRAIN_JOB_NEEDS_NETWORK=1`; `SECOND_BRAIN_PROFILE=controlled`, `AWS_PROFILE=second-brain-jobs`, `CODEX_HOME=<home>/.codex` (the plist carries the absolute path — launchd does not expand `~`); launchd stdout → `logs/codex-capture-launchd.log`; `WorkingDirectory` | Codex task capture. **Installed file was a copy → replace with a symlink** |
| `com.second-brain.capture-api` | `KeepAlive` service | `scripts/jobs/capture_api.sh` → `capture-api` | none while healthy; startup bounded by `SECOND_BRAIN_SERVICE_STARTUP_TIMEOUT_S=120` | `SECOND_BRAIN_JOB_SERVICE=1`, `SECOND_BRAIN_HEALTH_URL=http://127.0.0.1:8100/health`, `SECOND_BRAIN_NOTIFY_INTERVAL_S=1800`; stdout/err → `~/Library/Logs/second-brain/` | HTTP capture endpoint (port 8100). Token comes from the gitignored `.env`, never the plist |

**Why the dream cycle runs at 12:30, not 02:00 (2026-10-09).** The Mac sleeps with the lid closed, and a dark-wake (maintenance) run loses the network: the 2026-10-09 02:00 run failed on DNS, as dream-cycle had on 09-30. Mon/Wed/Fri 12:30 is a midday slot when the machine is normally in use, so the run gets a full network and up to its 6 h deadline without depending on dark wake. The network preflight below still applies. Backup (daily 02:00) and weekly-digest (Mon 06:00) keep their times.

Every plist carries `ExitTimeOut=150` (launchd's grace on bootout ≥ the wrapper's 120 s grace + 20).

`SECOND_BRAIN_JOB_NEEDS_NETWORK=1` marks the 12 jobs that talk to the network (Bedrock, Slack/Outlook MCP, YouTube, Crawlee, Drive, AWS credentials); the wrapper waits for DNS before starting them (see the contract below). **Verification after deploy: the first Monday 06:00 weekly-digest run.** Its `logs/weekly-digest-<date>.log` should show `Network preflight: … resolved after N attempt(s), Ns; deadline 7200s starts now` followed by the digest and `OK (exit 0)`, and `logs/jobs/weekly-digest.json` `network_reachable: true`, `state: succeeded`. A `network_unavailable` (exit 68) means DNS never came up inside 15 min — raise `SECOND_BRAIN_JOB_NETWORK_WAIT_S` in the plist or look at the Mac's power settings, not at the model. After two consecutive OK Mondays remove `weekly-digest` from `[jobs.advisory]` in `config/liveness.toml` and flip `channels.weekly_digest` back to `required`.

Deadlines come from observed wall-clock durations in `logs/` (first/last line per
job log, 2026-09-05 … 2026-10-06): dream-cycle 39/46/53/92/204 min, bookmarks
39/64/64 min, backup 14–31 min, weekly-digest 5–98 min; every other batch job
finished in under 6 minutes on every observed night. `tests/test_scheduling_plists.py`
fails if a plist's deadline drops below the floor table in `src/operations/jobs.py`.

## Environment contract (`src/operations/jobs.py`)

| Variable | Default | Meaning |
|---|---|---|
| `SECOND_BRAIN_JOB_TIMEOUT_<NAME>` | — | Deadline in seconds for job `<NAME>` (`chat-capture` → `CHAT_CAPTURE`). **Set in every committed batch plist.** An explicit per-job value always wins, even below the floor (manual rehearsals). |
| `SECOND_BRAIN_JOB_TIMEOUT` | `3600` | Generic deadline when no per-job value is set. The runner raises it (and the default) to the job's floor: `dream-cycle` 21600, `bookmarks` 10800, `backup`/`verify`/`weekly-digest`/`codex-capture`/`distill` 7200, everything else 3600 — so a bare `job_wrapper.sh dream_cycle_scheduled.sh dream-cycle` can never be killed at 1 h. The status file records `timeout_s` and `timeout_source` (`per-job`, `generic`, `default`, `*-raised-to-floor`, `argument`, `service-startup`). |
| `SECOND_BRAIN_JOB_GRACE_S` | `120` | On deadline (or SIGTERM to the wrapper) the whole process group gets SIGTERM, then up to this many seconds to exit — the dream-cycle orchestrator uses it to close its run row (`RunInterrupted`, R3) — then SIGKILL. Every plist sets `ExitTimeOut=150` (≥ grace + 20; pinned by `tests/test_scheduling_plists.py`) so launchd's own SIGKILL on `bootout`, logout or reboot waits for it instead of firing after the 20 s default and leaving the child (own session) to finish alone with the status stuck at `running` (S3-2). Exit 124 on deadline; `stopped_gracefully` in the status says whether SIGKILL was needed. |
| `SECOND_BRAIN_JOB_SERVICE` | `0` | `1` = service mode (capture-api): no deadline while `/health` answers `{"status":"healthy"}`; startup bounded by `SECOND_BRAIN_SERVICE_STARTUP_TIMEOUT_S` (120); probes every `SECOND_BRAIN_HEALTH_INTERVAL` s (30) with `SECOND_BRAIN_HEALTH_TIMEOUT` (3); `SECOND_BRAIN_HEALTH_FAILURES` (3) consecutive failed probes stop it (exit 1) and launchd restarts it. A restart waits up to `SECOND_BRAIN_SERVICE_LOCK_WAIT_S` (30) for the previous wrapper's lock. |
| `SECOND_BRAIN_JOB_NEEDS_NETWORK` | `0` | `1` = **network preflight** before the job starts. launchd fires the early-morning jobs (02:00 backup, 06:00 weekly-digest; dream-cycle until it moved to 12:30 on 2026-10-09) during a dark (maintenance) wake, before the network stack is up; weekly-digest died every Monday since 09-07 and dream-cycle on 09-30 with `EndpointConnectionError` / `nodename nor servname provided` on the first Bedrock call. The runner holds a `caffeinate -s -i` assertion (so the Mac does not sleep again) and retries `socket.getaddrinfo(SECOND_BRAIN_JOB_NETWORK_PROBE_HOST)` — DNS only, no HTTP — with backoff 5, 10, 20, 40, 60 … s for up to `SECOND_BRAIN_JOB_NETWORK_WAIT_S` (900). **The deadline starts after the wait.** If the host still does not resolve the job is *not* started: exit `68`, state `network_unavailable`, its own `consecutive_network_failures` counter (the job's `consecutive_failures` is untouched), no desktop notification — the 09:00 liveness sweep reports it ⚠ as infrastructure and ❌ once it happened on 2 scheduled runs in a row (`[jobs] network_failures_required`). Set in the 12 network-dependent plists (`tests/test_scheduling_plists.py::NETWORK_JOBS`). |
| `SECOND_BRAIN_JOB_NETWORK_PROBE_HOST` / `SECOND_BRAIN_JOB_NETWORK_WAIT_S` | `bedrock-runtime.us-east-1.amazonaws.com` / `900` | Probe host and wait budget (seconds) for the preflight. The status file records `network_probe_host`, `network_wait_budget_s`, `network_attempts`, `network_waited_s`, `network_reachable`. |
| `SECOND_BRAIN_NOTIFY` / `SECOND_BRAIN_NOTIFY_INTERVAL_S` | `1` / `1800` | Desktop notification on failure (osascript; never affects the exit). In service mode at most one per interval per job (durable `last_notified_at`), and the wrapper backs off 10, 20, 40 … ≤ `SECOND_BRAIN_SERVICE_BACKOFF_MAX_S` (300) s before exiting, so a database outage is one notification and a slowing restart loop, not a storm. |
| `SECOND_BRAIN_LOG_DIR` / `SECOND_BRAIN_STATUS_DIR` | `logs/` / `logs/jobs/` | Per-day logs (pruned after 30 days) and status/lock files (created 0700; files 0600). |
| `SECOND_BRAIN_CAFFEINATE` | `1` | `0` disables `caffeinate -i` (tests). |
| `SECOND_BRAIN_PYTHON` / `SECOND_BRAIN_JOB_PYTHON` | `.venv/bin/python` | Interpreter for `.py` targets and for the adapters; `SECOND_BRAIN_JOB_PYTHON` overrides only the runner itself. |
| `AWS_PROFILE`, `SECOND_BRAIN_PROFILE` | `second-brain-jobs`, `controlled` | Injected into every job unless already set (plists may pin them, as codex-capture does). |

Exit codes a log reader can classify: `0` ok · `1` job failure · `3`/`4` auth/transport (capture adapters) · `68` network unavailable at launch (preflight budget spent, job **not** started; infrastructure, see `SECOND_BRAIN_JOB_NEEDS_NETWORK`) · `69` PostgreSQL not reachable (`scripts/jobs/_preflight.sh`; **not auto-started**, see that file) · `75` already running (lock) · `78` configuration (missing interpreter/tool/.env) · `124` deadline · `127` script missing (a plist pointing at a deleted file) · `128+n` interrupted by signal *n*.

## Install / Uninstall

```bash
# Install one job (symlink, never a copy)
ln -sfn "$HOME/second-brain/scheduling/com.second-brain.<name>.plist" ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.second-brain.<name>.plist

# Reload a job after a checkout changed its plist
launchctl bootout gui/$(id -u)/com.second-brain.<name>; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.second-brain.<name>.plist

# Check which installed plists are copies instead of symlinks (should print nothing)
find ~/Library/LaunchAgents -maxdepth 1 -name 'com.second-brain.*.plist' -type f

# Status: loaded labels (16; distill stays disabled) and the last run of each job
launchctl list | grep second-brain
for f in ~/second-brain/logs/jobs/*.json; do python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['job'], d['state'], d.get('exit_code'), d.get('finished_at'))" "$f"; done
```

## Logs and status

* `logs/<job-name>-<YYYYMMDD>.log` — the job's own output, wrapped in `Starting …` / `OK|FAILED (exit N)` lines.
* `logs/jobs/<job-name>.json` — durable status: `state` (`running|succeeded|failed|interrupted`), `exit_code`, `started_at`, `finished_at`, `duration_s`, `timeout_s`, `timeout_source`, `grace_s`, `last_success_at`, `consecutive_failures`, `first_failure_at`, `last_notified_at`; services add `heartbeat_at`, `health_state`.
* `logs/jobs/liveness-report.json` — every finding of the 09:00 sweep (`config/liveness.toml`).
* `~/Library/Logs/second-brain/capture-api-*.log` — launchd stdout/stderr of the service wrapper.

See [OPERATIONS.md](../docs/OPERATIONS.md) and [DISASTER-RECOVERY.md](../docs/DISASTER-RECOVERY.md).
