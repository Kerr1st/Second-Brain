# Agent Memory Architecture — Disaster Recovery Runbook

> Last updated: 2026-10-06 (R5 — encrypted bundles)
> Backup schedule: Daily at 2:00 AM (launchd `com.second-brain.backup`, via `job_wrapper.sh`, deadline 2 h)
> Verification schedule: Weekly, Sundays at 3:00 AM (`com.second-brain.verify`, deadline 2 h)
> Encryption: AES-256 via GPG (symmetric, passphrase file `.backup-key`, 0600)
> RPO: 24 hours | RTO: ~15-30 minutes

> ## BACKUP POSTURE — single-cloud (Google Drive) + local · ACCEPTED 2026-06-12
> **AWS S3 was de-scoped** (brittle overnight SSO). Backups go to **Google Drive + local
> only** (code/config also in git on `origin`/`mini`). Scenario 2 (S3) is UNAVAILABLE and
> retained for historical reference only. Google Drive is the sole offsite path — the
> weekly verify job and the 7-day local copy are the safety net.

## R5 backup contract (`scripts/jobs/backup.sh` → `scripts/maintenance/backup_bundle.py create`)

| | Pre-R5 (`memory_bank_<ts>.dump.gpg` + 4 `*.json.gpg`) | R5 (`second-brain_<UTC stamp>.tar.gpg` + `.receipt.json`) |
|---|---|---|
| Database | `pg_dump -Fc` | same, `--no-owner --no-acl`, inside the bundle as `database.dump` |
| JSON fallbacks | 4 `row_to_json` exports (incl. dormant KG tables) | dropped — the custom dump is restore-tested weekly instead |
| Repository | `backups/config/` copies of docs/migrations/README (local only after R1) | `repository/{src,scripts,docs,config,migrations,scheduling,.kiro,README.md,requirements*.txt,requirements.lock,docker-compose.yml,pyproject.toml}` inside the bundle |
| Knowledge sources | `rsync` to `backups/kb-sources/` (local only after R1) | `knowledge-sources/` inside the bundle, path from `config/backup.toml` (`~/Work/Tools/Crawlee/knowledge/sources`); a configured path that is missing/a symlink **fails the job** (E-N3) |
| Secrets | none | `repository/.env` (`CAPTURE_API_TOKEN`) is **inside the bundle on purpose** — a replacement host needs it. Acceptable only because the bundle is ciphertext end to end: plaintext exists in a 0700 temp dir under `backups/` for the duration of the run and is deleted before the `.tar.gpg` is published; rclone is handed nothing but the `.tar.gpg` (`tests/test_backup_bundle.py::test_only_the_encrypted_bundle_is_ever_passed_to_rclone`). `.backup-key` itself is excluded. |
| Integrity | gpg decrypts | manifest v2: SHA-256 + size + mode (0600/0700) per file, `pg_restore --list` of the dump, decrypt-and-verify of the just-written ciphertext before publishing |
| Cloud | `rclone copy backups/ … --include "*.gpg"` + `rclone lsf` | `rclone copyto <bundle> gdrive:memory-bank-backups/<bundle>`, then **download it again and compare SHA-256** (`remote_verified` in the receipt) |
| Retention (E-N2) | local 7 d / remote 30 d (`*.gpg`) | **same policy**, from `config/backup.toml`: local `*.gpg` + `*.receipt.json` older than 7 d at the root of `backups/` (both naming schemes, so the pre-R5 files age out too); remote `rclone delete gdrive:memory-bank-backups/ --min-age 30d --include "*.gpg" --max-depth 1`. Pruning runs **only after** the new bundle is verified (remote only after read-back matched). `0` disables a leg. |
| Plaintext on the remote | the two `rclone sync`s of kb-sources/config stopped in R1; historical trees frozen | **never touched by any job**; retirement is the CONFIRM-REQUIRED manual step below |
| Postgres down | `brew services start` silently | exit 69, no auto-start (`scripts/jobs/_preflight.sh`) |

Receipt (`backups/second-brain_<stamp>.receipt.json`): `sha256`, `bytes`, `files`, `payload_verified`, `file_modes_verified`, `remote_verified`, `knowledge_sources_included`, `pruned_local` (names), `pruned_remote` (bool), `retention`.

