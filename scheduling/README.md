# Scheduling — macOS LaunchAgents

All plists live in this directory. Install by symlinking to `~/Library/LaunchAgents/`.

## Jobs

| Plist | Schedule | Script | Description |
|---|---|---|---|
| `com.second-brain.bookmarks` | Sat 1:00 AM | `scrape_bookmarks.py` | Chrome bookmarks scrape |
| `com.second-brain.youtube` | Daily 1:30 AM | `youtube_scrape.sh` | YouTube playlist/history scrape |
| `com.second-brain.backup` | Daily 2:00 AM | `backup.sh` | Encrypted backup to Google Drive |
| `com.second-brain.chat-extract` | Daily 2:30 AM | `chat_extract.py` | Phase 1 chat extraction |
| `com.second-brain.ingest` | Daily 3:00 AM | `ingest_staged.sh` | Phase 2 staged ingestion |
| `com.second-brain.qd-sync` | Every 3600s | `qd_sync.sh` | Quick Desktop sync (5 scripts: memories, tags, events, Slack graph, chats) |
| `com.second-brain.verify` | Sun 3:00 AM | `verify_backup.sh` | Backup verification (Google Drive) |
| `com.second-brain.dream-cycle` | Daily 12:00 PM | `dream_cycle_scheduled.sh` | Dream cycle pipeline |
| `com.second-brain.liveness` | Daily 9:00 AM | `verify_liveness.sh` | Per-source capture liveness check (alerts if a channel goes silent) |
| `com.second-brain.capture-api` | Removed | `capture_api.sh` | Deprecated HTTP capture endpoint (not installed) |

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
