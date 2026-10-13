# Backup and recovery

Posture (accepted 2026-06-12, unchanged): **Google Drive + local only**; AWS S3 was de-scoped on 2026-06-01 and is not planned to return. RPO 24 h. The daily job `com.second-brain.backup` (02:00, deadline 2 h) and the weekly `com.second-brain.verify` (Sunday 03:00, deadline 2 h) run through the job runner ([OPERATIONS.md](OPERATIONS.md)). The June 2026 runbook (JSON exports, Docker, S3 and secret-manager key copies) is superseded; its generalized snapshot is [archived](archive/2026-09-10/docs/DISASTER-RECOVERY.md).

## Backup contract (R5: `scripts/jobs/backup.sh` → `scripts/maintenance/backup_bundle.py create`, policy `config/backup.toml`)

| | Pre-R5 (`memory_bank_<ts>.dump.gpg` + four `*.json.gpg`) | R5 (`second-brain_<UTC stamp>.tar.gpg` + `.receipt.json`) |
|---|---|---|
| Database | `pg_dump -Fc` | same, `--no-owner --no-acl`, inside the bundle as `database.dump`; runs as the app role (needs only SELECT) |
| JSON fallbacks | four `row_to_json` exports (including the graph tables) | dropped — the custom dump is restore-tested weekly instead |
| Repository | `backups/config/` copies | `repository/{src,scripts,docs,config,migrations,scheduling,.kiro,README.md,requirements*.txt,requirements.lock,docker-compose.yml,pyproject.toml}` |
| Knowledge sources | `rsync` to `backups/kb-sources/` | `knowledge-sources/` from the path in `config/backup.toml`; a missing or symlinked path **fails the job** |
| Secrets | none | `repository/.env` (the capture token) is inside the bundle on purpose — a replacement host needs it; the bundle is ciphertext end to end and rclone is handed nothing else (`tests/test_backup_bundle.py`). `.backup-key` itself is excluded |
| Integrity | gpg decrypts | manifest v2 (SHA-256, size, mode 0600/0700 per file), `pg_restore --list` of the dump, decrypt-and-verify of the just-written ciphertext before publishing |
| Cloud | `rclone copy … --include "*.gpg"` | `rclone copyto <bundle> gdrive:memory-bank-backups/<bundle>`, then download it again and compare SHA-256 (`remote_verified`) |
| Retention | local 7 d / remote 30 d | same policy from `config/backup.toml`, applied **only after** the new bundle is verified; `*.gpg` and `*.receipt.json` at the root of `backups/` only, both naming schemes; `0` disables a leg |
| Plaintext on the remote | nightly `rclone sync` of kb-sources and config (stopped in R1) | never touched by any job; purge is the CONFIRM-REQUIRED manual step below |
| PostgreSQL down | `brew services start` silently | exit 69, no auto-start |

Receipt fields: `sha256`, `bytes`, `files`, `payload_verified`, `file_modes_verified`, `remote_verified`, `knowledge_sources_included`, `pruned_local`, `pruned_remote`, `retention`. A failed dump, encryption, verification or upload is a failed job with no receipt; a verified local bundle stays recoverable if the upload fails.

### Verify (weekly, `scripts/jobs/verify_backup.sh`)

1. `rclone lsf gdrive:memory-bank-backups --files-only --max-depth 1 --include '*.gpg'` — both naming schemes, because the remote holds pre-R5 dumps next to bundles during the transition.
2. `backup_bundle.py select-latest`: newest by the timestamp in the name; **fails if older than 36 h** — a Drive copy that stopped updating no longer passes because it decrypts.
3. Bundle → `backup_bundle.py verify <file> --restore`: manifest checksums and modes, then a full `pg_restore` into a freshly initialized socket-only cluster under `/private/tmp` (its own superuser, never an existing database), per-table row counts, cluster stopped, plaintext removed, subprocess groups unwound on interruption. The disposable cluster runs with `LC_ALL=C`, because launchd sets no locale and PostgreSQL 17 aborts startup without one. A failure reports a redacted stderr tail, plus the `restore.log` tail when `pg_ctl start` fails; `pg_restore` stderr is withheld because it may echo decrypted content. Legacy dump → `verify-legacy` (decrypt + `pg_restore --list`).

Budget: ~1.1 GB download + ~10 s decrypt + ~4 min restore. PostgreSQL 17, pgvector and GPG must be available to the wrapper; rclone only for cloud operations.