### Verify (weekly, `scripts/jobs/verify_backup.sh`)
1. `rclone lsf gdrive:memory-bank-backups --files-only --max-depth 1 --include '*.gpg'` — **both** naming schemes are listed, because during the transition the remote holds pre-R5 dumps next to bundles.
2. `backup_bundle.py select-latest`: newest by the timestamp in the name; **fails if older than 36 h** (`[verify].max_age_hours`; E-N9 — a Drive copy that stopped updating no longer passes because it decrypts).
3. Bundle → `backup_bundle.py verify <file> --restore`: manifest checksums/modes, then a **full `pg_restore` into a fresh socket-only cluster** under `/private/tmp`, reporting per-table row counts; legacy dump → `backup_bundle.py verify-legacy` (decrypt + `pg_restore --list`, the pre-R5 check), so old files stay verifiable until they age out.
Time budget: ~1.1 GB download (Drive-bound) + decrypt ~10 s + restore ~4 min (219 s on the R3 clone); per-command cap 3600 s, job deadline 7200 s.

### Restore a bundle (new machine or corrupted database)
```bash
# key: ~/second-brain/.backup-key (or the OneDrive recovery copy — see "Key custody" / "Encryption Key Storage")
rclone lsf gdrive:memory-bank-backups --files-only --include 'second-brain_*.tar.gpg' | sort | tail -1   # newest bundle
rclone copyto gdrive:memory-bank-backups/<bundle> /private/tmp/restore/<bundle>
# 1. verify (incl. a full restore into a throwaway cluster; prints row counts)
SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/maintenance/backup_bundle.py verify /private/tmp/restore/<bundle> --restore --key ~/second-brain/.backup-key
# 2. unpack for a real restore
umask 077; mkdir -p /private/tmp/restore/payload
gpg --batch --pinentry-mode loopback --passphrase-file ~/second-brain/.backup-key --output /private/tmp/restore/payload.tar --decrypt /private/tmp/restore/<bundle>
tar -xf /private/tmp/restore/payload.tar -C /private/tmp/restore/payload      # database.dump, manifest.json, repository/, knowledge-sources/
# 3. database (native PG 17 + pgvector; `migrations/` are NOT needed — the dump carries the schema and the schema_migrations ledger)
brew services start postgresql@17
createuser -s memory_bank 2>/dev/null || true; createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"
PGOPTIONS='-c maintenance_work_mem=1GB' pg_restore -h localhost -U memory_bank -d memory_bank --no-owner --no-acl -j 4 /private/tmp/restore/payload/database.dump
psql -h localhost -U memory_bank -d memory_bank -c "ALTER DATABASE memory_bank SET hnsw.ef_search = 200"   # database-level GUC is not in a dump
# 4. code, config, secrets, knowledge sources
git clone <origin> ~/second-brain && cd ~/second-brain && git checkout <commit from repository/…>   # or copy repository/ wholesale
cp /private/tmp/restore/payload/repository/.env ~/second-brain/.env && chmod 600 ~/second-brain/.env
rsync -a /private/tmp/restore/payload/knowledge-sources/ ~/Work/Tools/Crawlee/knowledge/sources/
uv venv .venv && uv pip sync --python .venv/bin/python requirements.lock
# 5. clean up plaintext
rm -rf /private/tmp/restore
```
Legacy `memory_bank_<ts>.dump.gpg` files restore as before (Scenario 3 below).

