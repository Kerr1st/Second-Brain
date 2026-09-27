# Isolated tests

Run from the repository root with a Python environment containing the project
dependencies:

```sh
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q
```

For an isolated worktree without its own virtual environment, use the absolute
path to an existing compatible Python interpreter. Everything after `--` is a
pytest argument; selected files and `-k` expressions work normally.

The runner requires macOS `sandbox-exec`, PostgreSQL 17 tools, and pgvector.
`SECOND_BRAIN_TEST_PG_BIN` can select another directory containing compatible
PostgreSQL tools. Missing prerequisites fail; there is no live-database fallback.

Each invocation creates a new private directory under `/private/tmp/sbtest-*`,
starts PostgreSQL listening only on its own Unix socket, and provisions empty
synthetic databases owned by a non-superuser test role. It stops this server when
pytest exits or is interrupted. The printed directory retains bootstrap, server,
and model-metrics evidence. No production database is copied; test data comes
from the repository's fixtures, including its existing recorded excerpts.

Before collection, the process receives a fresh home directory, synthetic source
paths, a temporary metrics directory, and no inherited cloud credentials. Database
connections must match the runner's exact host, port, role, and database allowlist.
Provider constructors and external-service commands are refused. An OS policy
also blocks network connections from child shell commands, allowing only the
disposable PostgreSQL socket. Tests use injected models/embeddings and explicitly
stubbed shell dependencies.

The production migration runner initializes test schemas. Schema edits and ledger
entries share a transaction and advisory lock. Tests verify failure rollback,
rerun behavior, and concurrent runners. Every test requesting a shared database
fixture starts and ends with empty application tables; the migration ledger is
preserved. Direct `pytest` is refused before collection. Live-provider or live-data
qualification is a separate operational workflow and is not enabled by a marker.

Passing this suite establishes synthetic implementation behavior, not provider
availability, successful deployment, or improved work outcomes.
