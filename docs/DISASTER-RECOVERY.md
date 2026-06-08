# Agent Memory Architecture — Disaster Recovery Runbook

> Last updated: 2026-06-08
> Backup schedule: Daily at 2:00 AM (launchd)
> Verification schedule: Weekly, Sundays at 3:00 AM (launchd)
> Encryption: AES-256 via GPG
> RPO: 24 hours | RTO: ~15-30 minutes

> ## ⚠️ CURRENT POSTURE (changed 2026-06-01)
> **AWS S3 was de-scoped** (brittle overnight SSO). Backups now go to **Google Drive + local
> only** (code/config also in git on `origin`/`mini`). This means:
> - The old "**Either Google OR AWS alone is sufficient**" dual-cloud guarantee **no longer holds**
>   for data — offsite redundancy is now **single-cloud (Google Drive)**.
> - **Scenario 2 (recovery from AWS S3) is UNAVAILABLE** — no S3 backups since 2026-06-01.
> - The AWS sections below are retained for **historical reference / if S3 is re-enabled**.
>
> **OPEN DECISION:** accept single-cloud + local, or re-establish a second offsite target
> (re-enable S3, or add e.g. Backblaze B2). Until then, treat Google Drive + local as the
> sole recovery path and verify the Google leg diligently.

### Known Failure Modes

| Issue | Impact | Mitigation |
|---|---|---|
| Google OAuth token expiry | Google Drive uploads fail → **no offsite backup** (S3 de-scoped, so Google is the only cloud) | Uses refresh token — hasn't expired in practice. Re-auth: `rclone config reconnect gdrive:`. Local copy still retained 7 days. |
| Docker not running at 2 AM | Backup aborts | Script auto-starts container; pre-flight check |
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

## Dual-Site Backup Strategy

```
                         Local Machine
                              │
                   daily encrypted backup
                         ┌────┴────┐
                         ▼         ▼
                  Google Drive    AWS S3
                  ┌──────────┐  ┌──────────┐
                  │ pg_dump   │  │ pg_dump   │
                  │ JSON      │  │ JSON      │
                  │ KB sources│  │ KB sources│
                  │ Config    │  │ Config    │
                  └──────────┘  └──────────┘
                  Key in         Key in
                  Google         AWS Secrets
                  Secret Mgr     Manager
```

Either column alone can rebuild the entire system from scratch.

---

## What's Backed Up (and Where)

| Asset | Google Drive | AWS S3 | Local |
|---|---|---|---|
| Layer 2: PostgreSQL dump (.dump.gpg) | ✅ | ✅ | Temporary |
| Layer 2: JSON — memories (.json.gpg) | ✅ | ✅ | Temporary |
| Layer 2: JSON — entities (.json.gpg) | ✅ | ✅ | Temporary |
| Layer 2: JSON — entity_edges (.json.gpg) | ✅ | ✅ | Temporary |
| Layer 2: JSON — memory_entities (.json.gpg) | ✅ | ✅ | Temporary |
| Layer 3: Knowledge base source docs | ✅ | ✅ | Local copy |
| Layer 1: Kiro skills / bootstrap | ✅ | ✅ | `~/.kiro/skills/` |
| Config: docker-compose.yml, schema.sql | ✅ | ✅ | `~/second-brain/` |
| Config: backup & verify scripts | ✅ | ✅ | `~/second-brain/` |
| Config: this runbook | ✅ | ✅ | `~/second-brain/` |
| Encryption key | ✅ Google Secret Mgr | ✅ AWS Secrets Mgr | `~/second-brain/.backup-key` |

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
- Docker container state — stateless, just restart
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

The backup script lives at `scripts/jobs/backup.sh`. It handles:
- PostgreSQL dump (`pg_dump -Fc`)
- JSON fallback exports (memories, entities, entity_edges, memory_entities)
- GPG encryption (AES-256)
- Upload to Google Drive and AWS S3
- AWS SSO token health check with auto-refresh
- Local retention (7 days), cloud retention (30 days)

Do not duplicate the script here — see the source file for current implementation.

---

## Recovery Scenarios

### Scenario 1: Full recovery from Google Drive (no AWS, no local machine)

```bash
# On a new machine:
brew install postgresql rclone gpg docker

# 1. Configure rclone for Google Drive
rclone config  # n → gdrive → drive → authorize in browser

# 2. Retrieve encryption key
gcloud auth login
mkdir -p ~/second-brain
gcloud secrets versions access latest --secret=memory-bank-key > ~/second-brain/.backup-key
chmod 600 ~/second-brain/.backup-key

# 3. Download everything from Google Drive
rclone copy gdrive:memory-bank-backups/config/ ~/second-brain/ 
rclone copy gdrive:memory-bank-backups/kb-sources/ ~/second-brain/kb-sources/
rclone copy gdrive:memory-bank-backups/ /tmp/restore/ --include "*.dump.gpg" --max-age 1d

# 4. Restore Kiro skills
cp -r ~/second-brain/kiro-skills/ ~/.kiro/skills/

# 5. Restore Layer 2 (PostgreSQL memory bank)
docker compose -f ~/second-brain/docker-compose.yml up -d
sleep 5  # wait for PostgreSQL to start
psql -h localhost -U memory_bank -d memory_bank -f ~/second-brain/migrations/001_initial_schema.sql
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

# Start fresh database, apply schema, import JSON
docker compose -f ~/second-brain/docker-compose.yml up -d
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

Runs every Sunday at 3 AM via `scripts/jobs/verify_backup.sh`. Checks BOTH destinations independently — downloads latest dump, decrypts, validates with `pg_restore -l`.

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
| List backups (AWS) | `rclone ls s3:memory-bank-backups/` |
| Download latest (Google) | `rclone copy gdrive:memory-bank-backups/<file> ./` |
| Download latest (AWS) | `aws s3 cp s3://memory-bank-backups/<file> ./` |
| Get key (Google) | `gcloud secrets versions access latest --secret=memory-bank-key` |
| Get key (AWS) | `aws secretsmanager get-secret-value --secret-id memory-bank-key --query SecretString --output text --region us-east-1` |
| Decrypt a file | `gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key <file.gpg> > <output>` |
| Restore database | `pg_restore -h localhost -U memory_bank -d memory_bank --clean --if-exists <file.dump>` |
| Restart PostgreSQL | `docker compose up -d` (from `~/second-brain/`) |
| Re-sync Bedrock KB | `aws bedrock-agent start-ingestion-job --knowledge-base-id <id> --data-source-id <id> --region us-east-1` |
| Verify backups | `bash ~/second-brain/scripts/jobs/verify_backup.sh` |
| Manual backup now | `bash ~/second-brain/scripts/jobs/backup.sh` |
| Check backup logs | `cat ~/second-brain/logs/backup-$(date +%Y%m%d).log` |
| Check verify logs | `cat ~/second-brain/logs/verify.log` |