### Historical plaintext on Google Drive — CONFIRM-REQUIRED manual retirement
Until 2026-10-05 the nightly job `rclone sync`ed `backups/kb-sources/` (personal knowledge corpus) and `backups/config/` (docs, migrations, Kiro skills) to `gdrive:memory-bank-backups/{kb-sources,config}/` in **plaintext**. R1 stopped refreshing them; no R5 job touches them (retention is `--include "*.gpg" --max-depth 1`). They should be removed, by hand, **only after** the first R5 bundle's receipt shows `"remote_verified": true` and `verify_backup.sh` has passed on it — the bundle then contains everything those trees held, encrypted.
```bash
# 1. LIST FIRST (read-only) — confirm these are the two plaintext trees and nothing else
rclone lsf gdrive:memory-bank-backups/ --dirs-only                 # expect exactly: config/  kb-sources/
rclone size gdrive:memory-bank-backups/kb-sources/
rclone size gdrive:memory-bank-backups/config/
rclone lsf gdrive:memory-bank-backups/kb-sources/ --recursive | head -20
rclone lsf gdrive:memory-bank-backups/config/ --recursive | head -20
# 2. CONFIRM with the operator, then purge (irreversible on Drive after the trash window)
rclone purge gdrive:memory-bank-backups/kb-sources/
rclone purge gdrive:memory-bank-backups/config/
# 3. verify
rclone lsf gdrive:memory-bank-backups/ --dirs-only                 # expect no output
rclone lsf gdrive:memory-bank-backups/ --files-only --include '*.gpg' | wc -l   # ciphertext untouched
```
(`rclone delete` removes files but leaves the directory objects; `purge` removes the tree. The local `backups/kb-sources/` and `backups/config/` directories are harmless and may be deleted once the first bundle is verified.)

---

### Known Failure Modes

| Issue | Impact | Mitigation |
|---|---|---|
| Google OAuth token expiry | Google Drive uploads fail → **no offsite backup** (S3 de-scoped, so Google is the only cloud) | Uses refresh token — hasn't expired in practice. Re-auth: `rclone config reconnect gdrive:`. Local copy still retained 7 days. |
| PostgreSQL not running at 2 AM | Backup exits 69 (`PostgreSQL is not reachable … not auto-started`), recorded in `logs/jobs/backup.json`, reported by the 09:00 liveness sweep | `brew services restart postgresql@17`; jobs no longer auto-start the service (R5, see `scripts/jobs/_preflight.sh`) |
| ~~AWS SSO token expiry~~ | ~~S3 uploads fail~~ | **N/A — S3 de-scoped 2026-06-01.** SSO now only affects Bedrock embedding/generation. |

### AWS SSO Recovery

```bash
# Check token status
aws sts get-caller-identity

# Refresh (opens browser)
aws sso login --profile default

# Profile: default → SSO session kiro-admin-session
# Start URL: https://d-9066306d67.awsapps.com/start
# Account: 048588987132, AdministratorAccess role
```

---

## Backup Strategy (Google Drive + local — S3 de-scoped 2026-06-01)

```
                         Local Machine
                              │
                   daily encrypted backup (GPG AES-256)
                         ┌────┴────┐
                         ▼         ▼
                  Google Drive    Local disk
                  ┌──────────┐  ┌──────────┐
                  │ pg_dump   │  │ pg_dump   │
                  │ JSON      │  │ JSON      │
                  │ KB sources│  │ (recent)  │
                  │ Config    │  │           │
                  └──────────┘  └──────────┘
                  30-day cloud   7-day local
                  retention      retention

                  Key: Google Secret Mgr + ~/second-brain/.backup-key
```

Google Drive is the sole offsite copy; the 7-day local retention + git (code/config on `origin`/`mini`) are the other safety nets.

---

## What's Backed Up (and Where)

| Asset | Google Drive | Local |
|---|---|---|
| Layer 2: PostgreSQL dump (.dump.gpg) | ✅ | 7-day |
| Layer 2: JSON — memories (.json.gpg) | ✅ | 7-day |
| Layer 2: JSON — entities (.json.gpg) | ✅ | 7-day |
| Layer 2: JSON — entity_edges (.json.gpg) | ✅ | 7-day |
| Layer 2: JSON — memory_entities (.json.gpg) | ✅ | 7-day |
| Layer 3: Knowledge base source docs | ✅ | Local copy |
| Layer 1: Kiro skills / bootstrap | ✅ | `~/.kiro/skills/` |
| Config: docker-compose.yml, schema.sql | ✅ | `~/second-brain/` (+ git) |
| Config: backup & verify scripts | ✅ | `~/second-brain/` (+ git) |
| Config: this runbook | ✅ | `~/second-brain/` (+ git) |
| Encryption key | ✅ Google Secret Mgr | `~/second-brain/.backup-key` |

