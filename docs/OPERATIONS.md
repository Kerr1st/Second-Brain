# Operations

Read [START-HERE.md](START-HERE.md) and record the actual host, checkout, schema ledger and loaded labels before acting. A schedule template does not establish installed state; a change to the source tree the launchd jobs execute is deployment. Which release is installed, and the per-release runbooks with their hard gates and rollbacks, are in [RELEASES.md](RELEASES.md) and the private notes it names.

## Development

Install `requirements.lock` into the checkout's `.venv` and run only

```sh
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q
```

The runner creates a fresh socket-only PostgreSQL cluster under `/private/tmp` with an owner role, a NOSUPERUSER application role and a legacy superuser role, applies the real migrations through `migrations/migrate.sh`, isolates HOME, metrics and credentials before import, denies outbound network to the process and its shell descendants, stops the cluster even on failure and keeps a dated evidence directory. Direct `pytest` refuses. Never point tests, migrations or maintenance scripts at the installed database as a development step.

## Database roles (migration 017)

| Role | Attributes | Owns | Used by |
|---|---|---|---|
| `memory_bank_owner` | LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE; no password while `pg_hba.conf` is `trust` for local connections | the database, every relation, index and function in `public`, the `vector` extension; default privileges grant the app role DML on future owner-created objects | `migrations/migrate.sh` (`DB_MIGRATE_USER`, the default), `scripts/jobs/reindex_embedding.sh`, `scripts/jobs/dedup_ide_backfill.sh` (VACUUM step), `scripts/cleanup_duplicate_sources.py --apply`, restore-based rollback and disaster recovery |
| `memory_bank` | after the operator step: NOSUPERUSER, NOCREATEDB, NOCREATEROLE | nothing | every job, the MCP servers, capture-api, `pg_dump` in the backup, the graph export and the smoke script |
| cluster superuser (the Homebrew account) | SUPERUSER | `postgres`, templates | `ALTER ROLE memory_bank NOSUPERUSER` and its reversal; `CREATE EXTENSION vector` on a brand-new database |

The app role holds `SELECT, INSERT, UPDATE, DELETE` on every table, `USAGE, SELECT` on sequences, `EXECUTE` on the trigger functions and `USAGE` on the schema — and nothing structural: no `TRUNCATE`, `REFERENCES`, `TRIGGER`, `MAINTAIN`, `CREATE`, no `ALTER DATABASE … SET`, no membership of the owner. Session GUCs (`statement_timeout`, `lock_timeout`, `hnsw.iterative_scan`), read-only transactions and advisory locks need no privilege, so every application path works unchanged; the suite proves it by running every application test as the NOSUPERUSER role. `MAINTAIN` is deliberately not granted; autovacuum covers the app and the post-bulk-delete rebuild is the owner's `reindex_embedding.sh`.

`migrate.sh` connects as `DB_MIGRATE_USER` (default `memory_bank_owner`; `DB_MIGRATE_PASSWORD` if authentication is ever tightened), takes `DB_OWNER_ROLE` and `DB_APP_ROLE` from the environment (role names are never in the SQL files), and exits 2 before touching anything when the connected role is not the database owner, when owner and app role are the same name, or — inside 017 — when the app role is a member of the owner or lacks DML afterwards. `MIGRATE_UNTIL=<version file>` stops after a given file; it exists for one purpose: the **bootstrap** run on an installation whose `memory_bank` is still the superuser owner, `DB_MIGRATE_USER=memory_bank DB_OWNER_ROLE=memory_bank_owner MIGRATE_UNTIL=017_role_separation.sql bash migrations/migrate.sh`, the only `migrate.sh` run ever made as the app role. 017 creates the owner, `REASSIGN OWNED BY memory_bank`, grants DML and default privileges and verifies them; it does **not** demote `memory_bank` — a migration cannot safely demote its own session, and until `ALTER ROLE memory_bank NOSUPERUSER NOCREATEDB NOCREATEROLE` is run by the cluster superuser the grants are inert (017 says so with a NOTICE). The demotion is the one step that changes what running services may do; its reversal is `ALTER ROLE memory_bank SUPERUSER`, immediate, no data involved. `REASSIGN OWNED` also moves ownership of any other databases the app role owned (stale test databases on the installed cluster).

Verification tools: `scripts/maintenance/prepare_release.py --roles-and-indexes --output <file>` writes a read-only role audit (`currently_superuser`, `structural_privileges`, `objects_not_owned_by_owner`, `remaining_steps`); `scripts/maintenance/smoke_app_role.py` connects as the configured role, exercises the read paths (list, read, graph, search legs, dream-cycle stats, feedback, briefing, run lock, capture lookups), does one write round trip inside a rolled-back transaction and probes the structural statements expecting refusal, and prints a JSON report (exit 0 only when every application step is ok). `psql -U memory_bank` is for DML; schema work is `psql -U memory_bank_owner`. Disaster recovery into a fresh database needs the 017 re-grant step ([DISASTER-RECOVERY.md](DISASTER-RECOVERY.md)).

