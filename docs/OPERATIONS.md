# Second Brain — Operations

> Last updated: 2026-04-22

## Scheduled Jobs

All jobs use macOS launchd. Plists live in `scheduling/`. Most use `scripts/jobs/job_wrapper.sh` which sends a macOS notification on failure.

| Schedule | Job | Script | Plist | Installed | Status |
|---|---|---|---|---|---|
| Sat 1:00 AM | Bookmark scrape | `scrape_bookmarks.py` | `com.second-brain.bookmarks` | ✅ | Working |
| Daily 1:30 AM | YouTube scrape | `youtube_scrape.sh` | `com.second-brain.youtube` | ✅ | Working |
| Daily 2:00 AM | Backup | `backup.sh` | `com.second-brain.backup` | ✅ | Working |
| Daily 2:30 AM | Chat extraction | `chat_extract.py` | `com.second-brain.chat-extract` | ✅ | Exit 126 (permission) |
| Daily 3:00 AM | Staged ingestion | `ingest_staged.sh` | `com.second-brain.ingest` | ✅ | Working |
| Hourly | QD sync | `qd_sync.sh` | `com.second-brain.qd-sync` | ✅ | Working |
| Sun 3:00 AM | Backup verify | `verify_backup.sh` | `com.second-brain.verify` | ✅ | Working |
| Sun 4:00 AM | Dream cycle | `dream_cycle_scheduled.sh` | `com.second-brain.dream-cycle` | ❌ | Not installed |
| — | Capture API | `capture_api.sh` | `com.second-brain.capture-api` | ❌ | Not installed |

### Installing a LaunchAgent

```bash
# Symlink and load
ln -s ~/second-brain/scheduling/com.second-brain.<name>.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.second-brain.<name>.plist

# Verify
launchctl list | grep second-brain
```

### Checking Job Health

```bash
# All loaded jobs and exit codes (0 = success)
launchctl list | grep second-brain

# Recent logs
cat ~/second-brain/logs/<job-name>-$(date +%Y%m%d).log

# QD sync state
cat ~/.quickwork/.second_brain_sync_state.json
```

## Backup

Daily at 2 AM. Full details in [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md).

| What | Where | Retention |
|---|---|---|
| pg_dump (all tables) | Google Drive + S3 | 30 days cloud, 7 days local |
| JSON exports (memories, entities, edges, memory_entities) | Google Drive + S3 | 30 days cloud, 7 days local |
| KB source docs | Google Drive + S3 | Current only |
| Config (migrations, docker-compose, DR doc) | Google Drive + S3 | Current only |

The backup script (`scripts/jobs/backup.sh`) checks AWS SSO token validity before S3 upload and attempts auto-refresh. If SSO is expired and can't be refreshed, S3 is skipped with a warning — Google Drive still runs.

## AWS SSO Management

The S3 backup leg and Bedrock embedding calls depend on AWS SSO credentials.

```bash
# Check token status
aws sts get-caller-identity

# Refresh (opens browser for authorization)
aws sso login --profile default

# Profile: default → SSO session kiro-admin-session
# Start URL: https://d-9066306d67.awsapps.com/start
# Account: 048588987132, AdministratorAccess role
```

SSO tokens expire after 8-12 hours. When expired:
- S3 backups fail (Google Drive unaffected)
- Bedrock embedding calls fail (affects memory_search, integration tests)
- The backup script attempts auto-refresh but this requires an active desktop session

## Quick Desktop Sync

Hourly incremental sync from Quick Desktop's SQLite database (`~/.quickwork/knowledge_v1.db`) into PostgreSQL.

- **What syncs:** memories, KG entities, KG edges, memory↔entity bridge links
- **Dedup:** via `source_url` using `qd://memory/{id}` and `qd://entity/{id}` scheme
- **State file:** `~/.quickwork/.second_brain_sync_state.json` (tracks `last_memory_id`, `last_entity_id`, `last_kg_entity_id`)
- **Script:** `scripts/migrate/migrate_quick_desktop.py` (incremental by default, `--full` for backfill)

See [QUICK-DESKTOP-INTEGRATION.md](QUICK-DESKTOP-INTEGRATION.md) for design details.

## Docker

PostgreSQL 17 + pgvector runs in Docker. Compose file at project root.

```bash
# Start
docker compose up -d

# Check health
docker exec second-brain-db pg_isready -U memory_bank

# Apply migrations
./migrations/migrate.sh

# Data volume
docker/data/   # ~2.5 GB
```

## Monitoring Checklist

| Check | Command | Expected |
|---|---|---|
| All jobs loaded | `launchctl list \| grep second-brain` | 7 entries, exit code 0 |
| Backup ran today | `cat logs/backup-$(date +%Y%m%d).log` | "Backup complete" |
| QD sync running | `cat logs/qd-sync-$(date +%Y%m%d).log` | Hourly entries |
| AWS SSO valid | `aws sts get-caller-identity` | Returns account info |
| PostgreSQL up | `docker exec second-brain-db pg_isready -U memory_bank` | "accepting connections" |
| Memory count | `docker exec second-brain-db psql -U memory_bank -c "SELECT count(*) FROM memories"` | 74K+ |