### Data Sources (upstream of Second Brain)

These are NOT backed up by this script because they are independent sources of truth:

| Source | Location | What it holds |
|---|---|---|
| Quick Desktop | `~/.quickwork/knowledge_v1.db` | 815+ memories, 2100+ entities, 4500+ edges |
| Kiro CLI chat histories | `~/.kiro/` | All conversation sessions |
| KB source files | `~/Work/Tools/Crawlee/knowledge/sources/` | Crawled articles, docs |

If the Second Brain is lost, it can be rebuilt from these sources via the migration scripts.

## What's NOT Backed Up (by design)

- Vector indexes (Layer 2 pgvector + Layer 3 Bedrock) — rebuilt automatically from source data
- PostgreSQL service config — managed by Homebrew (`brew services`); reinstall via `brew install postgresql@17 pgvector`
- rclone config — re-authorize in 2 minutes (interactive)

---

## Encryption Key Storage

The key is stored in three places. Any ONE is sufficient to decrypt backups.

| Location | How to Access | When to Use |
|---|---|---|
| Local file | `cat ~/second-brain/.backup-key` | Normal daily operations |
| Google Secret Manager | `gcloud secrets versions access latest --secret=memory-bank-key` | Local machine gone, no AWS |
| AWS Secrets Manager | `aws secretsmanager get-secret-value --secret-id memory-bank-key --query SecretString --output text --region us-east-1` | Local machine gone, no Google |

### Initial Key Setup (one-time, during system build)

```bash
# Generate the key
mkdir -p ~/second-brain
openssl rand -base64 32 > ~/second-brain/.backup-key
chmod 600 ~/second-brain/.backup-key

# Store in Google Secret Manager
gcloud secrets create memory-bank-key --data-file=~/second-brain/.backup-key

# Store in AWS Secrets Manager
aws secretsmanager create-secret \
  --name memory-bank-key \
  --secret-string "$(cat ~/second-brain/.backup-key)" \
  --region us-east-1
```

---

## Backup Script

`scripts/jobs/backup.sh` (preflight: tools, `pg_isready`) → `scripts/maintenance/backup_bundle.py create` (policy in `config/backup.toml`). See "R5 backup contract" at the top. Do not duplicate the script here — see the source for the current implementation. The "What's Backed Up" table and Scenarios 1/3/4 below describe the **legacy** `.dump.gpg`/`.json.gpg` layout, still valid for files produced before R5 (they remain on the remote until the 30-day retention removes them).

---

## Recovery Scenarios

### Scenario 1: Full recovery from Google Drive (no AWS, no local machine)

```bash
# On a new machine:
brew install postgresql@17 pgvector rclone gpg

# 1. Configure rclone for Google Drive
rclone config  # n → gdrive → drive → authorize in browser

# 2. Retrieve encryption key
gcloud auth login
mkdir -p ~/second-brain
gcloud secrets versions access latest --secret=memory-bank-key > ~/second-brain/.backup-key
chmod 600 ~/second-brain/.backup-key

# 3. Download everything from Google Drive
#    NOTE (2026-10-05): backup.sh no longer syncs config/ or kb-sources/ to Drive
#    in plaintext. Whatever is under those two remote prefixes is a historical copy
#    frozen at the last pre-change run; the authoritative config is the git repo
#    and the local backups/kb-sources/ mirror. Only *.gpg is refreshed nightly.
rclone copy gdrive:memory-bank-backups/config/ ~/second-brain/ 
rclone copy gdrive:memory-bank-backups/kb-sources/ ~/second-brain/kb-sources/
rclone copy gdrive:memory-bank-backups/ /tmp/restore/ --include "*.dump.gpg" --max-age 1d

# 4. Restore Kiro skills
cp -r ~/second-brain/kiro-skills/ ~/.kiro/skills/

# 5. Restore Layer 2 (PostgreSQL memory bank — native PG 17 + pgvector)
brew services start postgresql@17          # serves localhost:5432
createuser -s memory_bank 2>/dev/null || true
createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"
for f in ~/second-brain/migrations/0*.sql; do
  psql -h localhost -U memory_bank -d memory_bank -f "$f"
done

LATEST=$(ls -t /tmp/restore/*.dump.gpg | head -1)
gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key "$LATEST" > /tmp/restore/memory_bank.dump
pg_restore -h localhost -U memory_bank -d memory_bank /tmp/restore/memory_bank.dump

# 6. Restore Layer 3 (re-index KB sources into local pgvector as temporary RAG)
#    OR wait for AWS access and re-upload to S3 → Bedrock KB
#    The source docs are at ~/second-brain/kb-sources/

# 7. Re-import Quick Desktop knowledge graph (if not in pg_dump)
cd ~/second-brain && .venv/bin/python scripts/migrate/migrate_quick_desktop.py --full

# 8. Verify
psql -h localhost -U memory_bank -d memory_bank -c "SELECT type, count(*) FROM memories GROUP BY type;"
psql -h localhost -U memory_bank -d memory_bank -c "SELECT count(*) FROM entities;"
ls ~/second-brain/kb-sources/
ls ~/.kiro/skills/
echo "✅ Full recovery from Google Drive complete"
```