## Scheduling and truthful health

[scheduling/README.md](../scheduling/README.md) is the job table (17 plists, 16 labels loaded on the installed Mac, `distill` disabled by the operator) and the environment contract; [the generated inventory](generated/runtime-inventory.md) is the cadence table derived from the plists. Calendar entries are host local time; Weekday 1/3/5 at 02:00 is Monday/Wednesday/Friday. Plists are installed as **symlinks**, so a checkout changes the job definitions in place, effective at the next `launchctl bootout` + `bootstrap`; a copied plist silently keeps an old definition (the R5 runbook replaces the last two copies).

Every job runs `scripts/jobs/job_wrapper.sh` → `python -m src.operations.jobs <script> <job-name>`: PATH, `AWS_PROFILE=second-brain-jobs`, `SECOND_BRAIN_PROFILE=controlled`, `umask 077`, `caffeinate`, one `flock` per job, a committed per-job deadline with floors the runner never undercuts (dream-cycle 6 h, bookmarks 3 h, backup/verify/weekly-digest/codex-capture/distill 2 h, others 1 h), SIGTERM → 120 s grace → SIGKILL, the per-day log `logs/<job>-YYYYMMDD.log` and the durable status `logs/jobs/<job>.json` (state, exit code, timings, timeout source, last success, consecutive failures, first failure, last notification). Capture-api runs in service mode: bounded startup, `/health` probes, no deadline while healthy, back-off and at most one notification per 30 minutes during an outage. Jobs that need the network wait for DNS first (`SECOND_BRAIN_JOB_NEEDS_NETWORK=1`, up to 15 minutes under a sleep assertion) and exit 68 without starting if it never comes — dark-wake launches were the root cause of the chronic Monday weekly-digest failure. PostgreSQL is never auto-started by a job: `scripts/jobs/_preflight.sh` exits 69 with a named cause. Exit-code taxonomy and every variable: [scheduling/README.md](../scheduling/README.md).

Liveness (`com.second-brain.liveness`, 09:00, `config/liveness.toml`): required channels with freshness thresholds; advisory channels with a mandatory reason (`distilled_chat` while distill is disabled, `weekly_digest` until two consecutive OK Mondays, `quick_desktop_synth` while the upstream Quick export is stale); a generic "produced zero rows for longer than its own cadence tolerates" rule over every `source_type`; `dream_cycle_runs.completed_at` ≤ 7 days and zero-candidate-run detection; a sweep of `logs/jobs/*.json` for non-zero last exits, overdue runs, stale service heartbeats and repeated `network_unavailable`. Every finding lands in `logs/jobs/liveness-report.json`; the desktop notification is a supplement. A running process with no useful output is unhealthy.

Backups and restore: [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md).

## Release sequence

Each release has a private runbook with a hard gate, a quiesce, a backup, the switch, $0 smoke checks, bootstrap and rollback; the shape is always:

1. **Gate.** Installed `HEAD` equals the expected previous release, the working tree is clean, the migration ledger's count and `max(version)` are exactly what the release expects, the release branch contains the installed head, the backup key is 0600, required tools are on PATH. Any failure stops the window before anything changes.
2. **Quiesce.** `launchctl bootout` every label (never `disable`/`enable`: `distill` must stay disabled), check `pgrep` for job processes and `src.mcp_server` processes started by editor sessions (they keep the old code loaded and hold connections), and check `pg_stat_activity` for `memory_bank` until only this session remains. Quiesce comes **before** the backup so the backup is the exact rollback point.
3. **Backup.** Run the backup through the wrapper and verify the receipt (`remote_verified`, `payload_verified`) or `pg_restore --list` the dump. Record the file name.
4. **Switch.** `git merge --ff-only release/<name>` in the installed checkout; verify `HEAD` and that the expected files appeared or disappeared; reinstall changed plists as symlinks.
5. **Migrate**, when the release has one, as the owner role, recording the output; a lock-timeout failure rolls back the whole file and is re-run after the holder is found, not a rollback situation.
6. **$0 smoke.** Imports, profile, ledger count, a list/graph read, `bash -n` of the job scripts, the wrapper with a trivial script, the role audit and app-role smoke after 017 — no model calls, no writes.
7. **Bootstrap** the 16 labels, watch capture-api come up healthy, run liveness by hand, restart editor sessions so their MCP servers load the new code, and check the next scheduled runs.
8. **Rollback** = `git checkout -B main <previous head>` plus the previous plists; when a migration committed, restore the pre-window backup as the owner first. Git restores source; it cannot recover deleted data.

