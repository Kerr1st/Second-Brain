# Second Brain

A personal knowledge management and ideation system powered by AI. Gives AI agents persistent memory across sessions, turning them into thinking partners that accumulate knowledge over time.

~122K memories. 10 migrations. Native PostgreSQL 17 + pgvector (plus a dormant imported knowledge graph). MCP server (9 tools) for agent access. Dream cycle for autonomous synthesis. Express delivery layer (on-demand briefing, gated email push, in-session tool, feedback).

## Quickstart

```bash
# PostgreSQL 17 + pgvector (native, via Homebrew) — serves localhost:5432, auto-starts at login
brew install postgresql@17 pgvector
brew services start postgresql@17

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

Tests require a running PostgreSQL instance (native, on `localhost:5432`). The test fixture creates an isolated `memory_bank_test` database. Embedding calls are mocked — no Bedrock credentials needed for tests.

## Documentation

**New to Second Brain? Start with the [User Guide](docs/user-guide/index.md)** — what it is, how to install and configure it, day-to-day use, and operations. The references below are deeper design and architecture docs.

| Doc | What it covers |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System diagram, database schema, search architecture, tech stack |
| [OPERATIONS.md](docs/OPERATIONS.md) | Scheduled jobs, backup, SSO management, monitoring |
| [EXPRESS-PLAN.md](docs/EXPRESS-PLAN.md) | Express delivery layer: briefing, gated email push, in-context tool, feedback loop |
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
  migrate/          Data migration and sync scripts
    migrate_quick_desktop.py   QD memories + KG entities/edges
    enrich_qd_tags.py          QD memory tags + domains
    ingest_eventlog.py         QD feed events + interactions
    import_slack_graph.py      Slack channels + users → KG
    ingest_doc_chunks.py       QD pre-chunked documents (hourly)
  eval/             Evaluation framework (retrieval quality metrics)
  batch_ingest_parallel.py   Parallel batch ingest (20 workers)
  ingest_qd_chats.py         Quick Desktop chat session extractor
migrations/         Numbered SQL migrations (000-010) + runner script
scheduling/         macOS launchd plists (9 jobs, all installed)
tests/              643 tests (property-based, unit, integration, E2E)
docs/               Architecture specs and runbooks
docker/             Legacy container data dir (gitignored) — retained as migration rollback, removed in Phase 5
staging/            Transient pipeline data (gitignored)
```

## Current Status

**Working:**
- Memory CRUD, hybrid search, reranking, retrieval reinforcement
- MCP server (9 tools) connected to Kiro CLI and Claude Code
- Quick Desktop hourly sync (memories, tags, KG, chats, feed events, Slack graph)
- Daily encrypted backups to Google Drive + local (S3 de-scoped 2026-06-01)
- Chat extraction, Crawlee ingestion, bookmark/YouTube scraping
- Dream cycle pipeline (daily at noon; chains the Express email push)
- Express delivery — on-demand `brief`, gated Gmail push, `memory_brief` MCP tool, feedback loop
- All 9 LaunchAgents installed and operational
- 643 tests (unit, property-based, integration, E2E)

**Planned:**
- Entity extraction on ingest (auto-populate knowledge graph from all sources)
- Dream cycle integration with knowledge graph (Explorer uses graph traversal)
- Metadata extraction on ingest (people, topics, action items)