## Roles after migration 017

Grants and default privileges are **per database and not in a dump**. `pg_dump` runs as `memory_bank` (unchanged job credentials); `pg_restore` runs as `memory_bank_owner`.

- **Into the live database** (restore-based rollback): `pg_restore --clean --if-exists --no-owner --no-acl -h localhost -U memory_bank_owner -d memory_bank <dump>` — the owner owns everything including the `vector` extension, so `COMMENT ON EXTENSION` succeeds. `--clean` drops only objects that are in the dump; a table created by a later migration survives and must be dropped by hand if the schema is to be exactly the earlier release's. 016 and 017 are re-runnable after such a restore.
- **Into a fresh database** (disaster recovery): as the cluster superuser `CREATE DATABASE memory_bank OWNER memory_bank_owner; \c memory_bank; CREATE EXTENSION vector;` then `pg_restore --exit-on-error --no-owner --no-acl --no-comments -h localhost -U memory_bank_owner -d memory_bank <dump>` (`--no-comments` because the owner does not own the extension in a fresh database). The app role then has nothing until 017 is re-applied: `DELETE FROM schema_migrations WHERE version='017_role_separation.sql'`, then `bash migrations/migrate.sh` as the owner (017 is idempotent; `test_app_role_dump_restores_as_the_owner_into_a_fresh_database_and_017_regrants`). Re-run `ALTER DATABASE memory_bank SET hnsw.ef_search = 200` too (a database-level GUC, also not in a dump).
- `backup_bundle.py verify --restore` is unaffected: it restores into a disposable cluster as its own superuser.

## Restore a replacement installation

```bash
# key: the installed .backup-key or the OneDrive recovery copy (see "Key custody")
rclone lsf gdrive:memory-bank-backups --files-only --include 'second-brain_*.tar.gpg' | sort | tail -1
rclone copyto gdrive:memory-bank-backups/<bundle> /private/tmp/restore/<bundle>
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/maintenance/backup_bundle.py verify /private/tmp/restore/<bundle> --restore --key <key>   # full restore into a throwaway cluster, row counts
umask 077; mkdir -p /private/tmp/restore/payload
gpg --batch --pinentry-mode loopback --passphrase-file <key> --output /private/tmp/restore/payload.tar --decrypt /private/tmp/restore/<bundle>
tar -xf /private/tmp/restore/payload.tar -C /private/tmp/restore/payload        # database.dump, manifest.json, repository/, knowledge-sources/
brew install postgresql@17 pgvector && brew services start postgresql@17
psql -h localhost -d postgres -c "CREATE ROLE memory_bank_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; CREATE ROLE memory_bank LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; CREATE DATABASE memory_bank OWNER memory_bank_owner;"
psql -h localhost -d memory_bank -c "CREATE EXTENSION vector;"                     # cluster superuser: pgvector is not a trusted extension
PGOPTIONS='-c maintenance_work_mem=1GB' pg_restore -h localhost -U memory_bank_owner -d memory_bank --exit-on-error --no-owner --no-acl --no-comments -j 4 /private/tmp/restore/payload/database.dump
psql -h localhost -U memory_bank_owner -d memory_bank -c "DELETE FROM schema_migrations WHERE version='017_role_separation.sql'; ALTER DATABASE memory_bank SET hnsw.ef_search = 200;"
git clone <origin> ~/second-brain && cd ~/second-brain && git checkout <commit recorded in repository/>   # or copy repository/ wholesale
bash migrations/migrate.sh                                                            # as memory_bank_owner: re-grants the app role (017), applies anything newer
cp /private/tmp/restore/payload/repository/.env .env && chmod 600 .env
rsync -a /private/tmp/restore/payload/knowledge-sources/ <path from config/backup.toml>/
uv venv .venv && uv pip sync --python .venv/bin/python requirements.lock
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/maintenance/smoke_app_role.py --json-indent 1   # as memory_bank: every application path ok, 0 permission errors
rm -rf /private/tmp/restore
```

Then validate retrieval, a synthetic embedding and model call, source identity/refresh and job statuses before installing the schedules (symlinks, [scheduling/README.md](../scheduling/README.md)). A legacy `memory_bank_<ts>.dump.gpg` restores the same way (it is a plain custom-format dump after decryption). Recovery success does not prove useful discoveries or that a static credential is still valid.

## Knowledge-graph archive (R7)

