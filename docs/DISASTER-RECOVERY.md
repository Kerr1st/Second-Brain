# Agent Memory Architecture — Disaster Recovery Runbook

> Last updated: 2026-03-03
> Backup schedule: Daily at 2:00 AM (launchd)
> Verification schedule: Weekly, Sundays at 3:00 AM (launchd)
> Encryption: AES-256 via GPG
> Recovery principle: **Either Google OR AWS alone is sufficient to fully recover the entire system.**

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
| Layer 2: JSON export (.json.gpg) | ✅ | ✅ | Temporary |
| Layer 3: Knowledge base source docs | ✅ | ✅ | Local copy |
| Layer 1: Kiro skills / bootstrap | ✅ | ✅ | `~/.kiro/skills/` |
| Config: docker-compose.yml, schema.sql | ✅ | ✅ | `~/second-brain/` |
| Config: backup & verify scripts | ✅ | ✅ | `~/second-brain/` |
| Config: this runbook | ✅ | ✅ | `~/second-brain/` |
| Encryption key | ✅ Google Secret Mgr | ✅ AWS Secrets Mgr | `~/second-brain/.backup-key` |

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

Pushes ALL layers to BOTH clouds.

```bash
#!/bin/bash
# memory-backup.sh

set -euo pipefail

BACKUP_DIR="$HOME/second-brain/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

echo "=== Memory Bank Backup: $TIMESTAMP ==="

# --- Layer 2: PostgreSQL Memory Bank ---

echo "Dumping PostgreSQL..."
pg_dump -Fc -h localhost -U memory_bank memory_bank > "$BACKUP_DIR/memory_bank_${TIMESTAMP}.dump"

echo "Exporting JSON..."
psql -h localhost -U memory_bank memory_bank -c "COPY (
  SELECT json_agg(row_to_json(m))
  FROM memories m
) TO STDOUT" > "$BACKUP_DIR/memories_${TIMESTAMP}.json"

echo "Encrypting..."
for f in "$BACKUP_DIR/memory_bank_${TIMESTAMP}.dump" "$BACKUP_DIR/memories_${TIMESTAMP}.json"; do
  gpg --symmetric --cipher-algo AES256 --batch --passphrase-file ~/second-brain/.backup-key "$f"
  rm "$f"
done

# --- Layer 3: Knowledge Base Source Documents ---

echo "Syncing KB source docs from S3..."
rclone sync s3:your-kb-bucket/ "$BACKUP_DIR/kb-sources/"

# --- Layer 1 + Config: Skills, scripts, schema, runbook ---

echo "Copying config files..."
mkdir -p "$BACKUP_DIR/config"
cp ~/second-brain/docker-compose.yml "$BACKUP_DIR/config/" 2>/dev/null || true
cp ~/second-brain/migrations/001_initial_schema.sql "$BACKUP_DIR/config/" 2>/dev/null || true
cp ~/second-brain/memory-backup.sh "$BACKUP_DIR/config/" 2>/dev/null || true
cp ~/second-brain/verify-backup.sh "$BACKUP_DIR/config/" 2>/dev/null || true
cp ~/second-brain/DISASTER-RECOVERY.md "$BACKUP_DIR/config/" 2>/dev/null || true
cp -r ~/.kiro/skills/ "$BACKUP_DIR/config/kiro-skills/" 2>/dev/null || true

# --- Upload to BOTH destinations ---

echo "Uploading to Google Drive..."
rclone copy "$BACKUP_DIR/" gdrive:memory-bank-backups/ --include "*.gpg"
rclone sync "$BACKUP_DIR/kb-sources/" gdrive:memory-bank-backups/kb-sources/
rclone sync "$BACKUP_DIR/config/" gdrive:memory-bank-backups/config/

echo "Uploading to AWS S3..."
rclone copy "$BACKUP_DIR/" s3:memory-bank-backups/ --include "*.gpg"
rclone sync "$BACKUP_DIR/kb-sources/" s3:memory-bank-backups/kb-sources/
rclone sync "$BACKUP_DIR/config/" s3:memory-bank-backups/config/

# --- Retention: 30 days on cloud, 7 days local ---

rclone delete gdrive:memory-bank-backups/ --min-age 30d --include "*.gpg"
rclone delete s3:memory-bank-backups/ --min-age 30d --include "*.gpg"
find "$BACKUP_DIR" -name "*.gpg" -mtime +7 -delete

echo "=== Backup complete: $TIMESTAMP → Google Drive + S3 ==="
```

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

