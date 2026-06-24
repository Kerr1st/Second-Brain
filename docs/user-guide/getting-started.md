---
title: "Getting Started"
type: tutorial
---

# Getting Started

Install Second Brain, configure its dependencies, and verify your first memory round-trip in about 15 minutes.

## Prerequisites

> [!NOTE]
> Before you begin, ensure you have:
> - macOS with [Homebrew](https://brew.sh) installed
> - Python 3.13+ (3.14 recommended)
> - An AWS account with Amazon Bedrock access and the AWS CLI configured for SSO
> - An MCP client — [Kiro CLI](https://kiro.dev) or [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

## 1. Install PostgreSQL 17 + pgvector

Second Brain stores memories and vector embeddings in PostgreSQL 17 with the pgvector extension, running natively via Homebrew.

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
```

Expected output:

```text
==> Successfully started `postgresql@17`
```

> [!TIP]
> `postgresql@17` is keg-only. Add its binaries to your PATH:
> ```bash
> echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zshrc
> source ~/.zshrc
> ```

## 2. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Expected output (last lines):

```text
Successfully installed psycopg2-binary-2.9.11 boto3-1.42.69 mcp-1.26.0 ...
```

## 3. Create the database and apply migrations

Create the `memory_bank` role and database, then enable the pgvector extension:

```bash
createuser -s memory_bank 2>/dev/null || true
createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

> [!NOTE]
> The `|| true` guards make these safe to re-run — they no-op if the role or database already exists.

Apply the schema. The migration runner adds tables and indexes to the `memory_bank` database and records applied versions, so it is safe to re-run:

```bash
./migrations/migrate.sh
```

Expected output:

```text
apply: 001_initial_schema.sql
apply: 002_v2_columns.sql
...
apply: 010_express_feedback.sql
done
```

Migrations that were already applied are skipped: `skip: <version> (already applied)`.

## 4. Configure AWS / Bedrock

Second Brain uses Amazon Bedrock for two purposes: generating vector embeddings (Titan Text Embeddings v2, 1024 dimensions) and powering the *dream cycle* — a nightly autonomous synthesis pipeline that discovers connections across your memories.

Authenticate your AWS SSO session:

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
    "UserId": "AROA...:you@example.com",
    "Account": "<aws-account-id>",
    "Arn": "arn:aws:sts::<aws-account-id>:assumed-role/..."
}
```

> [!NOTE]
> SSO tokens expire after 8–12 hours. When expired, embedding and search calls fail. Re-run `aws sso login --profile default` to refresh.
>
> The model backend profile defaults to `laptop` (set via `SECOND_BRAIN_PROFILE` env var; see `config/backends.toml`).

## 5. Start the MCP server and connect an agent

Start the MCP server:

```bash
python -m src.mcp_server
```

The server exposes 9 tools over stdio: `memory_create`, `memory_search`, `memory_read`, `memory_update`, `memory_relate`, `memory_list`, `memory_graph`, `memory_learn`, and `memory_brief`.

To connect your MCP client, add this stdio server configuration (adjust the path to your clone):

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

<!-- TODO: Update with client-specific config path (e.g. ~/.kiro/mcp.json or ~/.claude/config.json) once documented. -->

## 6. Verify

### Check PostgreSQL is running

```bash
pg_isready -h 127.0.0.1 -p 5432 -U memory_bank
```

Expected output:

```text
127.0.0.1:5432 - accepting connections
```

### Store and retrieve a memory

In your connected agent, run:

1. **Create** a test memory:
   > "Use `memory_create` to store a memory with content 'Second Brain is operational' and type 'research'."

2. **Search** for it:
   > "Use `memory_search` to find memories about 'operational'."

You see the memory you just created in the results.

### Run the smoke test (alternative)

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_search.py tests/test_mcp_server.py -q
```

Expected output:

```text
... passed
```

Tests run against an isolated `memory_bank_test` database and mock Bedrock calls — no AWS credentials needed.

## Next steps

- [Using Second Brain](using-second-brain.md) — day-to-day workflows: capturing, searching, and the dream cycle
- [Operations](operations.md) — backups, scheduled jobs, monitoring, and SSO renewal