**Recovery time: ~15 minutes. No AWS access required. All three layers restored.**

---

### Scenario 2: Full recovery from AWS (no Google, no local machine)

> **⚠️ UNAVAILABLE since 2026-06-01 — S3 was de-scoped, so no S3 backups exist to recover from.**
> This scenario is retained for historical reference and applies only if S3 is re-enabled.
> Use Scenario 1 (Google Drive) as the offsite recovery path.

```bash
# On a new machine:
brew install postgresql gpg docker awscli

# 1. Configure AWS CLI
aws configure  # access key, secret, region us-east-1

# 2. Retrieve encryption key
mkdir -p ~/second-brain
aws secretsmanager get-secret-value \
  --secret-id memory-bank-key \
  --query SecretString --output text \
  --region us-east-1 > ~/second-brain/.backup-key
chmod 600 ~/second-brain/.backup-key

# 3. Download everything from S3
aws s3 sync s3://memory-bank-backups/config/ ~/second-brain/
aws s3 sync s3://memory-bank-backups/kb-sources/ ~/second-brain/kb-sources/
aws s3 cp s3://memory-bank-backups/ /tmp/restore/ --recursive --exclude "*" --include "*.dump.gpg"

# 4. Restore Kiro skills
cp -r ~/second-brain/kiro-skills/ ~/.kiro/skills/

# 5. Restore Layer 2 (PostgreSQL memory bank)
docker compose -f ~/second-brain/docker-compose.yml up -d
sleep 5
for f in ~/second-brain/migrations/0*.sql; do
  psql -h localhost -U memory_bank -d memory_bank -f "$f"
done

LATEST=$(ls -t /tmp/restore/*.dump.gpg | head -1)
gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key "$LATEST" > /tmp/restore/memory_bank.dump
pg_restore -h localhost -U memory_bank -d memory_bank /tmp/restore/memory_bank.dump

# 6. Restore Layer 3 (re-upload to S3 and re-sync Bedrock KB)
aws s3 sync ~/second-brain/kb-sources/ s3://your-kb-bucket/
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <your-kb-id> \
  --data-source-id <your-ds-id> \
  --region us-east-1

# 7. Verify
psql -h localhost -U memory_bank -d memory_bank -c "SELECT type, count(*) FROM memories GROUP BY type;"
echo "✅ Full recovery from AWS complete"
```

**Recovery time: ~15 minutes. No Google access required. All three layers restored.**

---

### Scenario 3: Database corrupted but machine is fine

```bash
LATEST=$(ls -t ~/second-brain/backups/*.dump.gpg 2>/dev/null | head -1)

if [ -z "$LATEST" ]; then
  rclone copy gdrive:memory-bank-backups/ /tmp/restore/ --include "*.dump.gpg" --max-age 1d
  LATEST=$(ls -t /tmp/restore/*.dump.gpg | head -1)
fi

gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key "$LATEST" > /tmp/memory_bank.dump
pg_restore -h localhost -U memory_bank -d memory_bank --clean --if-exists /tmp/memory_bank.dump
```

