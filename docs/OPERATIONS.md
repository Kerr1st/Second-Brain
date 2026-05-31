# Second Brain — Operations

> Last updated: 2026-05-17

## Scheduled Jobs

All jobs use macOS launchd. Plists live in `scheduling/`. Most use `scripts/jobs/job_wrapper.sh` which sends a macOS notification on failure.

| Schedule | Job | Script | Plist | Status |
|---|---|---|---|---|
| Sat 1:00 AM | Bookmark scrape | `scrape_bookmarks.py` | `com.second-brain.bookmarks` | Working |
| Daily 1:30 AM | YouTube scrape | `youtube_scrape.sh` | `com.second-brain.youtube` | Working |
| Daily 2:00 AM | Backup | `backup.sh` | `com.second-brain.backup` | Working |
| Daily 2:30 AM | Chat extraction | `chat_extract.py` | `com.second-brain.chat-extract` | Working |
| Daily 3:00 AM | Staged ingestion | `ingest_staged.sh` | `com.second-brain.ingest` | Working |
| Hourly | QD sync | `qd_sync.sh` | `com.second-brain.qd-sync` | Working |
| Sun 3:00 AM | Backup verify | `verify_backup.sh` | `com.second-brain.verify` | Working |
| Sun 4:00 AM | Dream cycle | `dream_cycle_scheduled.sh` | `com.second-brain.dream-cycle` | Working |
| KeepAlive | Capture API | `capture_api.sh` | `com.second-brain.capture-api` | Working (port 8100) |

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
| Config (all docs, migrations, docker-compose, requirements.txt) | Google Drive + S3 | Current only |

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

Hourly incremental sync from Quick Desktop's SQLite databases into PostgreSQL. The `qd_sync.sh` orchestrator runs 5 scripts in sequence:

| Script | What it syncs | Source |
|---|---|---|
| `migrate_quick_desktop.py` | Memories + KG entities/edges | `knowledge_v1.db` |
| `enrich_qd_tags.py` | Tags (2,506) + domains (95) onto memories | `knowledge_v1.db` |
| `ingest_eventlog.py` | Feed events (304) + interaction metadata (175) | `eventlog/` |
| `import_slack_graph.py` | Channels (330) + users (699) → KG entities | `slack_cache/` |
| `ingest_qd_chats.py` | Chat sessions (2+ messages) | `sessions.db` |

**Databases:**
- `~/.quickwork/knowledge_storage/knowledge_v1.db` — memories, KG, tags, domains
- `~/.quickwork/sessions/sessions.db` — chat sessions
- `~/.quickwork/eventlog/events.jsonl` — curated feed events
- `~/.quickwork/eventlog/interactions.jsonl` — user engagement signals
- `~/.quickwork/slack_cache/` — channels and users

**Dedup schemes:** `qd://memory/{id}`, `qd://entity/{id}`, `qd-chat://{id}`, `qd-feed://{id}`

**State file:** `~/.quickwork/.second_brain_sync_state.json`

All scripts are idempotent — safe to run repeatedly. See [QUICK-DESKTOP-INTEGRATION.md](QUICK-DESKTOP-INTEGRATION.md) for design details.

## Capture API

HTTP endpoint for ingesting content from Slack, browser extensions, mobile, and email. Runs as a KeepAlive LaunchAgent on port 8100.

```bash
# Health check
curl http://localhost:8100/health

# Submit a memory
curl -X POST http://localhost:8100/capture \
  -H "Authorization: Bearer $CAPTURE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "...", "content": "...", "type": "insight"}'
```

Endpoints: `/capture`, `/capture/slack`, `/capture/browser`, `/capture/email`

Token is set in the LaunchAgent plist (`com.second-brain.capture-api.plist`) as the `CAPTURE_API_TOKEN` environment variable. Logs at `~/Library/Logs/second-brain/capture-api-{stdout,stderr}.log`.

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
| All jobs loaded | `launchctl list \| grep second-brain` | 9 entries, exit code 0 |
| Capture API healthy | `curl -s http://localhost:8100/health` | `{"status":"healthy"}` |
| Backup ran today | `cat logs/backup-$(date +%Y%m%d).log` | "Backup complete" |
| QD sync running | `cat logs/qd-sync-$(date +%Y%m%d).log` | Hourly entries |
| AWS SSO valid | `aws sts get-caller-identity` | Returns account info |
| PostgreSQL up | `docker exec second-brain-db pg_isready -U memory_bank` | "accepting connections" |
| Memory count | `docker exec second-brain-db psql -U memory_bank -c "SELECT count(*) FROM memories"` | 164K+ |
