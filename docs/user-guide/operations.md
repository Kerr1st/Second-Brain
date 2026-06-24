---
title: "Operations"
type: how-to | reference
---

# Operations

Day-to-day tasks for keeping Second Brain healthy: scheduled jobs, backups, credential refresh, and monitoring.

## Scheduled jobs

All jobs run as macOS *launchd agents* (user-level scheduled tasks defined by `.plist` files in `scheduling/`). Most run overnight so they don't compete with daytime resources.

| Job | Schedule | What it does | Plist |
|-----|----------|--------------|-------|
| Bookmark scrape | Sat 1:00 AM | Captures new browser bookmarks | `com.second-brain.bookmarks` |
| YouTube capture | Daily 1:30 AM | Downloads and transcribes new YouTube videos | `com.second-brain.youtube` |
| Backup | Daily 2:00 AM | Encrypts and uploads pg_dump + JSON to Google Drive + local | `com.second-brain.backup` |
| Chat extraction | Daily 2:30 AM | Extracts Kiro/IDE chat sessions into staged files | `com.second-brain.chat-extract` |
| Staged ingestion | Daily 3:00 AM | Parses, classifies, embeds, and stores staged captures | `com.second-brain.ingest` |
| Session distill | Daily 5:00 AM | Distills chat sessions into concise memories | `com.second-brain.distill` |
| Weekly digest | Mon 6:00 AM | Generates a weekly synthesis digest | `com.second-brain.weekly-digest` |
| Liveness check | Daily 9:00 AM | Verifies core services are running | `com.second-brain.liveness` |
| Dream cycle | Daily 12:00 PM | Runs autonomous synthesis (Explorer → Thinker → Evaluator panel) | `com.second-brain.dream-cycle` |
| QD sync | Every 60 min | Incrementally syncs Quick Desktop data into PostgreSQL | `com.second-brain.qd-sync` |
| Backup verify | Sun 3:00 AM | Downloads and decrypts latest backup to confirm integrity | `com.second-brain.verify` |

> [!NOTE]
> The `capture-api` plist exists in `scheduling/` but is **deprecated and unused** — ignore it.

## Installing and loading a LaunchAgent

To activate a new or updated job:

```bash
# 1. Symlink the plist into the LaunchAgents directory
ln -sf ~/second-brain/scheduling/com.second-brain.<name>.plist ~/Library/LaunchAgents/

# 2. Load the agent
launchctl load ~/Library/LaunchAgents/com.second-brain.<name>.plist
```

Verify all Second Brain jobs are loaded:

```bash
launchctl list | grep second-brain
```

Expected output — one line per loaded job with PID (or `-` if not currently running) and last exit code (`0` = success):

```text
-    0    com.second-brain.backup
-    0    com.second-brain.qd-sync
-    0    com.second-brain.dream-cycle
...
```

To unload a job: `launchctl unload ~/Library/LaunchAgents/com.second-brain.<name>.plist`

## Backups

The daily backup job (`scripts/jobs/backup.sh`) runs at 2:00 AM. It creates encrypted snapshots and stores them in two locations.

### What's backed up

| Asset | Destinations | Retention |
|-------|-------------|-----------|
| PostgreSQL dump (`.dump.gpg`) | Google Drive + local | 30 days cloud / 7 days local |
| JSON exports (memories, entities, edges) | Google Drive + local | 30 days cloud / 7 days local |
| Knowledge-base source docs | Google Drive + local | Current only |
| Config files (migrations, scripts, compose) | Google Drive + local (+ git) | Current only |

All files are encrypted with GPG AES-256 using a passphrase file at `~/second-brain/.backup-key`.

### How to restore (high-level)

1. Install prerequisites: `brew install postgresql@17 pgvector rclone gpg`
2. Obtain the GPG passphrase key — the `~/second-brain/.backup-key` file (keep a secure offline copy; see [Disaster Recovery](../DISASTER-RECOVERY.md) for key custody).
3. Download the latest backup from Google Drive with `rclone copy`.
4. Decrypt with `gpg --decrypt --batch --passphrase-file`.
5. Restore into PostgreSQL with `pg_restore`.

For the complete step-by-step procedure, see [Disaster Recovery](../DISASTER-RECOVERY.md).

## AWS SSO refresh

Second Brain uses Amazon Bedrock (via AWS SSO) for embeddings and LLM calls. SSO tokens expire after approximately 8–12 hours.

**When tokens expire:**
- Embedding, search, ingestion, and the dream cycle fail.
- Backups are **unaffected** (they use Google Drive + local, not AWS).

**To refresh:**

```bash
aws sso login --profile default
```

This opens a browser for authorization. Verify the token is valid:

```bash
aws sts get-caller-identity
```

Expected output:

```json
{
    "Account": "<aws-account-id>",
    "Arn": "arn:aws:sts::<aws-account-id>:assumed-role/...",
    "UserId": "..."
}
```

> [!TIP]
> Run `aws sso login --profile default` each morning (or after wake from sleep) to avoid mid-day failures.

## Monitoring checklist

Run these checks to confirm the system is healthy:

| Check | Command | Expected |
|-------|---------|----------|
| Jobs loaded | `launchctl list \| grep second-brain` | All jobs listed, exit code `0` |
| Backup ran today | `cat ~/second-brain/logs/backup-$(date +%Y%m%d).log` | Ends with "Backup complete" |
| QD sync running | `cat ~/second-brain/logs/qd-sync-$(date +%Y%m%d).log` | Hourly entries present |
| SSO valid | `aws sts get-caller-identity` | Returns account info (no error) |
| PostgreSQL up | `pg_isready -h 127.0.0.1 -p 5432 -U memory_bank` | "accepting connections" |
| Memory count | `psql -h 127.0.0.1 -U memory_bank -d memory_bank -c "SELECT count(*) FROM memories;"` | ~122 K |

## Troubleshooting

### PostgreSQL is down

```bash
brew services start postgresql@17
```

Verify:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U memory_bank
```

```text
127.0.0.1:5432 - accepting connections
```

### AWS SSO expired

Symptom: `memory_search`, ingestion, or the dream cycle returns an `ExpiredTokenException`.

```bash
aws sso login --profile default
```

### Job failed (non-zero exit code)

Check the job's log:

```bash
cat ~/second-brain/logs/<job-name>-$(date +%Y%m%d).log
```

The `job_wrapper.sh` also sends a macOS notification on failure — look in Notification Center.

### Google Drive upload failed

Re-authorize rclone:

```bash
rclone config reconnect gdrive:
```

Then re-run the backup manually:

```bash
bash ~/second-brain/scripts/jobs/backup.sh
```

---

## Related

- [User Guide index](index.md)
- [Getting started](getting-started.md)
- [Disaster Recovery (full procedure)](../DISASTER-RECOVERY.md)
