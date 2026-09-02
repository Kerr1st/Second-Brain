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
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
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
apply: 011_backend_provenance.sql
apply: 012_agent_task_capture.sql
apply: 013_context_governance.sql
apply: 014_local_embedding_space.sql
apply: 015_enforce_active_embedding_space.sql
done
```

Migrations that were already applied are skipped: `skip: <version> (already applied)`.

## 4. Install the local embedding runtime

Second Brain uses local Ollama BGE-M3 for 1,024-dimension embeddings. Install the runtime, start it
at login, and pull the model:

```bash
brew install ollama
brew services start ollama
ollama pull bge-m3
```

Verify the active space:

```bash
.venv/bin/python -c \
  'from src.embeddings import generate_embedding, active_embedding_space; v=generate_embedding("health check"); print(active_embedding_space(), len(v))'
```

Expected output:

```text
ollama:bge-m3:1024 1024
```

> [!NOTE]
> AWS credentials are needed only if you explicitly select a Bedrock-backed reasoning profile.
> The active embedding path and backups do not require AWS.

## 5. Start the MCP server and connect an agent

Start the MCP server:

```bash
python -m src.mcp_server
```

The server exposes 11 tools over stdio, adding `memory_context` and `memory_context_outcome` to the existing memory, relationship, learning, and briefing tools.

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

Client-specific config file locations vary by version. See [Connect an AI agent](connect-ai-agent.md) for Kiro CLI and Claude Code notes.

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

Tests run against an isolated `memory_bank_test` database and mock Ollama calls — the local model
does not need to run during tests.

## Next steps

- [Using Second Brain](using-second-brain.md) — day-to-day workflows: capturing, searching, and the dream cycle
- [Operations](operations.md) — backups, scheduled jobs, monitoring, and SSO renewal
