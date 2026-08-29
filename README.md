# Second Brain

Second Brain is a local-first, user-controlled learning and governance layer across AI agents. It preserves provenance-rich task evidence, builds bounded context for future work, and turns validated user direction into reviewed, versioned guidance. PostgreSQL + pgvector, MCP, the Dream Cycle, and delivery adapters provide the underlying memory and synthesis substrate.

This repository is a public technical proof artifact. The live private instance has about 122K memories; this repo contains the application code, migrations, tests, and documentation, not the private memory database.

## What This Demonstrates

- **Agent memory over MCP:** 11 stdio tools for memory CRUD, search, bounded context, outcome receipts, relationships, learning, and briefings.
- **Retrieval engineering:** hybrid full-text + vector search, RRF fusion, utility reranking, depth scoring, temporal context, and regression tests.
- **Operational maturity:** PostgreSQL migrations, launchd job specs, backup/disaster recovery docs, smoke tests, and troubleshooting guides.
- **AI workflow design:** a dream-cycle pipeline for autonomous synthesis plus an Express briefing layer with feedback.
- **Reviewable security posture:** local-only assumptions, placeholder config, secret-handling docs, and localhost-bound Docker fallback.

For reviewers and AI code-review tools such as CodeRabbit, the most interesting surfaces are MCP tool contracts, SQL migrations, retrieval/reranking behavior, credential boundaries, and docs-to-code setup accuracy.

## Quickstart

The recommended path is native PostgreSQL 17 via Homebrew on macOS. Use Python 3.13+; the examples below use Homebrew Python 3.14.

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17

python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

createuser -s memory_bank 2>/dev/null || true
createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"

./migrations/migrate.sh
python -m src.mcp_server
```

Copy `.env.example` to `.env` for local overrides. The default database credentials are development-only and assume PostgreSQL is bound to localhost.

## Running Tests

```bash
# Quick smoke test
.venv/bin/python -m pytest tests/test_db.py tests/test_search.py tests/test_mcp_server.py -q

# Full suite
.venv/bin/python -m pytest
```

Tests require a running local PostgreSQL instance. The test fixture creates an isolated `memory_bank_test` database. Embedding calls are mocked, so Bedrock credentials are not required for tests.

## Documentation

**New to Second Brain? Start with the [User Guide](docs/user-guide/index.md).** It covers installation, configuration, day-to-day use, operations, and troubleshooting. The references below are deeper design and architecture docs.

| Doc | What it covers |
|---|---|
| [Architecture Component Index](docs/components/index.md) | Canonical component boundaries, contracts, entry points, tests, operations, and related decisions |
| [Project Charter](docs/PROJECT-CHARTER.md) | Canonical scope, non-goals, four planes, and vertical-slice delivery strategy |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System diagram, database schema, search architecture, tech stack |
| [OPERATIONS.md](docs/OPERATIONS.md) | Scheduled jobs, backup, SSO management, monitoring |
| [EXPRESS-PLAN.md](docs/EXPRESS-PLAN.md) | Express delivery layer: briefing, gated email push, in-context tool, feedback loop |
| [DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md) | Backup topology, recovery procedures, encryption keys |
| [DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) | Why key architectural choices were made (cognitive science rationale) |
| [QUICK-DESKTOP-INTEGRATION.md](docs/QUICK-DESKTOP-INTEGRATION.md) | Quick Desktop sync design and knowledge graph import |
| [HYBRID-CHAT-EXTRACTION.md](docs/HYBRID-CHAT-EXTRACTION.md) | Chat ingestion pipeline spec |
| [SECURITY.md](SECURITY.md) | Public security posture, secret handling, and reporting guidance |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, test expectations, and review focus |

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
migrations/         Numbered SQL migrations (000-013) + runner script
scheduling/         macOS launchd plist templates for local automation
tests/              Property-based, unit, integration, and E2E tests
docs/               Architecture specs and runbooks
docker-compose.yml  Optional local PostgreSQL fallback, bound to 127.0.0.1
staging/            Transient pipeline data (gitignored)
```

## Current Status

**Working in the private/local deployment:**
- Memory CRUD, hybrid search, reranking, retrieval reinforcement
- MCP server (11 tools) connected to Kiro CLI and Claude Code
- Quick Desktop hourly sync (memories, tags, KG, chats, feed events, Slack graph)
- Daily encrypted backups to Google Drive + local (S3 de-scoped 2026-06-01)
- Chat extraction, Crawlee ingestion, bookmark/YouTube scraping
- Dream cycle pipeline (daily at noon; chains the Express email push)
- Express delivery — on-demand `brief`, gated Gmail push, `memory_brief` MCP tool, feedback loop
- launchd templates for local automation
- Comprehensive unit, property-based, integration, and E2E test suite

**Planned:**
- Entity extraction on ingest (auto-populate knowledge graph from all sources)
- Dream cycle integration with knowledge graph (Explorer uses graph traversal)
- Metadata extraction on ingest (people, topics, action items)
