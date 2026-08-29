# Security Policy

Second Brain is a local-first, single-user AI memory system. It is not designed to be exposed as a hosted multi-tenant service.

## Supported Use

- Run the MCP server over stdio from a trusted local AI client.
- Keep PostgreSQL bound to localhost.
- Store real credentials in environment variables, a local secret manager, or user-local config files.
- Encrypt backups before syncing them off-machine.

## Do Not Commit

- API keys, OAuth tokens, Gmail app passwords, or AWS credentials.
- Database dumps, raw memory exports, logs, or personal notes.
- Backup keys or decrypted backup material.
- Browser cookies or captured session data, except bounded real-data test
  fixtures deliberately reviewed under `tests/fixtures/` as described below.

## Reviewed Real-Data Fixtures

Agent Task capture tests may commit bounded excerpts of real task history under
`tests/fixtures/`. This deliberate exception is recorded in ADR 0005. Before
committing such a fixture, review it for credentials, tokens, private keys,
cookies, database exports, and unrelated personal material. Accepted fixture
content and local path metadata become permanent Git history; deleting the file
later does not erase it from existing clones or prior commits.

Use `.env.example` as the shape of local configuration. Copy it to `.env` and replace placeholders locally; `.env` is gitignored.

## Reporting

This is a personal public proof artifact, not a production service. If you find a security issue in the public code, open a GitHub issue with reproduction details but do not include secrets, private memory content, or exploit payloads against third-party services.

## Related Documentation

- [Security model](docs/user-guide/security-model.md)
- [Operations](docs/user-guide/operations.md)
- [Disaster recovery](docs/DISASTER-RECOVERY.md)
