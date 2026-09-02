# Contributing

Second Brain is published primarily as a technical proof artifact: a local-first AI memory system with MCP tools, retrieval quality tests, migrations, and operations docs.

## Development Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Create and migrate a local PostgreSQL database as described in [Getting started](docs/user-guide/getting-started.md).

## Before Opening a Change

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_search.py tests/test_mcp_server.py -q
```

For changes that touch retrieval, reranking, migrations, or agent tool contracts, run the full test suite:

```bash
.venv/bin/python -m pytest
```

## Review Focus

High-value review areas:

- MCP tool contract clarity and backward compatibility.
- SQL migration safety and idempotence.
- Retrieval quality, reranking behavior, and regression coverage.
- Secret handling, local-only network assumptions, and backup hygiene.
- Documentation accuracy for setup, operations, and troubleshooting.

Do not include real memory content, secrets, local logs, or database exports in
issues or pull requests. The sole content exception is a bounded Agent Task
fixture under `tests/fixtures/` that has been deliberately reviewed under
[ADR 0005](docs/adr/0005-use-real-agent-task-data-throughout-testing.md) and
[the security policy](SECURITY.md#reviewed-real-data-fixtures). Never include
credentials or unrelated private material in a fixture. For guide-page
conventions, see the [user-guide contributing notes](docs/user-guide/CONTRIBUTING.md).
