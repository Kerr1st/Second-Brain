#!/bin/bash
# Quick Desktop → Second Brain incremental sync
# Runs hourly via LaunchAgent, imports new QD memories and KG entities since last sync

set -euo pipefail
cd "$(dirname "$0")/../.."

DATE=$(date +%Y%m%d)
LOG="logs/qd-sync-$DATE.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') Starting QD sync" >> "$LOG"

.venv/bin/python scripts/migrate/migrate_quick_desktop.py >> "$LOG" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') QD sync complete" >> "$LOG"
