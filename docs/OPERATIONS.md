# Operations

Read [Start here](START-HERE.md) and record the actual host/checkout/schema before acting. A schedule template does not establish installed state. Changes to a source tree used by launchd jobs are deployment.

## Development

Install `requirements.lock` into the isolated checkout's virtual environment. Run only:

```sh
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q
```

The runner creates a fresh Unix-socket-only PostgreSQL cluster and non-superuser test role, isolates HOME/metrics/credentials before import, applies the real transactional migrations and blocks outbound network/provider/CLI access, including shell descendants. It stops the cluster even on failure and retains a dated evidence directory. Direct pytest is disabled.

## Scheduling and truthful health

[The generated schedule table](generated/runtime-inventory.md) is derived from the plists. Regenerate it after schedule changes. Calendar entries use the host's local timezone; Weekday 1/3/5 at 02:00 means Monday/Wednesday/Friday. Calendar availability and a closed laptop lid require observed verification.

The shared wrapper supplies PATH, repository directory, virtual environment, AWS profile, sleep prevention, logs, locking and a timeout. It deliberately handles status explicitly rather than relying on `set -e`. Per-job scripts preserve command exit taxonomy. A bounded runner status file under `logs/jobs/` records start/finish, exit, timeout, last success, consecutive failures and the first unresolved failure. These durable records work on a headless host; notifications supplement them. Batch jobs have a deadline. The Capture API service has a bounded startup and recurring local health probes instead: a fresh heartbeat marks health, repeated failed probes stop the service, and an unexpected normal exit is a failure. Signals stop the owned process group with bounded TERM/KILL cleanup and persist an interrupted status.

Liveness checks both fresh output and silence from a channel that previously produced data. Distilled chat, weekly digest, IDE capture and completed Dream runs are monitored, as are persistently failing/overdue wrapper records. A working process with no useful output must not count as healthy. Run the deliberately broken-channel fixture before installing the schedule changes.

## Qualified release sequence

1. Record original HEAD/diff, config identities, schema and migration ledger, enabled/disabled jobs and last successful artifacts. Preserve original dirty files separately.
2. Create an encrypted database/config/source backup and verify a full restore in a new isolated cluster. Verify independent key custody. [Recovery](DISASTER-RECOVERY.md)
3. Run the isolated suite and inspect migrations against a restored snapshot. Source duplication and historical vocabulary must be reconciled before validating stricter indexes/constraints. `scripts/maintenance/qualify_release.py run` takes a selected encrypted backup, its SHA-256, recovery key, new output directory and explicit embedding planning rate. It restores only into its own temporary socket-only cluster, applies migrations and unpaid repairs there, and prices the post-dedup rechunk plan without provider access. Its manifests target that clone and cannot authorize live application.
4. Prepare a separate owner role for schema changes and a runtime role without superuser/role-creation/database-creation privileges. Grant only required schema/table/sequence rights. Verify readers/writers with the runtime role before revoking the former permissions. Role names belong in an explicit deployment configuration, not hardcoded application migrations.
5. Apply the reviewed release to its explicit target with affected jobs stopped and record each migration result. Do not overwrite unrelated dirty work. Revalidate runtime writes, read-only Explorer behavior, source refresh and liveness, then restore only the intended job enablement.
6. Keep source and data rollback boundaries separate. Git can restore source; it cannot recover deleted corpus/graph data. Prefer a verified restore or a reviewed forward repair over assuming every SQL file is reversible.

Nothing in this runbook automatically resumes the paused distiller or enables email.

## Corpus maintenance

`python -m scripts.maintenance.dedup_corpus --output <manifest>` and `python -m scripts.maintenance.rechunk_and_embed --output <manifest>` are dry-run planning commands. Apply uses an explicit manifest and matching SHA-256; paid embedding additionally requires `--allow-paid-embedding`. Manifests bind the actual database incarnation and row hashes and must reject stale plans. Keep them private because they identify corpus records.

Deduplicate proven captured-source identities first, remapping all foreign keys, JSON/source arrays, slice/skim evidence and lineage. Rechunk/re-embed the remaining necessary records. Replacement data is prepared before transactional writes and embedding results are cached for resume. Do not install a global source-URL uniqueness constraint that conflates distinct derived task memories. Produce a current post-dedup price estimate rather than reusing September audit counts.

## Authentication and credentials

Never print credentials. Backups/residue files in `~/.aws` require 0600 as well as the active files. Replacing the static job key requires a functioning unattended credential provider, permissions/expiry verification and synthetic model probes. Deleting a local file is not revoking an IAM key. Revoke only the identified old key after the replacement is verified and recovery is available; retain a metadata-only receipt.

Provider routing and costs are in [Models](MODEL-BACKENDS.md). Cloud backup authentication, email credentials and model credentials are separate dependencies.

## Retirement preparation

`scripts/maintenance/prepare_operational_retirement.py` writes a private read-only inventory of audited ignored artifacts, installed plist copies, credential mechanisms, orphaned Kiro agents and optional graph-table dependencies. It stores file hashes and metadata, not credential values. Identical plist copies can be distinguished from divergent user edits. A retirement manifest is not a preservation receipt or permission to delete data; ignored backup/log directories need independent encrypted preservation before removal. Graph deletion needs the verified full database restore and a checked dependency order, with no blanket CASCADE.
