# Backup and recovery — original implementation

Source-reviewed 2026-09-10. This replaces earlier multi-cloud and estimated 15–30 minute recovery claims. **Current recovery point/time objectives require a successful timed restore test; none is certified by this documentation review.**

## What the backup script actually does

`scripts/jobs/backup.sh` targets PostgreSQL at 127.0.0.1:5432, creates a custom-format `pg_dump`, and JSON-lines exports for memories, entities, entity edges, and memory-entity links. It encrypts those five files with symmetric GPG AES256 using `~/second-brain/.backup-key`, uploads `.gpg` files through the configured `gdrive:` rclone remote, and attempts 7-day local / 30-day cloud retention.

It also copies Crawlee knowledge sources and a subset of configuration to local backup directories and syncs them to Google Drive **as plain files**. “All backups are encrypted” was incorrect. Configuration copying includes top-level docs, README, SQL, requirements, Docker-compose, scheduling README and Kiro skills; it is **not a full source checkout, full nested documentation set, complete schedules/backend configuration, or credential backup**. Preserve a separately versioned code/config snapshot for recovery.

| Asset | Present in scripted backup | Limitation |
|---|---|---|
| PostgreSQL tables including dream-cycle/feedback | Full custom dump when successful | Restore must be tested; dump-file presence alone is insufficient |
| JSON fallback | Four exported datasets listed above | Omits memory relationships, run/candidate history, Express feedback and migration ledger; no implemented complete JSON restore utility |
| Crawlee sources | Plain rsync/rclone copy | Current mirror, not historical version archive; does not cover all upstream app stores |
| Code/config/docs | Partial plain config copy plus separately maintained Git history | Git is only durable if intended commits exist at an accessible remote; uncommitted work is not protected by that assumption |
| QD databases, Kiro session store, Chrome state | Not backed up by this script | Separate upstream preservation required for a true capture replay |
| GPG key, AWS/CLI auth, Gmail/rclone secrets | Not part of the dump backup | Verify independent key custody privately; old cloud-secret locations are historical claims, not current proof |

The implemented remote backup destination is Google Drive. S3 upload/recovery is not an active fallback in these scripts.

## Failure behavior that needs repair

The script uses `set -uo pipefail`, not fail-fast `-e`, and does not check each `pg_dump`/export/GPG result before continuing. It removes plaintext files after attempted encryption and may report successful cloud copy even if a valid new dump was never produced. Secondary source/config sync errors are ignored. These are code-review findings, **not repaired by rewriting this guide**.

`verify_backup.sh` downloads the selected encrypted dump, decrypts it, and calls `pg_restore -l`. That validates readable archive structure; it does not restore rows/relationships or validate application behavior. The script uses a fixed temporary directory and deletes it at the end; do not run concurrent verification there.

## Recovery procedure with explicit gates

1. **Preserve evidence.** Identify the failed installation, last known good source revision/config, latest valid backup timestamp, and data since it. Do not restore over the only existing database.
2. **Prepare isolation.** Create a separate PostgreSQL 17/pgvector cluster/port and dedicated recovery directory with restricted permissions. Recover the matching source tree and requirements independently of the partial config backup.
3. **Select and decrypt.** Obtain the specific encrypted dump from the approved existing backup destination and the established key through the user's private custody process. Do not print the key or overwrite it with a newly generated one. Record hash/date/size and check decrypt success before proceeding.
4. **Inspect archive.** Use `pg_restore --list` on the decrypted file; review database-level commands, extension requirements, roles, and restore options. The custom dump is authoritative; JSON fallback has the limitations above.
5. **Restore into the empty recovery target.** Use explicit host/port/database and stop-on-error (`pg_restore --exit-on-error`), with ownership handling appropriate for the recovery role. Do not pipe into production or run the original migration shell against an assumed target.
6. **Verify contents and contracts.** Compare table counts, relationships, parents/chunks, latest completed runs, feedback rows, vector dimensions, required columns/indexes, and representative full source records against the backup inventory. Missing migration-ledger entries require schema inspection, not blind replay.
7. **Verify application behavior.** Start an MCP server with the recovery DB environment, read representative records, and test bounded search with synthetic/approved queries. Test synthesis separately only after provider and data-scope authorization. Ensure email/schedules cannot activate accidentally.
8. **Cut over only when reviewed.** Record restore duration, data loss window, functional results, and a rollback route. Repoint one component at a time; observe source/output freshness. Preserve the old target until recovery is accepted.

Recovery verification should write its manifest and results outside the live DB. A practical manifest contains code revision+diff hash, schema dump/ledger, source snapshot hashes, encrypted backup hash/date, decryption/restore exits, representative checks, provider checks, and elapsed time. The complete end-to-end evaluation additionally needs a human usefulness outcome; restoring the DB does not prove that outcome.

## Improvements in order

First make dump/encryption/transfer failure truthful and retain the last good artifact. Then test an actual isolated restore, account for complete code/config/upstream-state coverage, and verify off-machine key custody. Avoid promising a backup SLA from cron/plist schedules or `pg_restore -l` alone.
