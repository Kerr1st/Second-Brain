# Operations — original implementation

Source-reviewed 2026-09-10. Use [Start here](START-HERE.md) to identify the checkout and current operational evidence. This runbook does not start services, resume schedules, install software, or grant authority to send email just by existing.

## Read-only orientation first

From the intended checkout, inspect `git rev-parse HEAD` and `git status --short`; preserve existing backend work. Read the selected profile and override names without dumping secret-bearing files. Check the DB target, installed/loaded/disabled schedules, source freshness, and latest substantive output separately. Avoid interpreting `launchctl` exit 0 as successful data processing.

Useful bounded inspection commands:

```bash
git rev-parse HEAD
git status --short
/opt/homebrew/opt/postgresql@17/bin/pg_isready -h 127.0.0.1 -p 5432
launchctl list | rg 'second-brain|postgresql'
launchctl print-disabled "gui/$(id -u)"
```

Inspect only relevant log tails; logs/debug responses can contain private content. For source counts/schema checks, use an explicit read-only DB transaction and roll it back. `memory_search` is **not** a read-only inspection because it updates retrieval counts. `memory_list`/`memory_read` are suitable narrow probes once the right DB is selected.

## Fresh isolated setup

The code is a Python repository installed from `requirements.txt`, not a packaged application. `tomllib` requires Python 3.11+; the selected Python version must also satisfy pinned dependencies. PostgreSQL 17 with compatible pgvector is the intended engine; filtered HNSW uses pgvector iterative-scan settings. Do not repurpose an existing production data directory.

1. Create a dedicated checkout/environment and separate PostgreSQL cluster/port with a task-specific directory. Install requirements into that checkout's virtual environment.
2. Create the test role/database using the cluster administrator. Configure `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` before Python imports. No `.env` loader is implemented automatically.
3. Apply reviewed SQL to the isolated target with explicit stop-on-error handling. **Do not use `migrations/migrate.sh` for a nondefault-port test:** it hardcodes 127.0.0.1:5432, and migration 009 explicitly alters database `memory_bank` regardless of `DB_NAME`. Rewrite/substitute that one database-level statement only inside the disposable fixture, and record it as test setup.
4. Compare required columns/indexes against migrations 000–011 and store the exact schema evidence. The migration ledger can be incomplete even when changes were manually applied; inspect both.
5. Verify providers separately per [Model backends](MODEL-BACKENDS.md). Use synthetic nonprivate content for access probes. Keep production source state, schedules, and output destinations unavailable to this fixture.
6. Connect MCP and exercise the synthetic record flow in [Interfaces](INTERFACES.md). No schedule installation is needed for this gate.

`python -m venv .venv` and `.venv/bin/python -m pip install -r requirements.txt` install dependencies; they do not provision DB roles, migrations, model credentials, or source integrations. Old “15-minute setup” and “safe to rerun” promises are retired.

## MCP connection

The server entrypoint is `.venv/bin/python -m src.mcp_server` with the repository root as working directory. It waits for a stdio client; silence at a terminal is normal, not a connectivity proof.

