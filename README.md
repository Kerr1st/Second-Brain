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
- [docs/HYBRID-CHAT-EXTRACTION.md](docs/HYBRID-CHAT-EXTRACTION.md) — Chat ingestion pipeline spec
- [docs/DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md) — Backup and recovery procedures

## Project Structure

```
docker/          PostgreSQL + pgvector setup
src/             Core library (MCP server, DB, embeddings, ingestion, parsers)
scripts/         Scheduled jobs (chat extraction, Crawlee ingestion, backup)
scheduling/      macOS launchd plists
staging/         Transient data between pipeline phases (gitignored)
docs/            Architecture specs and runbooks
```
