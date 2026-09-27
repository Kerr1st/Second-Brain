# Backup and recovery

The repaired backup job creates one encrypted bundle containing a custom PostgreSQL dump, repository source/configuration/documentation, and explicitly configured knowledge sources. It does not depend on the dormant graph tables: the database dump includes whichever tables actually exist. `SECOND_BRAIN_KB_SOURCES` must point to the intended source directory; a configured missing or symlink directory fails instead of silently omitting it.

## Backup contract

`scripts/maintenance/backup_bundle.py create` builds a private temporary payload, records every file's SHA-256, size and private permission mode (0600, or 0700 for owner-executable scripts), verifies the custom dump structure, encrypts with GPG AES256, decrypts and verifies the resulting payload, then publishes the local ciphertext. It never deletes original sources or starts a database service. A failed dump/encryption/verification is a failed job.

Only ciphertext is uploaded. The cloud copy is downloaded again and its checksum compared before remote success is reported. There is no plaintext `rclone sync`, implicit remote deletion or unconditional retention cleanup. A verified local bundle remains recoverable if cloud upload fails. Existing historical plaintext cloud/source backups require a separate inventory and reviewed retirement; new encryption does not retroactively protect them.

Manifest version 2 preserves owner execution while removing group/public access. Legacy version 1 can be checked for content but cannot establish executable permissions. The receipt distinguishes payload, file-mode, full restore and cloud verification. Merely listing an archive is not a restore test. PostgreSQL recommends custom-format dumps for flexible restores; a dump is a trusted-code input and should only be restored from the expected source. [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)

## Verify without touching the installed database

```sh
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/maintenance/backup_bundle.py verify /absolute/path/second-brain_TIMESTAMP.tar.gpg --key /absolute/path/backup-key --restore
```

The verifier decrypts into a mode-0700 temporary directory, rejects unsafe archive paths/symlinks and unexpected or changed members, then restores into a newly initialized socket-only PostgreSQL cluster. It never accepts an existing database as the restore target. The cluster is stopped and temporary plaintext is removed. Interruptions unwind subprocess groups and stop the temporary database before cleanup. A failure must be resolved before corpus/schema maintenance.

The scheduled verification script downloads the newest encrypted bundle and runs this full restore check. PostgreSQL 17, pgvector and GPG must be available to the wrapper; rclone is required only for cloud operations.

## Key custody

The installed key remains `/Users/kerrfir/second-brain/.backup-key`, mode 0600. At the user's request a matching recovery copy was placed at:

`/Users/kerrfir/Library/CloudStorage/OneDrive-amazon.com/Second Brain Recovery/backup-key`

The copy was compared byte-for-byte and restricted locally to 0600 inside a 0700 directory. Local OneDrive placement does not prove cloud synchronization or cloud sharing restrictions. Verify the file exists through OneDrive on another device/account session before treating loss-of-Mac recovery as established. Keep the folder private. Do not paste the key in a chat or commit it to Git.

A lost key makes encrypted bundles unusable. A key in the same physical machine as the only backup does not protect against loss of the machine. The key must be independent from the encrypted backup's failure domain.

## Restore a replacement installation

After an isolated verification passes, install the recorded code and locked dependencies on the replacement host; create owner/runtime roles explicitly; restore the dump into a chosen new database; recover required source/config files from the verified encrypted payload; set local secret permissions; and apply only qualified subsequent migrations. Validate retrieval, source identity/refresh, a synthetic embedding/model call, read-only discovery and job statuses before enabling schedules.

Use the saved code/schema/config receipt to distinguish a restored old release from a new migration. Recovery success does not prove useful discoveries or that a previous static credential remains valid.

## September 26 recovery evidence

The full v2 bundle restored successfully from the OneDrive key copy, including the database, repository files and configured knowledge sources. Migrations and unpaid repairs also passed in its disposable clone. The plaintext clone was stopped and removed. A separate encrypted preservation archive covers 1,512 ignored backup/log/fixture files and verifies every restored hash; the main backup deliberately excludes those directories. Neither archive causes original deletion. Exact hashes, cloud-copy state and qualification receipts are retained in the private workspace evidence and summarized in the [completion ledger](REFACTOR-COMPLETION-2026-09-26.md).
