---
title: "Troubleshooting"
type: how-to
---

# Troubleshooting

Diagnose and fix common Second Brain issues. Each section follows: symptom → likely cause → fix.

## Prerequisites

> [!NOTE]
> Before troubleshooting, ensure you have:
> - Terminal access with the `.venv` activated (`source .venv/bin/activate`)
> - PostgreSQL 17 binaries on your PATH (`/opt/homebrew/opt/postgresql@17/bin`)

## Where to find logs

All *launchd job* logs write to `logs/<job-name>-YYYYMMDD.log` inside the project directory:

```bash
ls ~/second-brain/logs/
```

Common log files:

| Job | Log path |
|-----|----------|
| Backup | `logs/backup-YYYYMMDD.log` |
| Dream cycle | `logs/dream-cycle-YYYYMMDD.log` |
| QD sync | `logs/qd-sync-YYYYMMDD.log` |
| Ingestion | `logs/ingest-YYYYMMDD.log` |
| Backup verify | `logs/verify-YYYYMMDD.log` |

Check today's log for any job:

```bash
cat ~/second-brain/logs/<job-name>-$(date +%Y%m%d).log
```

The `job_wrapper.sh` sends a macOS notification on failure — check Notification Center if you missed it.

## PostgreSQL not running

**Symptom:** `pg_isready` reports "no response" or "could not connect"; MCP tools fail with connection errors.

**Likely cause:** PostgreSQL was not started after a reboot, or it crashed.

**Fix:**

```bash
brew services start postgresql@17
```

Expected output:

```text
==> Successfully started `postgresql@17`
```

Verify:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U memory_bank
```

Expected output:

```text
127.0.0.1:5432 - accepting connections
```

> [!TIP]
> `brew services` auto-starts PostgreSQL at login. If it repeatedly fails to start, check the Homebrew log: `cat /opt/homebrew/var/log/postgresql@17.log`

## PostgreSQL not accepting connections

**Symptom:** `pg_isready` returns "rejecting connections" or the MCP server logs `FATAL: role "memory_bank" does not exist`.

**Likely cause:** The `memory_bank` role or database was not created, or PostgreSQL is in recovery mode.

**Fix — missing role/database:**

```bash
createuser -s memory_bank 2>/dev/null || true
createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**Fix — recovery mode:** Restart the service:

```bash
brew services restart postgresql@17
```

## AWS SSO token expired

**Symptom:** A Bedrock-backed *dream cycle* model profile fails with `ExpiredTokenException` or
`UnauthorizedSSOTokenError`.

**Likely cause:** SSO tokens expire after 8–12 hours.

**Fix:**

```bash
aws sso login --profile default
```

This opens your browser for authorization. Verify the session:

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
> Refresh SSO only when using a Bedrock-backed reasoning profile. Local embedding, search,
> ingestion, and backups are unaffected.

## MCP server won't connect from the agent

**Symptom:** Your *agent* (Kiro CLI, Claude Code, or another MCP client connected to the MCP server) cannot find or start the MCP server. Errors mention "spawn failed", "ENOENT", or "tool not found".

**Likely cause:** The `cwd` or `command` path in your MCP client config is wrong, or the `.venv` Python path is incorrect.

**Fix:** Verify your MCP client configuration points to the correct paths:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<you>/second-brain/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/Users/<you>/second-brain"
    }
  }
}
```

Check that the paths exist:

```bash
ls /Users/<you>/second-brain/.venv/bin/python
ls /Users/<you>/second-brain/src/mcp_server.py
```

> [!NOTE]
> The `command` must be the absolute path to the `.venv` Python — not a system Python. The `cwd` must be the project root so relative imports resolve. See [Connect an AI agent](connect-ai-agent.md) for full setup instructions.

## Local embedding failures

**Symptom:** `memory_create` or `memory_search` reports a connection error for
`127.0.0.1:11434`, a missing `bge-m3` model, or an invalid vector dimension.

**Likely cause:** Ollama is stopped, BGE-M3 has not been pulled, or the configured local model does
not match the required 1,024-dimension active space.

**Fix:**

1. Start Ollama and ensure the model is present:

```bash
brew services start ollama
ollama pull bge-m3
ollama list
```

2. Verify the Interface directly:

```bash
.venv/bin/python -c \
  'from src.embeddings import generate_embedding, active_embedding_space; v=generate_embedding("health check"); print(active_embedding_space(), len(v))'
