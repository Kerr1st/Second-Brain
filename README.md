# Second Brain

Second Brain captures work context across the user's work surfaces (chats, Codex tasks, Slack, Outlook, browser, YouTube, Quick Desktop, Crawlee) into PostgreSQL + pgvector, retrieves it over MCP, and runs a scheduled multi-seat discovery cycle that looks for evidence-backed connections across projects. A useful discovery names a current problem, cites evidence, explains what transfers and proposes a next step; more records or reviewer agreement alone do not count.

Start with [docs/START-HERE.md](docs/START-HERE.md). [docs/RELEASES.md](docs/RELEASES.md) says which release is built, reviewed or deployed; nothing else in this tree is a claim about the installed system.

| Need | Document |
|---|---|
| Orientation, purpose, what to read per component | [Start here](docs/START-HERE.md) |
| Release line R1 … R7: what changed, status, private notes | [Releases](docs/RELEASES.md) |
| Storage, retrieval, discovery, delivery | [Architecture](docs/ARCHITECTURE.md) |
| Capture channels and identity | [Capture](docs/CAPTURE-COMPONENTS.md) |
| Model routing, spend, data boundaries | [Models](docs/MODEL-BACKENDS.md) |
| Jobs, roles, release and maintenance procedure | [Operations](docs/OPERATIONS.md), [scheduling/README.md](scheduling/README.md) |
| Backups, restore, key custody | [Recovery](docs/DISASTER-RECOVERY.md) |
| MCP, Python and shell contracts | [Interfaces](docs/INTERFACES.md) |
| Tests, effectiveness, owned hazards | [Validation](docs/VALIDATION.md) |
| Specifications | [Spec index](docs/SPEC-INDEX.md) |
| Counted facts generated from source | [Runtime inventory](docs/generated/runtime-inventory.md) |

## Develop and verify

Python 3.14, PostgreSQL 17 with pgvector, macOS (`sandbox-exec`). Install the locked dependencies and run the suite only through the isolated runner:

```sh
uv venv .venv && uv pip sync --python .venv/bin/python requirements.lock   # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q
```

The runner creates a disposable socket-only cluster with three roles (`sb_test_owner` runs `migrations/migrate.sh`, `sb_test` is the NOSUPERUSER application role every application path runs as, `sb_test_legacy` stands in for the pre-R7 superuser), isolates HOME, metrics and credentials, and blocks provider and network access before collection. Direct `pytest` refuses to run. Missing prerequisites are errors, not fallbacks. See [tests/README.md](tests/README.md).

Do not run `migrations/migrate.sh`, the job installers or any `scripts/maintenance/*` apply mode against an existing installation as a development step. [Operations](docs/OPERATIONS.md) and the private release notes listed in [Releases](docs/RELEASES.md) describe the maintenance windows, hard gates and rollbacks.

## Layout

```
src/            application code: db/ (pool, queries, maintenance), capture/, dream_cycle/, operations/, skim/, backends/, mcp_server.py, mcp_readonly.py
scripts/        jobs/ (launchd targets through job_wrapper.sh), maintenance/ (backup bundle, dedup/rechunk planners, graph export, app-role smoke), eval/, docs/inventory.py, test_isolated.py
migrations/     000 … 018 + migrate.sh (transactional ledger, advisory lock, owner-role gate)
scheduling/     17 launchd plists (symlinked into ~/Library/LaunchAgents on the installed Mac)
config/         backends.toml (profiles/roles), backup.toml, liveness.toml, skim.toml
tests/          isolated suite (see tests/README.md)
docs/           current guides; docs/archive/ holds dated, superseded material
```
