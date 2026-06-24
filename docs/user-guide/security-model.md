---
title: "Security Model"
type: explanation
---

# Security Model

Second Brain is a single-user, local-first system for personal knowledge and AI-agent memory. It is not designed as a hosted multi-tenant service.

## Threat Model

Primary risks:

1. Credential leakage through committed files, logs, or examples.
2. Local-machine compromise exposing the database or backup key.
3. Accidental network exposure of local-only services.

Out of scope for this version:

- Multi-user authorization.
- Public internet hosting.
- Multi-tenant isolation.

## Trust Boundaries

```
Local AI client -> MCP server over stdio -> PostgreSQL on localhost
```

The MCP server is intended to run over stdio as a child process of a trusted local AI client. It should not be exposed over TCP without adding authentication and authorization.

## Network Exposure

Normal operation should have no public network listener. PostgreSQL should bind to localhost only. Any deprecated or experimental HTTP capture endpoints should remain disabled unless explicitly secured.

## Data at Rest

The live database relies on host-level disk protection, such as full-disk encryption. Backups should be encrypted before leaving the machine.

## Credentials

Keep credentials outside git:

- API keys in environment variables or a local secret manager.
- Backup keys in a password vault or secret manager.
- OAuth/rclone/cloud configs in user-local config directories.
- App passwords only in local environment configuration, never in committed plist or config files.

## Secret Handling

Never commit a real secret. If one slips through, rotate it immediately and rewrite public history if the repository has already been published.

Before making this repository public, scan both the current tree and git history for secrets, logs, database dumps, raw exports, and private notes.

## Assumptions

1. You are the only user of the host machine.
2. Full-disk encryption is enabled.
3. PostgreSQL is bound to localhost.
4. MCP is stdio-only.
5. Long-lived secrets are stored outside the repository.

## Related

- [Architecture](architecture.md)
- [Operations](operations.md)
- [Getting started](getting-started.md)
