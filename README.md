# Second Brain

A personal knowledge management and ideation system powered by AI. Gives AI agents persistent memory across sessions, turning them into thinking partners that accumulate knowledge over time.

74K+ memories. 9 migrations. PostgreSQL + pgvector + knowledge graph. MCP server for agent access. Dream cycle for autonomous insight generation.

## Quickstart

```bash
# Start PostgreSQL + pgvector
docker compose up -d

# Create virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Apply database migrations
./migrations/migrate.sh

# Start the MCP server
python -m src.mcp_server
```

## Running Tests

```bash
# All tests
.venv/bin/python -m pytest

# Specific test file
.venv/bin/python -m pytest tests/test_db.py

# Quick smoke test (core modules)
.venv/bin/python -m pytest tests/test_db.py tests/test_search.py tests/test_mcp_server.py -q
```

Tests require a running PostgreSQL instance (Docker). The test fixture creates an isolated `memory_bank_test` database. Embedding calls are mocked — no Bedrock credentials needed for tests.

## Documentation

| Doc | What it covers |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System diagram, database schema, search architecture, tech stack |
| [OPERATIONS.md](docs/OPERATIONS.md) | Scheduled jobs, backup, SSO management, monitoring |
| [DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md) | Backup topology, recovery procedures, encryption keys |
| [DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) | Why key architectural choices were made (cognitive science rationale) |
| [QUICK-DESKTOP-INTEGRATION.md](docs/QUICK-DESKTOP-INTEGRATION.md) | Quick Desktop sync design and knowledge graph import |
| [HYBRID-CHAT-EXTRACTION.md](docs/HYBRID-CHAT-EXTRACTION.md) | Chat ingestion pipeline spec |

## Project Structure

```
src/                Core library (MCP server, DB, search, embeddings, ingestion, parsers)
  dream_cycle/      Four-agent autonomous learning pipeline
  parsers/          Chat and content parsers
  prompts/          Agent prompt templates
scripts/
  jobs/             Scheduled job scripts (backup, sync, scrape, ingest)
  migrate/          Data migration scripts (Claude, ChatGPT, Notion, Quick Desktop)
  eval/             Evaluation framework (retrieval quality metrics)
migrations/         Numbered SQL migrations (000-008) + runner script
scheduling/         macOS launchd plists (9 jobs)
tests/              Tests (property-based, unit, integration)
docs/               Architecture specs and runbooks
docker/             PostgreSQL data volume (gitignored)
staging/            Transient pipeline data (gitignored)
```

## Current Status

**Working:**
- Memory CRUD, hybrid search, reranking, retrieval reinforcement
- MCP server (8 tools) connected to Kiro CLI and Claude Code
- Quick Desktop hourly sync (memories + knowledge graph)
- Daily encrypted backups to Google Drive + S3
- Chat extraction, Crawlee ingestion, bookmark/YouTube scraping
- Capture API (Slack, mobile, browser, email)

**In progress:**
- Dream cycle pipeline (code complete, one manual run, LaunchAgent not yet installed)
- Evaluation framework (scripts exist, needs documentation and regular runs)

**Planned:**
- Entity extraction on ingest (auto-populate knowledge graph from all sources)
- Dream cycle integration with knowledge graph (Explorer uses graph traversal)
- Metadata extraction on ingest (people, topics, action items)