A generic JSON client descriptor is:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/absolute/path/to/second-brain/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/absolute/path/to/second-brain"
    }
  }
}
```

Use the installed client's supported registration format; not every client accepts this exact JSON shape or `cwd` key. Code-owned role adapters supply their own config. `scripts/jobs/mcp_serve.sh` uses system `python3`, while the adapters use the invoking interpreter; confirm the interpreter has project dependencies. A client UI listing the server is weaker evidence than a completed `memory_list` tool call.

## Schedule templates

There are **12 committed plist templates**. Times are local wall-clock schedule intent; launchd may execute after wake rather than exactly at that time. Template, installed file, loaded job, disabled override, last exit, and successful output are different facts.

| Label suffix | Template schedule | Entrypoint |
|---|---|---|
| `bookmarks` | Saturday 01:00 | `scripts/scrape_bookmarks.py` |
| `youtube` | Daily 01:30 | `scripts/jobs/youtube_scrape.sh` → `src.capture.youtube` |
| `backup` | Daily 02:00 | `scripts/jobs/backup.sh` |
| `chat-extract` | Daily 02:30 | `scripts/chat_extract.py` |
| `ingest` | Daily 03:00 | `scripts/jobs/ingest_staged.sh` |
| `verify` | Sunday 03:00 | `scripts/jobs/verify_backup.sh` |
| `qd-sync` | Every 3600 seconds; RunAtLoad=false | `scripts/jobs/qd_sync.sh` (six steps) |
| `distill` | Daily 05:00 | `scripts/distill_sessions.py` |
| `weekly-digest` | Monday 06:00 | `scripts/weekly_digest.py` |
| `liveness` | Daily 09:00 | `scripts/jobs/verify_liveness.sh` |
| `dream-cycle` | Daily 12:00 | `scripts/jobs/dream_cycle_scheduled.sh` |
| `capture-api` | KeepAlive | `scripts/jobs/capture_api.sh` |

All labels have `com.second-brain.` prefix. Most jobs use `job_wrapper.sh` for dated logs, exit reporting, notifications and 30-day `.log` cleanup. QD sync and capture API have separate logs. Many templates/wrappers hardcode the original user's checkout path. Copying them to another host without path/environment review is not deployment.

The dream-cycle wrapper may start PostgreSQL, runs synthesis, then invokes the email push **even when synthesis failed**; it retains only the dream-cycle exit code and ignores delivery failure. Staged ingestion explicitly exits 0 after logging failures. Several Python import/distill commands report failed counts but also exit 0. Liveness inspects maximum insertion date for selected source groups; it cannot prove complete capture, semantic accuracy, delivery, or all-surface coverage.

Preserve disabled jobs during investigation. If later activating a specific reviewed job, install a machine-specific copy, verify paths/interpreter/environment/destination, enable only that intended label, and observe its produced records. Do not mass-load every template: capture API contains a committed credential and synthesis/delivery jobs can make model calls or send email.

## Manual stage entrypoints

Run write/model commands only against the deliberately selected environment and data scope.

| Operation | Entrypoint and caveat |
|---|---|
| Extraction preview | `scripts/chat_extract.py --dry-run`; may still create log/staging directories and inspect DB skip state |
| Staged import preview | `scripts/batch_ingest_staged.py --dry-run --limit N`; no embedding model, but reads target DB |
| YouTube preview | `python -m src.capture.youtube --dry-run --limit N`; still enumerates remote playlists/cookies |
| Distillation | `scripts/distill_sessions.py --limit N`; `--dry-run` still sends transcripts to Kiro |
| Dream cycle | `scripts/dream_cycle_run.py --run-type user_triggered --topic '<task>'`; has no dry-run, writes DB/history and invokes providers |
| Briefing | `scripts/brief.py --no-llm`; DB composition/render only |
| Email preview | `scripts/express_push.py --dry-run --no-llm`; gated composition, no SMTP send |
| Delivery preference | `scripts/brief.py --useful/--less/--mute/--unmute <target>`; writes preferences |
| Retrieval evaluation | `scripts/eval/run_evaluation.py`; calls embedding provider and writes reports; `--dry-run` only lists tiers |
| Reindex / dedup / backfill | `scripts/jobs/reindex_embedding.sh`, `dedup_ide_backfill.sh`, `scripts/backfill_projects.py`, `redistill_capped.py`; maintenance changes require separate reviewed scope |

## Troubleshooting by boundary

| Symptom | Inspect and interpret |
|---|---|
| DB refuses connections | Actual host/port, cluster process/socket, auth method; don't auto-start a competing cluster on 5432 |
| Missing relation/column | Actual schema and ledger versus code; source migrations alone are not proof applied |
| Embedding failures | Boto3 credentials, region, exact model permission and request schema; Kiro login alone cannot fix this |
| Kiro not logged in | Reasoning CLI authentication; independent of Python AWS credentials |
| Zero slices / MCP startup | Working directory, interpreter, selected profile, actual tool results; empty Explorer aborts with CLI exit 2 |
| No candidates but exit 0 | Thinker failures can be skipped. Inspect stage errors rather than assuming “no useful ideas” |
| Zero new captured rows | Source missing/idle/moved, filters, stale ID skips, partial prior parent, watermark advancement; inspect source and result completeness |
| Useful candidates never surfaced | Panel source blindness, dedup gates,600-character truncation, muted prefs, email gate/composition mismatch |
| No Gmail delivery | Gate decision, credential presence, SMTP result and state file; benign skip 0 is not delivery success |
| Backup says success | Verify an actual current dump, decryption, restore in isolated DB, and source/config coverage; see Recovery |

## Upgrade/cutover boundary

Before adopting any change: preserve current code/config/state, create a verified restorable backup, compare schema requirements, record selected provider and model, run isolated component/full-loop checks, then make a reversible cutover plan. The experimental lineage is not an ordinary `git pull` upgrade of the original repository. No migration, provider change, cleanup, or cutover was performed in this documentation pass.