Nothing in this sequence resumes the disabled distiller or enables e-mail delivery. Do not run a window between 01:30 and 05:00, within five minutes of the hour (qd-sync) or on Monday/Wednesday/Friday 02:00–06:00.

## Corpus maintenance (R6, pending authorization)

`scripts/maintenance/dedup_corpus.py --output <manifest>`, `rechunk_and_embed.py --output <manifest>` and `prepare_release.py` are dry-run planners. Apply modes take the manifest and its SHA-256; the manifest binds the database incarnation and row hashes and rejects stale plans; paid embedding additionally requires `--allow-paid-embedding`. Order: metadata normalization (historical UUID project tags) → dedup of proven duplicate captured-source identities, remapping every foreign key, JSON/array id, slice/skim reference and lineage → rechunk and re-embed of the remaining necessary rows, with validated vectors cached for resume and old cited chunks kept as superseded history. Keep manifests private; they identify corpus records. Regenerate and re-price them after R4's capture runs; never install a global source-URL uniqueness constraint (derived Codex task memories share URLs). The `corpus_maintenance` table is the ledger of applied operations (including the graph export marker).

## Knowledge-graph retirement (R7)

`scripts/maintenance/export_graph_tables.py --dry-run` prints the counts; without the flag it writes `backups/graph-export/graph-export_<UTC>.dump.gpg` (AES-256 with the backup key), verifies it by decrypting and listing, writes a receipt and inserts the `graph-export` marker into `corpus_maintenance`; a count change during the export aborts it. Migration 018 (owner) drops `entities`, `entity_edges`, `memory_entities` only when the newest marker's counts match the live tables. The archive is local-only until copied off-site; the nightly dumps that still contain the tables age out of Drive after 30 days. Recovery: [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md).

## Authentication and credentials

Jobs use the static IAM profile `second-brain-jobs` for Bedrock; interactive shells use SSO. Never print credential contents; `~/.aws` residue files must be 0600 like the active ones. Replacing the static key requires a working unattended credential provider, permission and expiry verification and synthetic model probes before the identified old key is revoked with a metadata-only receipt; deleting a local file is not revocation. The capture-api token lives only in `.env` (0600). Google Drive (`rclone`), Gmail and model credentials are separate dependencies. Open items are listed in [PLAN-refactor.md](PLAN-refactor.md).

## Database basics

PostgreSQL 17 + pgvector run natively via Homebrew (`postgresql@17`, keg-only: `/opt/homebrew/opt/postgresql@17/bin` must be on PATH), serving `127.0.0.1:5432`. `brew services start|restart postgresql@17`; `pg_isready -h 127.0.0.1 -p 5432 -U memory_bank`; `psql -h 127.0.0.1 -U memory_bank -d memory_bank` for DML and `-U memory_bank_owner` for schema work; `bash migrations/migrate.sh` as the owner is idempotent (`skip: … (already applied)` for every file). Session-level `PGOPTIONS='-c maintenance_work_mem=1GB'` speeds a restore's HNSW build without a server restart.

## Monitoring checklist

| Check | Command | Expected |
|---|---|---|
| Labels loaded | `launchctl list \| grep -c second-brain`; `launchctl print-disabled gui/$(id -u) \| grep distill` | 16; `disabled` |
| Every job's last run | `for f in logs/jobs/*.json; do python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['job'], d['state'], d.get('exit_code'), d.get('timeout_s'))" "$f"; done` | `succeeded 0` (capture-api `running`) |
| Liveness | `python3 -c "import json; r=json.load(open('logs/jobs/liveness-report.json')); print(r['healthy'], r['required_failures'], r['advisories'])"` | `True [] [...]` |
| Backup | `tail -2 logs/backup-$(date +%Y%m%d).log`; `ls -t backups/*.receipt.json \| head -1 \| xargs cat` | `OK (exit 0)`; `"remote_verified": true` |
| Dream cycle | `psql … -c "SELECT started_at, completed_at, candidates_generated, candidates_accepted, cost_usd FROM dream_cycle_runs ORDER BY started_at DESC LIMIT 1"` | `completed_at` set after Mon/Wed/Fri 02:00 |
| Roles | `.venv/bin/python scripts/maintenance/prepare_release.py --roles-and-indexes --output /tmp/audit.json` | `remaining_steps: []`, `currently_superuser: false` after R7 |
| PostgreSQL | `pg_isready -h 127.0.0.1 -p 5432 -U memory_bank` | accepting connections |
| Ledger | `psql … -tAc "SELECT count(*), max(version) FROM schema_migrations"` | matches [RELEASES.md](RELEASES.md) for the installed release |
