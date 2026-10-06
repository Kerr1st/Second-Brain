# Scheduling — macOS LaunchAgents

All plists live in this directory. Install by symlinking to `~/Library/LaunchAgents/`.

## Jobs

| Plist | Schedule | Script | Description |
|---|---|---|---|
| `com.second-brain.bookmarks` | Sat 1:00 AM | `scrape_bookmarks.py` | Chrome bookmarks scrape |
| `com.second-brain.youtube` | Daily 1:30 AM | `youtube_scrape.sh` | YouTube playlist/history scrape |
| `com.second-brain.backup` | Daily 2:00 AM | `backup.sh` | Encrypted backup to Google Drive |
| `com.second-brain.chat-extract` | Daily 2:30 AM | `capture_chats.sh` → `src.capture.chats` (job name `chat-capture`) | Kiro IDE (native sessions + legacy `.chat`) and CLI capture, bounded per run (`--max-sessions`, default 50, newest first) |
| `com.second-brain.ingest` | Daily 3:00 AM | `crawlee_ingest.py` → `src.capture.crawlee` (job name `crawlee-ingest`) | Crawlee knowledge-store ingest (articles/transcripts); never-indexed sources bounded per run (`--max-unindexed`, default 25) |
| `com.second-brain.qd-sync` | Every 3600s | `job_wrapper.sh` → `qd_sync.sh` → `src.capture.qd` | Quick Desktop sync (stages: facts, tags, feed, chats; doc export retired, replay via `src.capture.qd_docs`) |
| `com.second-brain.verify` | Sun 3:00 AM | `verify_backup.sh` | Backup verification (Google Drive) |
| `com.second-brain.dream-cycle` | Mon/Wed/Fri 2:00 AM | `dream_cycle_scheduled.sh` | Dream cycle pipeline (`StartCalendarInterval` array in the plist; not nightly) |
| `com.second-brain.liveness` | Daily 9:00 AM | `verify_liveness.sh` | Per-source capture liveness check (alerts if a channel goes silent) |
| `com.second-brain.capture-api` | KeepAlive | `capture_api.sh` | HTTP capture endpoint (port 8100) |

## Install / Uninstall

```bash
# Install one job
ln -s ~/second-brain/scheduling/com.second-brain.<name>.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.second-brain.<name>.plist

# Install all jobs
for f in ~/second-brain/scheduling/*.plist; do
  ln -sf "$f" ~/Library/LaunchAgents/
done
launchctl load ~/Library/LaunchAgents/com.second-brain.*.plist

# Unload a job
launchctl unload ~/Library/LaunchAgents/com.second-brain.<name>.plist

# Check status (exit code 0 = last run succeeded)
launchctl list | grep second-brain
```

## Logs

All jobs write to `~/second-brain/logs/<job-name>-<YYYYMMDD>.log`.

See [OPERATIONS.md](../docs/OPERATIONS.md) for monitoring details.