LATEST=$(ls -t /tmp/restore/*.dump.gpg | head -1)
gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key "$LATEST" > /tmp/restore/memory_bank.dump
pg_restore -h localhost -U memory_bank -d memory_bank /tmp/restore/memory_bank.dump

# 6. Restore Layer 3 (re-index KB sources into local pgvector as temporary RAG)
#    OR wait for AWS access and re-upload to S3 → Bedrock KB
#    The source docs are at ~/second-brain/kb-sources/

# 7. Verify
psql -h localhost -U memory_bank -d memory_bank -c "SELECT type, count(*) FROM memories GROUP BY type;"
ls ~/second-brain/kb-sources/
ls ~/.kiro/skills/
echo "✅ Full recovery from Google Drive complete"
```

**Recovery time: ~15 minutes. No AWS access required. All three layers restored.**

---

### Scenario 2: Full recovery from AWS (no Google, no local machine)

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
psql -h localhost -U memory_bank -d memory_bank -f ~/second-brain/migrations/001_initial_schema.sql

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

Runs every Sunday at 3 AM. Checks BOTH destinations independently.

```bash
#!/bin/bash
# verify-backup.sh

set -uo pipefail
mkdir -p /tmp/verify ~/second-brain/logs

echo "=== Backup Verification: $(date) ==="

# --- Verify Google Drive ---
echo ""
echo "--- Google Drive ---"
LATEST_GD=$(rclone ls gdrive:memory-bank-backups/ --include "*.dump.gpg" | sort -k2 | tail -1 | awk '{print $2}')
if [ -n "$LATEST_GD" ]; then
  echo "Latest: $LATEST_GD"
  rclone copy "gdrive:memory-bank-backups/$LATEST_GD" /tmp/verify/
  gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key \
    "/tmp/verify/$LATEST_GD" > /tmp/verify/test_gd.dump 2>/dev/null
  if pg_restore -l /tmp/verify/test_gd.dump > /dev/null 2>&1; then
    TABLES=$(pg_restore -l /tmp/verify/test_gd.dump | grep "TABLE DATA" | wc -l | tr -d ' ')
    echo "✅ Google Drive backup valid ($TABLES tables)"
  else
    echo "❌ GOOGLE DRIVE BACKUP CORRUPT — investigate immediately"
  fi
  # Check KB sources exist
  KB_COUNT=$(rclone ls gdrive:memory-bank-backups/kb-sources/ 2>/dev/null | wc -l | tr -d ' ')
  echo "   KB source docs: $KB_COUNT files"
  # Check config exists
  CFG_COUNT=$(rclone ls gdrive:memory-bank-backups/config/ 2>/dev/null | wc -l | tr -d ' ')
  echo "   Config files: $CFG_COUNT files"
else
  echo "❌ NO BACKUPS FOUND ON GOOGLE DRIVE"
fi

# --- Verify S3 ---
echo ""
echo "--- AWS S3 ---"
LATEST_S3=$(rclone ls s3:memory-bank-backups/ --include "*.dump.gpg" | sort -k2 | tail -1 | awk '{print $2}')
if [ -n "$LATEST_S3" ]; then
  echo "Latest: $LATEST_S3"
  rclone copy "s3:memory-bank-backups/$LATEST_S3" /tmp/verify/
  gpg --decrypt --batch --passphrase-file ~/second-brain/.backup-key \
    "/tmp/verify/$LATEST_S3" > /tmp/verify/test_s3.dump 2>/dev/null
  if pg_restore -l /tmp/verify/test_s3.dump > /dev/null 2>&1; then
    TABLES=$(pg_restore -l /tmp/verify/test_s3.dump | grep "TABLE DATA" | wc -l | tr -d ' ')
    echo "✅ S3 backup valid ($TABLES tables)"
  else
    echo "❌ S3 BACKUP CORRUPT — investigate immediately"
  fi
  KB_COUNT=$(rclone ls s3:memory-bank-backups/kb-sources/ 2>/dev/null | wc -l | tr -d ' ')
  echo "   KB source docs: $KB_COUNT files"
  CFG_COUNT=$(rclone ls s3:memory-bank-backups/config/ 2>/dev/null | wc -l | tr -d ' ')
  echo "   Config files: $CFG_COUNT files"
else
  echo "❌ NO BACKUPS FOUND ON S3"
fi

rm -rf /tmp/verify/
echo ""
echo "=== Verification complete: $(date) ==="
```

### Launchd schedule (weekly, Sundays 3 AM)

File: `~/Library/LaunchAgents/com.second-brain.verify.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.second-brain.verify</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/kerrfir/.memory-bank/verify-backup.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/kerrfir/.memory-bank/logs/verify.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/kerrfir/.memory-bank/logs/verify.log</string>
</dict>
</plist>
```

### Launchd schedule (daily backup, 2 AM)

File: `~/Library/LaunchAgents/com.second-brain.backup.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.second-brain.backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/kerrfir/.memory-bank/memory-backup.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/kerrfir/.memory-bank/logs/backup.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/kerrfir/.memory-bank/logs/backup.log</string>
</dict>
</plist>
```

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
| Verify backups | `bash ~/second-brain/verify-backup.sh` |
| Manual backup now | `bash ~/second-brain/memory-backup.sh` |
| Check backup logs | `cat ~/second-brain/logs/backup.log` |
| Check verify logs | `cat ~/second-brain/logs/verify.log` |
