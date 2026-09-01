# Second Brain — Operations

> Last updated: 2026-08-29

## Model Backend Profile

The dream cycle's LLM execution path is selected by a named profile in `config/backends.toml` via the `SECOND_BRAIN_PROFILE` environment variable. **Unset (the default) = the `laptop` profile**, which runs every role through Kiro CLI with Claude Opus 4.8 under the current Kiro plan — i.e. today's behavior. Other profiles (e.g. `mini`) select an alternative backend; selecting a profile whose adapter isn't built yet fails fast with a clear error. Credentials never live in the TOML. See `docs/MODEL-BACKENDS.md`.

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
| Hourly | Codex Task capture | `codex_capture.sh` | `com.second-brain.codex-capture` | Working; active User-Owned Tasks only |
| Hourly | Local vector fill | `reembed_local.sh` | `com.second-brain.reembed-local` | Temporary; resumable backfill |
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

## Local Embedding Runtime

The active embedding space is local Ollama BGE-M3 (`ollama:bge-m3:1024`). Install and verify it
once on each machine:

```bash
brew install ollama
brew services start ollama
ollama pull bge-m3

# Verify runtime, model, dimension, and active space
curl -sS http://127.0.0.1:11434/api/version
ollama list
.venv/bin/python -c \
  'from src.embeddings import generate_embedding, active_embedding_space; v=generate_embedding("health check"); print(active_embedding_space(), len(v))'
```

Expected final output is `ollama:bge-m3:1024 1024`. The Titan Adapter is retained in code for
explicit legacy diagnostics but cannot be selected through the active embedding Interface; this
fails closed before incompatible vectors can enter the local space.

After migration 014, Titan vectors remain in `memories.legacy_embedding`; active local vectors use
`memories.embedding` with `memories.embedding_space`. Fill the local space incrementally:

```bash
# Count eligible preserved rows without writes
.venv/bin/python scripts/reembed_memories.py --dry-run

# Prove a small resumable batch, then continue in bounded runs
.venv/bin/python scripts/reembed_memories.py --limit 100 --batch-size 32
.venv/bin/python scripts/reembed_memories.py --limit 5000 --batch-size 32
```

Each batch commits independently. Re-running skips completed rows. Do not drop
`legacy_embedding` until the retrieval evaluation passes and retirement is explicitly approved.
The command prioritizes decisions, insights, syntheses, and other derived memories before raw
`source` rows so useful semantic recall recovers early in a gradual migration.

## AWS SSO Management

AWS SSO is no longer required for embeddings or backups. It remains relevant only for a model
execution profile explicitly configured to call Bedrock.

```bash
aws sts get-caller-identity
aws sso login --profile default

# Profile: default → SSO session <sso-session-name>
# Start URL: <aws-sso-start-url>
# Account: <aws-account-id>, AdministratorAccess role
```

SSO tokens expire after 8-12 hours. When expired:
- Bedrock-backed model generation fails for profiles that select it
- local embedding, ingestion, and `memory_search` remain available through Ollama
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

## Codex Desktop Task Capture

Codex capture has one command and one implementation path. The hourly
`com.second-brain.codex-capture` LaunchAgent runs it for every active User-Owned Task. Delegated
Tasks and Unknown-Ownership Tasks are skipped and reported. Archived history remains excluded, and
the full historical backfill has not been run:

```bash
# Read and count eligible active Tasks without database, model, or embedding writes
.venv/bin/python scripts/capture_codex.py --dry-run

# Inspect one Task, including an archived Task, without writes
.venv/bin/python scripts/capture_codex.py --dry-run --task-id <thread-id>

# Run the narrowest real-data proof against the isolated test database
set -a
source .env.codex-dev
set +a
DB_NAME="$TEST_DB_NAME" .venv/bin/python scripts/capture_codex.py \
  --task-id <thread-id>
```

On a machine using the bundled Codex executable for the semantic pass, add:

```bash
SECOND_BRAIN_PROFILE=codex_local \
CODEX_CLI=/Applications/ChatGPT.app/Contents/Resources/codex \
DB_NAME="$TEST_DB_NAME" .venv/bin/python scripts/capture_codex.py \
  --task-id <thread-id>
```

The command applies the six-hour inactivity threshold, performs a stable rollout read, captures
only user prompts and visible final answers, and runs the combined Task Semantic Pass immediately
after source capture. Its JSON report contains counts plus Task IDs, failure stages, and exception
class names when failures occur; it does not print captured prompt or answer content.

`--backfill` includes archived eligible Tasks through this same path. Do not invoke an unbounded
`--backfill` until explicit user approval. Task-bounded proof runs still use `--task-id` with
`DB_NAME="$TEST_DB_NAME"`; there is no separate pilot application. See
[CODEX-TASK-CAPTURE-BUILD-PLAN.md](CODEX-TASK-CAPTURE-BUILD-PLAN.md).

### Hourly active-task capture

The production job uses `scheduling/com.second-brain.codex-capture.plist`. It passes neither
`--task-id` nor `--backfill`, so the normal six-hour and Task Ownership policies select all and only
eligible active Tasks.

```bash
# Install the repository template for this checkout, then load it
sed "s|/path/to/second-brain|$PWD|g" \
  scheduling/com.second-brain.codex-capture.plist \
  > "$HOME/Library/LaunchAgents/com.second-brain.codex-capture.plist"
launchctl bootstrap "gui/$UID" \
  "$HOME/Library/LaunchAgents/com.second-brain.codex-capture.plist"

# Inspect status and the append-only local log
launchctl print "gui/$UID/com.second-brain.codex-capture"
tail -50 logs/codex-capture.log
```

The job checks the local Ollama runtime and required BGE-M3 model. If either is unavailable it
records `waiting_for_local_embedding` and exits successfully. Once the local runtime returns, the
next hourly run retries eligible captures and unchanged semantic tails. The former one-task canary
is retained as proof evidence but is not loaded alongside the production job.

## Memory Context Broker and Steering Governance

Agents request a bounded pack through `memory_context` and close its receipt through
`memory_context_outcome`. Receipt outcomes are `followed`, `corrected`, `not_used`, or `unknown`;
`corrected` requires a Correction Episode ID.

Steering changes use a review-before-write command sequence:

```bash
# Four independent evaluators; acceptance retains an inactive candidate
.venv/bin/python scripts/steering.py review \
  --title "<candidate title>" \
  --wording "<proposed rule>" \
  --source-memory-id <evidence-id> \
  --proposed-scope project \
  --applicability '{"semantic_projects":["second-brain"]}'

# Explicit user approval creates a versioned active rule
.venv/bin/python scripts/steering.py approve <candidate-id> \
  --wording "<approved rule>" \
  --scope project \
  --applicability '{"semantic_projects":["second-brain"]}'

# Preview first; the first output line is the current-file digest
.venv/bin/python scripts/steering.py preview-agents <rule-id> --path AGENTS.md

# Publish only the exact reviewed version
.venv/bin/python scripts/steering.py publish-agents <rule-id> --path AGENTS.md \
  --expected-current-digest <reviewed-digest>
```

Publication accepts only an active approved Steering Rule, rejects symlinks and non-`AGENTS.md`
targets, writes atomically, and keeps an ignored local rollback copy under
`.second-brain-backups/`. It never installs hooks, skills, tests, or CI automatically.

The 2026-08-29 Codex-first proof created context receipt
`c15a25c2-6a0b-4304-89a7-63be33667939`, delivered four items to follow-up Codex Task
`01a04e4c-4a73-7611-ade6-ad828caaf87b`, and recorded all four as used with outcome `followed`.
No rule correction was proposed.

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
