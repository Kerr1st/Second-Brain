# Disaster Recovery Runbook

> Public-safe version. Replace placeholders with your own local paths, backup remote, and key-management system before use.

## Backup Posture

Second Brain is designed as a single-user, local-first system. Backups should be encrypted before leaving the machine and copied to at least one offsite location.

Recommended posture:

- Daily encrypted PostgreSQL dump.
- Optional encrypted JSON exports for recovery/debugging.
- Local short-retention copy.
- Offsite encrypted copy using a provider you control.
- Backup key stored outside the repository.

## Failure Modes

| Issue | Impact | Mitigation |
|---|---|---|
| Offsite sync fails | No current offsite copy | Monitor backup logs and run a periodic restore verification. |
| Database unavailable | Backup job aborts | Pre-flight database health check before dumping. |
| Backup key unavailable | Encrypted backup cannot be restored | Store the key in a secure secret manager or offline password vault. |

## Backup Assets

| Asset | Recommended handling |
|---|---|
| PostgreSQL dump | Encrypt, store locally and offsite. |
| JSON exports | Encrypt, store if useful for diagnostics. |
| Source documents | Keep separate from generated backups if they contain private data. |
| Config and migrations | Store in git, without secrets. |
| Encryption key | Never commit. Store in a secure password vault or secret manager. |

## Example Backup Flow

```bash
# Create a dump
pg_dump -Fc -h localhost -U memory_bank memory_bank > /tmp/second_brain.dump

# Encrypt before upload
gpg --symmetric --cipher-algo AES256 /tmp/second_brain.dump

# Copy encrypted artifact to your offsite destination
# Example: rclone copy /tmp/second_brain.dump.gpg <remote>:<backup-path>/
```

## Example Restore Flow

```bash
# Install dependencies for your platform
# PostgreSQL 17 + pgvector + gpg are required.

# Retrieve encrypted backup and key from your secure locations.
gpg --decrypt --batch --passphrase-file /path/to/backup-key second_brain.dump.gpg > /tmp/second_brain.dump

createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"
pg_restore -h localhost -U memory_bank -d memory_bank /tmp/second_brain.dump
```

## Public Repo Safety

Do not commit:

- `.env` files.
- Database dumps.
- Backup keys.
- OAuth refresh tokens.
- Cloud credentials.
- Raw chat transcripts or personal memory exports.
- Logs that include URLs, filesystem paths, or private content.