`scripts/maintenance/export_graph_tables.py` writes `backups/graph-export/graph-export_<UTC>.dump.gpg` (custom-format dump of `entities`, `entity_edges`, `memory_entities`, AES-256 with the backup key, same gpg invocation as the bundle), decrypts it again, compares SHA-256, checks `pg_restore --list` for the three `TABLE DATA` entries, writes `graph-export_<UTC>.receipt.json` (rows per table, ciphertext and plain hashes, bytes) and inserts a `graph-export` row into `corpus_maintenance` with the same counts. Migration 018 drops the tables only when the newest marker matches the live counts. The subdirectory is outside the nightly prune (which never descends). The archive is **local-only until the operator copies the ciphertext off-site** (`rclone copyto backups/graph-export/<file> gdrive:memory-bank-backups/graph-export/<file>`); nightly dumps that still contain the tables leave Drive after 30 days.

Recovery: `gpg --batch --pinentry-mode loopback --passphrase-file <key> --output /tmp/graph.dump --decrypt backups/graph-export/<file>`, then `pg_restore --no-owner --no-acl -h localhost -U memory_bank_owner -d memory_bank /tmp/graph.dump` recreates the three tables with their data and the FK to `memories` (tested); `rm -P /tmp/graph.dump`; delete the 018 ledger row only if the tables are meant to stay.

## Key custody

The key is the passphrase file `.backup-key` at the repository root, mode 0600, excluded from the bundle. A lost key makes every bundle unusable; a key that lives only on the same machine as the backups does not protect against loss of the machine. A byte-identical recovery copy was placed in the user's OneDrive folder `~/Library/CloudStorage/OneDrive-…/Second Brain Recovery/backup-key` (0600 inside a 0700 directory) on 2026-09-26. Local placement does not prove cloud synchronization or sharing restrictions: **verify through OneDrive from another device or session before treating loss-of-Mac recovery as established** (H-RECOVERY in [VALIDATION.md](VALIDATION.md)). The June runbook's claims of copies in Google Secret Manager and AWS Secrets Manager were not verified by the September audit and must not be relied on. Never paste the key into a chat, a log or git.

If the key is lost while the database still runs: take an unencrypted `pg_dump` to a private location immediately, generate a new key (`openssl rand -base64 32 > .backup-key; chmod 600 .backup-key`), place the recovery copy, run the backup by hand and verify it; the old bundles stay unreadable.

## Historical plaintext on Google Drive — CONFIRM-REQUIRED manual retirement

Until 2026-10-05 the nightly job synced `backups/kb-sources/` (the knowledge corpus) and `backups/config/` (docs, migrations, Kiro skills) to `gdrive:memory-bank-backups/{kb-sources,config}/` in plaintext. R1 stopped refreshing them; no job touches them (retention is `--include "*.gpg" --max-depth 1`). Remove them by hand **only after** a bundle receipt shows `"remote_verified": true` and the weekly verify has passed on it:

```bash
rclone lsf gdrive:memory-bank-backups/ --dirs-only          # LIST FIRST: expect exactly  config/  kb-sources/
rclone size gdrive:memory-bank-backups/kb-sources/; rclone size gdrive:memory-bank-backups/config/
# CONFIRM with the operator, then (Drive trash retention is the only undo):
rclone purge gdrive:memory-bank-backups/kb-sources/
rclone purge gdrive:memory-bank-backups/config/
rclone lsf gdrive:memory-bank-backups/ --dirs-only          # expect no output; ciphertext untouched
```

The local `backups/kb-sources/` and `backups/config/` mirrors are harmless and may be deleted once the first bundle is verified.

## Known failure modes

| Issue | Impact | Mitigation |
|---|---|---|
| Google OAuth token expiry | uploads fail → no off-site copy; the local bundle is still written and verified | `rclone config reconnect gdrive:`; local retention 7 d |
| PostgreSQL not running at 02:00 | backup exits 69, status `failed`, liveness reports it at 09:00 | `brew services restart postgresql@17`; jobs never auto-start it |
| Very slow link | the read-back doubles the transfer; a first-night run could exceed the 2 h deadline (exit 124, local bundle kept, no receipt) | raise `SECOND_BRAIN_JOB_TIMEOUT_BACKUP` in the plist if durations approach 90 min |
| Key copy unverified | loss of the Mac means loss of every bundle | verify the OneDrive copy from a second device (open item) |