```

3. Expect `ollama:bge-m3:1024 1024`. Remove conflicting `EMBEDDING_PROVIDER`,
`OLLAMA_EMBEDDING_MODEL`, or `OLLAMA_BASE_URL` overrides if the result differs.

> [!WARNING]
> Do not select Titan as the active provider or copy preserved Titan vectors into `embedding`.
> Legacy vectors remain isolated in `legacy_embedding`; mixing spaces invalidates cosine search.

## A launchd job is failing

**Symptom:** `launchctl list | grep second-brain` shows a non-zero exit code next to a job name.

**Likely cause:** A dependency failed (e.g., PostgreSQL down, SSO expired) or a script error.

**Fix:**

1. Check the exit code:

```bash
launchctl list | grep second-brain
```

Expected (healthy):

```text
-    0    com.second-brain.backup
-    0    com.second-brain.dream-cycle
```

A non-zero second column (e.g., `1` or `78`) indicates failure.

2. Read the job's log:

```bash
cat ~/second-brain/logs/<job-name>-$(date +%Y%m%d).log
```

3. Fix the root cause (usually PostgreSQL down or SSO expired), then re-run the job manually:

```bash
bash ~/second-brain/scripts/jobs/<script>.sh
```

4. If you updated a plist, reload it:

```bash
launchctl unload ~/Library/LaunchAgents/com.second-brain.<name>.plist
launchctl load ~/Library/LaunchAgents/com.second-brain.<name>.plist
```

## Google Drive backup / verify failures

**Symptom:** The backup log ends with an rclone error; `logs/verify-YYYYMMDD.log` reports download failures; the weekly verify job has a non-zero exit code.

**Likely cause:** The Google OAuth refresh token expired or rclone lost its authorization.

**Fix:**

```bash
rclone config reconnect gdrive:
```

This opens a browser for re-authorization. Then re-run the backup:

```bash
bash ~/second-brain/scripts/jobs/backup.sh
```

Verify the connection:

```bash
rclone ls gdrive:memory-bank-backups/ | head -5
```

Expected output:

```text
 12345678 memory_bank_20260616.dump.gpg
 ...
```

> [!WARNING]
> Google Drive is the sole offsite backup destination (S3 was de-scoped). If rclone fails, you have only the 7-day local copy as a safety net. Fix promptly.

## Dream cycle producing nothing or aborting

**Symptom:** The dream cycle log shows "0 insights generated", aborts partway through, or does not run at all.

**Likely cause:** AWS SSO expired (most common — the dream cycle calls Bedrock for LLM synthesis), insufficient memories to synthesize, or the scheduled job did not fire.

**Fix:**

1. Check the log for errors:

```bash
cat ~/second-brain/logs/dream-cycle-$(date +%Y%m%d).log
```

2. If you see `ExpiredTokenException`, refresh SSO:

```bash
aws sso login --profile default
```

3. Verify the job is loaded:

```bash
launchctl list | grep dream-cycle
```

Expected output:

```text
-    0    com.second-brain.dream-cycle
```

4. Run it manually to test:

```bash
bash ~/second-brain/scripts/jobs/dream_cycle_scheduled.sh
```

5. If it produces nothing and there are no errors, the system may not have enough new memories since the last run. This is normal — the dream cycle skips synthesis when there is nothing new to connect.

## Related

- [Operations](operations.md) — scheduled jobs, monitoring checklist, quick fixes
- [Upgrade](upgrade.md) — safe upgrade procedure
- [Getting started](getting-started.md) — initial installation and verification
- [Connect an AI agent](connect-ai-agent.md) — MCP client configuration
- [Disaster Recovery](../DISASTER-RECOVERY.md) — full backup and restore procedures
- [Glossary](glossary.md) — terminology definitions
