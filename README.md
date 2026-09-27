# Second Brain

Second Brain captures work context and seeks evidence-backed connections across projects that improve an actual decision or deliverable.

Start with [the orientation guide](docs/START-HERE.md), then [the implementation ledger](docs/REFACTOR-COMPLETION-2026-09-26.md). This branch repairs the original PostgreSQL implementation. Tested development code and the installed system are separate states.

## Develop and verify

Use Python 3.14 and PostgreSQL 17 with pgvector. Install the locked dependencies into an isolated checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q
```

The macOS runner creates a disposable socket-only database, isolates HOME and credentials, and blocks provider/network access before collection. Direct pytest refuses to run. Missing isolation prerequisites are errors.

Do not run migrations or job installers against an existing installation as a development setup step. Follow [Operations](docs/OPERATIONS.md) for owner/runtime roles, backup qualification, release and rollback.

The current configuration is [generated from source](docs/generated/runtime-inventory.md). Provider and embedding dependencies are documented in [Model backends](docs/MODEL-BACKENDS.md). Passing tests establish component behavior; [Validation](docs/VALIDATION.md) explains the separate test of useful discoveries.
