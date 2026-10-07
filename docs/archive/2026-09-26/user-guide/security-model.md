---
title: "Security Model"
type: explanation
---

# Security Model

How Second Brain protects your data, where the trust boundaries lie, and what assumptions the design makes about threats.

## Threat model

Second Brain is a **single-user, single-host** system. It stores personal knowledge — memories, relationships, embeddings — on one machine that you control. The design optimises for this reality rather than for a multi-tenant service or a network-facing application.

The primary risks are:

1. **Credential leakage** — an API key, app password, or encryption passphrase accidentally committed to version control or exposed in logs.
2. **Local-machine compromise** — an attacker with physical or remote access to the host gaining access to the database or backup key.

Multi-tenant isolation, cross-network injection, and denial-of-service are explicitly **out of scope**. If you ever need to serve multiple users or expose a network endpoint, you would need to add authentication, authorization, and transport encryption — none of which exist today.

## Trust boundaries

```
┌─────────────────────────────────────────────┐
│              Your Mac (trusted host)         │
│                                             │
│  ┌───────────┐    stdio    ┌─────────────┐  │
│  │ Kiro CLI  │◄───────────►│ MCP server  │  │
│  │ Claude    │             │ (9 tools)   │  │
│  └───────────┘             └──────┬──────┘  │
│                                   │         │
│                          127.0.0.1:5432     │
│                                   ▼         │
│                          ┌──────────────┐   │
│                          │ PostgreSQL 17│   │
│                          │ + pgvector   │   │
│                          └──────────────┘   │
└─────────────────────────────────────────────┘
```

Connected *agents* — Kiro CLI, Claude Code, or any future MCP client — are **trusted local clients**. They communicate with the MCP server over **stdio** (standard input/output), which means data never leaves the process boundary of your machine. The MCP server has **no authentication layer** because stdio is not a network socket; there is nothing for a remote attacker to connect to.

> [!WARNING]
> If you ever expose the MCP server over a TCP port (for example via a network proxy or SSH tunnel), you **must** add authentication and authorization first. Without them, any process that can reach the port has full read/write access to every memory.

## Network exposure

In normal operation, Second Brain has **no network listeners**:

| Component | Binding | Reachable from |
|-----------|---------|----------------|
| PostgreSQL 17 | `127.0.0.1:5432` | localhost only |
| MCP server | stdio (no socket) | parent process only |

PostgreSQL binds to the loopback address exclusively — it does not accept connections from other machines on the network. The MCP server inherits its parent process's file descriptors and never opens a port.

The only component that *did* listen on a network port was the deprecated HTTP capture API (`src/capture_api.py`), which bound port 8100 and used a bearer-token check. That endpoint captured zero memories over its lifetime and is slated for removal. Once its launchd plist is unloaded, the system has zero inbound network surface.

## Data at rest

### Live database

The PostgreSQL data directory (`/opt/homebrew/var/postgresql@17`) is **not separately encrypted**. Protection relies on macOS full-disk encryption (FileVault). If FileVault is disabled, anyone with physical access to the disk can read your memories directly.

### Backups

Backups are encrypted with **GPG AES-256** symmetric encryption before leaving the machine. The passphrase lives in `~/second-brain/.backup-key` (permissions `chmod 600`, owner-read only). Encrypted dumps are uploaded to Google Drive (30-day retention) and kept locally (7-day retention).

The passphrase is also stored redundantly in Google Secret Manager and AWS Secrets Manager so you can recover even if the local machine is lost. See [operations.md](operations.md) for backup procedures and the [Disaster Recovery runbook](../DISASTER-RECOVERY.md) for full recovery scenarios.

## Credentials

| Credential | Purpose | Lifetime | Storage |
|------------|---------|----------|---------|
| AWS SSO token | Bedrock embeddings + LLM calls | 8–12 hours | `~/.aws/sso/cache/` (auto-refreshed via `aws sso login`) |
| `GMAIL_APP_PASSWORD` | Express email push | Long-lived | Environment variable in launchd plist (never committed) |
| Backup passphrase | Encrypt/decrypt GPG backups | Permanent | `~/second-brain/.backup-key` (chmod 600, gitignored) |
| Google OAuth (rclone) | Google Drive backup uploads | Refresh token (long-lived) | `~/.config/rclone/rclone.conf` |

AWS SSO uses **short-lived tokens** obtained through browser-based authorization. When a token expires, Bedrock calls (embedding, dream cycle LLM inference) fail until you run `aws sso login --profile default`. This is a deliberate trade-off: short-lived credentials limit the blast radius of a stolen token at the cost of periodic re-authentication.

The Express email push authenticates to Gmail with an **app password** supplied through the `GMAIL_APP_PASSWORD` environment variable. This credential is long-lived, so treat it like any other secret — rotate it if you suspect exposure.

## Secret-handling guidance

Keep secrets out of committed files:

- **Environment variables** — set them in your shell profile or in the relevant launchd plist's `EnvironmentVariables` dict. Use placeholders (e.g. `<your-app-password>`) in documentation and examples.
- **Gitignored files** — the `.backup-key` file, `.env` files, and rclone config all live outside version control.
- **macOS Keychain** — suitable for credentials that command-line tools can retrieve via `security find-generic-password`.

Never commit a real secret. If one slips through, rotate it immediately — git history is permanent even after a force-push.

> [!WARNING]
> **Known issue:** The deprecated capture API's launchd plist (`scheduling/com.second-brain.capture-api.plist`) currently stores its `CAPTURE_API_TOKEN` in **cleartext in version control**. This token should be rotated and moved out of the repository. The plist (and the capture API itself) are slated for removal as part of dead-code cleanup.

## Summary of assumptions

The security model rests on a small set of assumptions. If any of these change, you need to revisit the design:

1. You are the only user of this machine (no untrusted local accounts).
2. FileVault (or equivalent FDE) is enabled.
3. The MCP server is never exposed over a network.
4. Short-lived AWS SSO tokens are refreshed manually; long-lived secrets live only in environment variables or gitignored files.

## Related

- [Architecture](architecture.md) — system components and data flow
- [Operations](operations.md) — backup procedures, AWS SSO refresh, job health
- [Getting started](getting-started.md) — initial setup including credential configuration
- [Glossary](glossary.md) — canonical terminology
