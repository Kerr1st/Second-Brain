# Second Brain

A personal knowledge management and ideation system powered by AI. Gives AI agents persistent memory across sessions, turning them into thinking partners that accumulate knowledge over time.

## Quickstart

```bash
# Start PostgreSQL + pgvector
cd docker && docker compose up -d

# Apply database migrations
cd .. && ./migrations/migrate.sh

# Install Python dependencies
pip install -r requirements.txt
```

## Documentation

- [docs/AGENTS.md](docs/AGENTS.md) — Architecture, schema, and project status
- [docs/DREAM-CYCLE-DESIGN.md](docs/DREAM-CYCLE-DESIGN.md) — Dream cycle multi-agent learning pipeline
- [docs/V2-TASKS.md](docs/V2-TASKS.md) — V2 implementation task list
- [docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) — Why key architectural choices were made
- [docs/V2-ROADMAP.md](docs/V2-ROADMAP.md) — Cognitive science-grounded enhancements roadmap
- [docs/HYBRID-CHAT-EXTRACTION.md](docs/HYBRID-CHAT-EXTRACTION.md) — Chat ingestion pipeline spec
- [docs/DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md) — Backup and recovery procedures

## Project Structure

```
docker/          PostgreSQL + pgvector setup
migrations/      Numbered SQL migrations + runner script
src/             Core library (MCP server, DB, embeddings, ingestion, parsers)
  dream_cycle/   Four-agent pipeline (orchestrator, consensus, storage, digest, feedback)
  prompts/       Agent prompt templates
  parsers/       Chat and content parsers
scripts/         Scheduled jobs (chat extraction, Crawlee ingestion, backup)
scheduling/      macOS launchd plists
tests/           157 tests (property-based, unit, integration)
staging/         Transient data between pipeline phases (gitignored)
docs/            Architecture specs and runbooks
.kiro/specs/     Spec-driven development artifacts
```
