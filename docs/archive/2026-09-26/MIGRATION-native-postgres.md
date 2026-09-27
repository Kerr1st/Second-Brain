# Migration Plan — Docker Desktop → native PostgreSQL + pgvector (Option B)

Status: **Phases 1–4 COMPLETE (2026-06-12)** — native PostgreSQL 17 + pgvector is the live DB (`127.0.0.1:5432`, brew services, localhost-only; 643/643 tests pass). **Phase 5 (decommission Docker) is pending** a 2–3 day soak + ≥1 successful scheduled nightly backup. Reversible until Phase 5 (the Docker container is retained, stopped).

## Why
The app talks to plain `localhost:5432` (env-driven in `src/db.py`) — it does not care whether
Postgres is containerized or native. Docker Desktop is only the engine, and its org sign-in
(Amazon-managed-device policy) just broke backups. Going native removes Docker entirely:
no Desktop, no sign-in, no licensing, no Linux VM, lower memory, simpler backups.

## Current source (verified 2026-06-12)
- Container `second-brain-db`, image `pgvector/pgvector:pg17` → **PostgreSQL 17.9 + pgvector 0.8.2**
- Data: bind mount `./docker/data` → `/var/lib/postgresql/data`; DB size ~3.1 GB
- Port `5432` published on `0.0.0.0` (LAN-exposed) ; creds `memory_bank` / `memory_bank` / db `memory_bank`
- Fresh validated backup exists: `memory_bank_20260612_053730.dump.gpg` (restorable, 9 tables; local + Google Drive)

## Target
Native **PostgreSQL 17 + pgvector** on `127.0.0.1:5432` (localhost-only), same db/user/password,
auto-start at boot. App code unchanged.

### Native engine choice (pick ONE toolchain — do not mix)
- **Postgres.app (recommended):** pgvector is **pre-bundled** (no build step), version-matched to its PG.
  GUI + CLI tools; set to launch at login. Lowest friction.
- **Homebrew `postgresql@17` + `pgvector` (headless alt):** scriptable via `brew services`; pgvector
  must build against PG17 (`pg_config` must point at postgresql@17, NOT a Postgres.app/other PG, or
  `CREATE EXTENSION vector` fails). More moving parts.

## Scope of changes (what gets touched)
- **App:** none (`src/db.py` already `localhost:5432`, env-overridable).
- **~8 job scripts using `docker exec`** (this also completes recommendation #2 "Docker decoupling"):
  `backup.sh`, `verify_backup.sh`, `ingest_staged.sh`, `dream_cycle_scheduled.sh`, `qd_sync.sh`,
  `reindex_embedding.sh`, `dedup_ide_backfill.sh`, `verify_liveness.sh`, `migrations/migrate.sh`.
  Pattern: `docker exec second-brain-db pg_dump|psql|pg_isready ...` → native `... -h 127.0.0.1`;
  "ensure container running" → "ensure brew service / Postgres.app running".
- **`tests/conftest.py`** + a few tests: confirm they connect via `localhost:5432` (then no change) vs `docker exec` (update).
- **`docker-compose.yml`:** archived/removed in Phase 5.

## Phases (each ends with a verify gate; rollback = restart container)

**Phase 0 — Safety. DONE.** Validated dump exists local + GDrive (9 tables, 122,154 memories, decrypts + `pg_restore -l` clean). The container + `docker/data` stay intact as the live rollback.

**Phase 1 — Install native (container still running).**
- Install Postgres.app 17 (or `brew install postgresql@17 pgvector`). Do NOT start on 5432 yet (clash).
- Verify: `psql --version` = 17.x ; pgvector files present.

**Phase 2 — Load data (container stopped so 5432 is free).**
1. `docker compose stop` (frees 5432; data dir preserved = rollback).
2. Start native PG on `127.0.0.1:5432`.
3. `createdb memory_bank` ; create role `memory_bank` (password `memory_bank`) ; `CREATE EXTENSION vector;`
4. `gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key <dump>.gpg | pg_restore -d memory_bank --no-owner`
5. **Verify gate:** `select count(*) from memories` = **122,154** ; 9 public tables ; `select extversion from pg_extension where extname='vector'` ; one vector similarity query returns rows.

**Phase 3 — Repoint operations (= recommendation #2).**
- Edit the ~8 scripts: `docker exec` → native `-h 127.0.0.1` (+ `~/.pgpass` for non-interactive auth).
- Update `conftest.py`/tests if needed.
- **Verify gate:** run the full suite (expect **643/643**) ; run `backup.sh` + `verify_backup.sh` natively and confirm a clean encrypted dump + GDrive upload.

**Phase 4 — Auto-start + security hardening.**
- Enable launch-at-boot (`brew services start postgresql@17`, or Postgres.app login item).
- `listen_addresses='127.0.0.1'` (closes today's 0.0.0.0 LAN exposure) ; confirm `pg_hba.conf` local auth.
- **Verify gate:** reboot (or restart service) → app + a scheduled job connect cleanly.

**Phase 5 — Decommission Docker (after ~2–3 days of confirmed native operation + ≥1 nightly backup cycle).**
- `docker compose down` ; archive then remove `docker-compose.yml` + `docker/data` (~3 GB reclaimed).
- Optionally uninstall Docker Desktop.

## Rollback (any time before Phase 5)
Stop native PG → `docker compose up -d` → back to exactly today's state (data dir untouched).

## Risks / notes
- **Toolchain mixing** is the #1 real gotcha (Postgres.app vs brew `pg_config`). Pick one, verify `pg_config --bindir`.
- **Collation:** dump/restore (not data-dir reuse) avoids Debian-glibc → macOS collation mismatches; indexes rebuilt natively on restore.
- **Auth:** keep password auth to match `DB_PASSWORD=memory_bank` (least change); scripts use `~/.pgpass`.
- pgvector minor may differ from 0.8.2 if brew is newer — forward-compatible; note the version after install.
