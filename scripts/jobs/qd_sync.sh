#!/bin/bash
# Quick Desktop → Second Brain incremental sync
# Runs hourly via LaunchAgent, imports new QD memories and KG entities since last sync

set -euo pipefail
# Unattended jobs use a least-privilege IAM user (Bedrock InvokeModel only), not the
# interactive SSO session: the 8h Identity Center session is what silently killed
# every job on 2026-09-08. Override by exporting AWS_PROFILE before invoking.
export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"
# Re-exec under caffeinate so the hourly sync is not suspended by battery idle-sleep.
if [ -z "${SB_CAFFEINATED:-}" ]; then export SB_CAFFEINATED=1; exec /usr/bin/caffeinate -i "$0" "$@"; fi
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."

DATE=$(date +%Y%m%d)
LOG="logs/qd-sync-$DATE.log"

qd_count() { psql -h 127.0.0.1 -p 5432 -U memory_bank -d memory_bank -tAc \
  "SELECT COUNT(*) FROM memories WHERE source_type LIKE 'quick_desktop_%';" 2>/dev/null || echo 0; }

echo "$(date '+%Y-%m-%d %H:%M:%S') Starting QD sync" >> "$LOG"
BEFORE=$(qd_count)

.venv/bin/python scripts/migrate/migrate_quick_desktop.py >> "$LOG" 2>&1
.venv/bin/python scripts/migrate/enrich_qd_tags.py >> "$LOG" 2>&1
.venv/bin/python scripts/migrate/ingest_eventlog.py >> "$LOG" 2>&1
.venv/bin/python scripts/migrate/import_slack_graph.py >> "$LOG" 2>&1
.venv/bin/python scripts/ingest_qd_chats.py >> "$LOG" 2>&1
.venv/bin/python scripts/migrate/ingest_doc_chunks.py >> "$LOG" 2>&1

AFTER=$(qd_count)
echo "$(date '+%Y-%m-%d %H:%M:%S') QD sync complete — captures_this_cycle=$(( ${AFTER:-0} - ${BEFORE:-0} ))" >> "$LOG"
