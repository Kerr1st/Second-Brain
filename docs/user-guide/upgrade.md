---
title: "Upgrade Second Brain"
type: how-to
---

# Upgrade Second Brain

Safely upgrade a running instance to the latest version: pull code, apply migrations, update dependencies, and restart services.

## Prerequisites

> [!NOTE]
> Before you begin, ensure you have:
> - A working Second Brain installation (see [Getting started](getting-started.md))
> - PostgreSQL 17 running (`pg_isready -h 127.0.0.1 -p 5432 -U memory_bank`)
> - The `.venv` activated (`source .venv/bin/activate`)

## 1. Review recent changes

Skim the commit log or changelog for breaking changes before upgrading:

```bash
git log --oneline HEAD..origin/main | head -20
```

Look for migration files, dependency changes, or notes about config format updates.

> [!TIP]
> Nightly backups run at 2:00 AM (Google Drive + local, encrypted). A manual pre-upgrade backup is optional — your data is already protected. If you want one anyway: `bash scripts/jobs/backup.sh`

## 2. Pull the latest code

```bash
git pull origin main
```

Expected output:

```text
Updating a1b2c3d..e4f5g6h
Fast-forward
 src/mcp_server.py | 12 ++++++------
 migrations/011_new_index.sql | 8 ++++++++
 ...
```

## 3. Apply new migrations

The migration runner is idempotent — it skips already-applied versions and is safe to re-run:

```bash
./migrations/migrate.sh
```

Expected output:

```text
skip: 001_initial_schema.sql (already applied)
skip: 002_v2_columns.sql (already applied)
...
apply: 011_new_index.sql
done
```

If all migrations were already applied, you see only `skip:` lines followed by `done`.

## 4. Reinstall dependencies (if changed)

Check whether `requirements.txt` changed in the pull:

```bash
git diff HEAD~1 -- requirements.txt
```

If it shows changes, reinstall:

```bash
pip install -r requirements.txt
```

Expected output (last line):

```text
Successfully installed <new-or-updated-packages>
```

## 5. Restart services (if needed)

Restart PostgreSQL only if the upgrade notes mention a schema change that requires it, or if you experience connection issues:

```bash
brew services restart postgresql@17
```

Expected output:

```text
Stopping `postgresql@17`... (was running)
==> Successfully started `postgresql@17`
```

If any *launchd agents* were updated (new or modified plists in `scheduling/`), reload them:

```bash
launchctl unload ~/Library/LaunchAgents/com.second-brain.<name>.plist
launchctl load ~/Library/LaunchAgents/com.second-brain.<name>.plist
```

Verify all jobs are loaded:

```bash
launchctl list | grep second-brain
```

Expected output:

```text
-    0    com.second-brain.backup
-    0    com.second-brain.dream-cycle
...
```

## 6. Verify

### Check PostgreSQL

```bash
pg_isready -h 127.0.0.1 -p 5432 -U memory_bank
```

Expected output:

```text
127.0.0.1:5432 - accepting connections
```

### Run the smoke test

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_search.py tests/test_mcp_server.py -q
```

Expected output:

```text
... passed
```

Tests run against an isolated `memory_bank_test` database and mock Ollama calls — the local model
does not need to run during tests.

## Related

- [Operations](operations.md) — scheduled jobs, backups, monitoring
- [Troubleshooting](troubleshoot.md) — fixes for common post-upgrade issues
- [Getting started](getting-started.md) — full installation procedure
- [Disaster Recovery](../DISASTER-RECOVERY.md) — backup and restore details
