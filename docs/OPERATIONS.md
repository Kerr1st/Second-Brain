# Second Brain — Operations

> Last updated: 2026-06-16

## Model Backend Profile

The dream cycle's LLM execution path is selected by a named profile in `config/backends.toml` via the `SECOND_BRAIN_PROFILE` environment variable. **Unset (the default) = the `laptop` profile**, which runs every role on `kiro-cli` → Amazon Q (Claude Opus 4.8), $0 metered — i.e. today's behavior. Other profiles (e.g. `mini`) select an alternative backend; selecting a profile whose adapter isn't built yet fails fast with a clear error. Credentials never live in the TOML. See `docs/MODEL-BACKENDS.md`.

## Scheduled Jobs

All jobs use macOS launchd. Plists live in `scheduling/`. Most use `scripts/jobs/job_wrapper.sh` which sends a macOS notification on failure. (The list below is the primary set; `scheduling/` also holds `distill`, `weekly-digest`, and `liveness` jobs — see that directory for the authoritative set and exact schedules.)

| Schedule | Job | Script | Plist | Status |
|---|---|---|---|---|
| Sat 1:00 AM | Bookmark scrape | `scrape_bookmarks.py` | `com.second-brain.bookmarks` | Working |
| Daily 1:30 AM | YouTube capture | `youtube_scrape.sh` → `src/capture/youtube.py` | `com.second-brain.youtube` | Working (self-contained yt-dlp) |
| Daily 2:00 AM | Backup | `backup.sh` | `com.second-brain.backup` | Working |
| Daily 2:30 AM | Chat extraction | `chat_extract.py` | `com.second-brain.chat-extract` | Working |
| Daily 3:00 AM | Staged ingestion | `ingest_staged.sh` | `com.second-brain.ingest` | Working |
| Hourly | QD sync | `qd_sync.sh` | `com.second-brain.qd-sync` | Working |
| Sun 3:00 AM | Backup verify | `verify_backup.sh` | `com.second-brain.verify` | Working |
| (see plist) | Dream cycle | `dream_cycle_scheduled.sh` | `com.second-brain.dream-cycle` | Working |
| Removed | Capture API | `capture_api.sh` | Not installed | Deprecated endpoint retained only for historical reference |

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
| pg_dump (all tables) | Google Drive + local | 30 days cloud, 7 days local |
| JSON exports (memories, entities, edges, memory_entities) | Google Drive + local | 30 days cloud, 7 days local |
| KB source docs | Google Drive + local | Current only |
| Config (all docs, migrations, docker-compose, requirements.txt) | Google Drive + local | Current only |

**AWS S3 was de-scoped on 2026-06-01** (brittle overnight SSO). Durable copies are now Google Drive + local (code/config also in git on `origin`/`mini`). `scripts/jobs/backup.sh` no longer uploads to S3. The JSON exports still include `entities`/`edges`/`memory_entities` (the now-dormant KG tables) pending cleanup.

## AWS SSO Management

Bedrock embedding/generation calls depend on AWS SSO credentials. (The S3 backup leg was de-scoped 2026-06-01, so backups no longer depend on SSO — only embedding and the dream cycle's LLM calls do.)

```bash
# Check token status
aws sts get-caller-identity

# Refresh (opens browser for authorization)
aws sso login --profile default

# Profile: default → SSO session <sso-session-name>
# Start URL: <aws-sso-start-url>
# Account: <aws-account-id>, AdministratorAccess role
```

SSO tokens expire after 8-12 hours. When expired:
- Bedrock embedding/generation calls fail (affects ingestion, `memory_search`, the dream cycle, integration tests)
- Backups are unaffected (Google Drive + local; S3 de-scoped)

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

## Capture API — DEPRECATED

**Status: unused, slated for removal.** The HTTP capture endpoint (`src/capture_api.py`; Slack/browser/mobile/email on port 8100) has captured **0 memories** in its lifetime. Its launchd job has been removed; remaining code is retained only for historical reference. In-session capture goes through the MCP `memory_create` tool; new ingestion follows the capture component (`docs/CAPTURE-COMPONENTS.md`).

## Express (briefing & feedback)

The delivery layer that surfaces what the system has synthesized. The on-demand
`brief` is the pull surface; a high-bar Gmail push (`express_push.py`) is chained
after the noon dream cycle; the `memory_brief` MCP tool surfaces it in-session.

```bash
# Read the briefing
.venv/bin/python scripts/brief.py                # LLM-edited headlines
.venv/bin/python scripts/brief.py --no-llm       # fast deterministic ranking
.venv/bin/python scripts/brief.py --window-days 30

# Shape what it surfaces (delivery preferences — gradient)
.venv/bin/python scripts/brief.py --useful <target>   # boost
.venv/bin/python scripts/brief.py --less   <target>   # soft down-weight
.venv/bin/python scripts/brief.py --mute   <target>   # hard hide
.venv/bin/python scripts/brief.py --unmute <target>   # clear a preference
.venv/bin/python scripts/brief.py --prefs             # list preferences

# Proactive email (gated; sends only on a cross-project synthesis or contradiction)
.venv/bin/python scripts/express_push.py --dry-run    # compose + print, never send
```

`<target>` is an item id (the `#abcd1234` shown by `brief`), a kind
(`insight|contradiction|resurface|digest|question`), or a topic/project name.
Run `scripts/brief.py --help` for the full reference.

Email config (never committed) — set for the LaunchAgent that runs the chained push:
`EXPRESS_EMAIL_TO`, `EXPRESS_EMAIL_FROM`, `GMAIL_APP_PASSWORD`. Until set, the push
composes but skips sending (benign exit 0).

## Database

PostgreSQL 17 + pgvector runs **natively** via Homebrew (`postgresql@17`), serving `127.0.0.1:5432` (localhost-only), auto-started at login by `brew services`.

```bash
# Start / restart (auto-starts at login)
brew services start postgresql@17
brew services restart postgresql@17

# Check health
pg_isready -h 127.0.0.1 -p 5432 -U memory_bank

# Connect
psql -h 127.0.0.1 -U memory_bank -d memory_bank

# Apply migrations
./migrations/migrate.sh

# Data dir
/opt/homebrew/var/postgresql@17
```

> `postgresql@17` is keg-only — add `/opt/homebrew/opt/postgresql@17/bin` to `PATH` (or use full paths) for `psql`/`pg_dump`/`pg_isready`.
>
> The former Docker container (`second-brain-db`, `pgvector/pgvector:pg17`) is **stopped but retained as a migration rollback** until Phase 5 decommission; its bind-mount data is at `docker/data/` (~2.5 GB). See `docs/MIGRATION-native-postgres.md`.

## Monitoring Checklist

| Check | Command | Expected |
|---|---|---|
| All jobs loaded | `launchctl list \| grep second-brain` | jobs present, exit code 0 |
| Backup ran today | `cat logs/backup-$(date +%Y%m%d).log` | "Backup complete" |
| QD sync running | `cat logs/qd-sync-$(date +%Y%m%d).log` | Hourly entries |
| AWS SSO valid | `aws sts get-caller-identity` | Returns account info |
| PostgreSQL up | `pg_isready -h 127.0.0.1 -p 5432 -U memory_bank` | "accepting connections" |
| Memory count | `psql -h 127.0.0.1 -U memory_bank -d memory_bank -c "SELECT count(*) FROM memories"` | ~122K |