---

### Scenario 4: Can't restore .dump — fall back to JSON

```bash
# Download JSON backup (from whichever cloud is available)
rclone copy gdrive:memory-bank-backups/ /tmp/restore/ --include "*.json.gpg" --max-age 1d
# OR: aws s3 cp s3://memory-bank-backups/ /tmp/restore/ --recursive --exclude "*" --include "*.json.gpg"

LATEST=$(ls -t /tmp/restore/*.json.gpg | head -1)
gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key "$LATEST" > /tmp/memories.json

# Start fresh native database, apply schema, import JSON
brew services start postgresql@17
createuser -s memory_bank 2>/dev/null || true; createdb -O memory_bank memory_bank 2>/dev/null || true
psql -h localhost -U memory_bank -d memory_bank -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -h localhost -U memory_bank -d memory_bank -f ~/second-brain/migrations/001_initial_schema.sql

# Import (embeddings will need to be regenerated)
# Use your import script or the MCP server's bulk import tool
```

**Note:** JSON restore requires re-generating vector embeddings (~10 extra minutes).

**Note:** If entity/edge JSON backups are missing (pre-April 2026 backups), re-import from Quick Desktop:
```bash
cd ~/second-brain
.venv/bin/python scripts/migrate/migrate_quick_desktop.py --full
```

---

### Scenario 5: Lost encryption key everywhere

**Unrecoverable by design.** All encrypted backups are permanently unreadable.

**If the database is still running locally:**
```bash
# Emergency: take an unencrypted backup immediately
pg_dump -Fc -h localhost -U memory_bank memory_bank > ~/emergency_backup.dump

# Generate new key and re-store in both clouds
openssl rand -base64 32 > ~/second-brain/.backup-key
chmod 600 ~/second-brain/.backup-key
gcloud secrets versions add memory-bank-key --data-file=~/second-brain/.backup-key
aws secretsmanager update-secret --secret-id memory-bank-key \
  --secret-string "$(cat ~/second-brain/.backup-key)" --region us-east-1
```

---

## Backup Verification (Weekly)

Runs every Sunday at 3 AM via `scripts/jobs/verify_backup.sh` — see "Verify" in the R5 contract above (freshness ≤ 36 h, both naming schemes, full restore for bundles, `pg_restore -l` for legacy dumps).

See the script source for current implementation. Do not duplicate here.

### Launchd schedule (weekly, Sundays 3 AM)

File: `~/Library/LaunchAgents/com.second-brain.verify.plist` (symlinked from `scheduling/`)

See `scheduling/com.second-brain.verify.plist` for current configuration.

### Launchd schedule (daily backup, 2 AM)

File: `~/Library/LaunchAgents/com.second-brain.backup.plist` (symlinked from `scheduling/`)

See `scheduling/com.second-brain.backup.plist` for current configuration. All plists are documented in [OPERATIONS.md](OPERATIONS.md).

---

## Quick Reference Card

| Situation | Command |
|---|---|
| List backups (Google) | `rclone ls gdrive:memory-bank-backups/` |
| Download latest (Google) | `rclone copy gdrive:memory-bank-backups/<file> ./` |
| Get key (Google) | `gcloud secrets versions access latest --secret=memory-bank-key` |
| Decrypt a file | `gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key <file.gpg> > <output>` |
| Restore database | `pg_restore -h localhost -U memory_bank -d memory_bank --clean --if-exists <file.dump>` |
| Restart PostgreSQL | `brew services restart postgresql@17` |
| Re-sync Bedrock KB | `aws bedrock-agent start-ingestion-job --knowledge-base-id <id> --data-source-id <id> --region us-east-1` |
| Verify backups | `bash ~/second-brain/scripts/jobs/verify_backup.sh` |
| Manual backup now | `bash ~/second-brain/scripts/jobs/backup.sh` |
| Check backup logs | `cat ~/second-brain/logs/backup-$(date +%Y%m%d).log` |
| Check verify logs | `cat ~/second-brain/logs/verify.log` |
