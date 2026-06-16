#!/bin/bash
# Per-source liveness check — alerts when an active capture channel goes silent.
# Operating-Envelope liveness check (see docs/REFACTOR-PLAN.md).
#
# A job can exit 0 yet capture nothing (e.g. the YouTube/bookmark breakages of
# 2026-05), so liveness is judged by the DB itself: the most recent capture
# (MAX(created_at)) per channel. A channel older than its max-age is "silent".
#
# Run daily 09:00 via com.second-brain.liveness. Thresholds are in DAYS and are
# deliberately generous enough to absorb weekends / a powered-off machine.
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/../.."

CONTAINER="${DB_CONTAINER:-second-brain-db}"
DB_USER="${DB_USER:-memory_bank}"
DB_NAME="${DB_NAME:-memory_bank}"

log() { echo "[$(date '+%H:%M:%S')] $1"; }
notify() { osascript -e "display notification \"$1\" with title \"Second Brain — capture silent\" sound name \"Basso\"" 2>/dev/null || true; }

# Monitored channels: "label|SQL predicate over memories|max_age_days".
# Edit as channels are added/retired. Dead/one-time sources (kiro_ide_chat and
# the static quick_desktop_* imports) are excluded to avoid false alarms.
# Capture API (manual/slack) is on-demand with no cadence, so not monitored.
CHANNELS=(
  # QD doc ingest (qd_sync.sh, hourly) — its own channel so a doc-ingest
  # regression can't hide behind fresh synthesis output (it did pre-Tier-1).
  "quick_desktop_doc|source_type = 'quick_desktop_doc'|3"
  # Dream-cycle synthesis (qd_sync.sh hourly: migrate_quick_desktop + eventlog +
  # qd_chats), freshest-wins — answers "is QD synthesis producing anything?".
  # Sparse types (anti_pattern, people, profile...) have natural source-idle gaps
  # too long to alarm individually, but they share this pipeline so it covers
  # them (verified: people/profile stale only because the QD source has no new
  # rows in those categories — sync is fully caught up). Excludes quick_desktop_doc
  # (own channel) and quick_desktop_session_event (retired — was redundant with
  # quick_desktop_chat and read a dead legacy path; see git log).
  "quick_desktop_synth|source_type LIKE 'quick_desktop_%' AND source_type NOT IN ('quick_desktop_doc','quick_desktop_session_event')|5"
  "web_articles|source_type = 'article'|9"
  "cli_chat|source_type = 'kiro_cli_chat'|4"
  "youtube|source_type = 'youtube'|9"
)

if ! pg_isready -h 127.0.0.1 -p 5432 -U "$DB_USER" &>/dev/null; then
  log "ABORT: database not reachable"
  notify "Liveness check could not reach the database"
  exit 1
fi

log "=== Capture liveness: $(date '+%Y-%m-%d %H:%M') ==="
SILENT_MSG=""
for entry in "${CHANNELS[@]}"; do
  IFS='|' read -r label pred max_age <<< "$entry"
  age=$(psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -tAc \
    "SELECT COALESCE(now()::date - MAX(created_at)::date, 99999) FROM memories WHERE $pred;" 2>/dev/null)
  [[ "$age" =~ ^[0-9]+$ ]] || age=99999
  if [ "$age" -gt "$max_age" ]; then
    log "❌ $label SILENT — last capture ${age}d ago (max ${max_age}d)"
    SILENT_MSG="$SILENT_MSG ${label}(${age}d)"
  else
    log "✅ $label ok (${age}d / max ${max_age}d)"
  fi
done

if [ -n "$SILENT_MSG" ]; then
  log "=== SILENT CHANNELS:$SILENT_MSG ==="
  notify "Silent:$SILENT_MSG — check logs/liveness-$(date +%Y%m%d).log"
  exit 1
fi
log "=== All monitored channels live ==="
