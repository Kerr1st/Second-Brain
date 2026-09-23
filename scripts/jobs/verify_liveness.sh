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
#
# 2026-09-23 — thresholds corrected so this monitor can actually be trusted. It
# exited 1 every night on the two Quick channels, and a monitor that always fails
# is a monitor nobody reads (that is how Slack and Outlook stayed broken for days).
# The Quick breaches were real *silence* but the wrong *diagnosis*: our hourly
# qd_sync was fully caught up; the upstream Quick export had stopped. Measuring our
# own table can never tell those two apart, so the upstream sqlite file is now
# stat'd directly (see the QD upstream check below) and the channel thresholds are
# set from observed cadence instead of aspiration.
#
# Output vocabulary — the exit code follows the ❌ column only:
#   ✅ fresh        ⚠ advisory / retired / not-ours-to-fix (never fatal)
#   ❌ a required channel is genuinely silent → notify + exit 1
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")/../.."

CONTAINER="${DB_CONTAINER:-second-brain-db}"
DB_USER="${DB_USER:-memory_bank}"
DB_NAME="${DB_NAME:-memory_bank}"
PSQL_BIN="${PSQL_BIN:-psql}"
PG_ISREADY_BIN="${PG_ISREADY_BIN:-pg_isready}"
PYTHON_BIN="${SECOND_BRAIN_PYTHON:-.venv/bin/python}"

log() { echo "[$(date '+%H:%M:%S')] $1"; }
notify() { osascript -e "display notification \"$1\" with title \"Second Brain — capture silent\" sound name \"Basso\"" 2>/dev/null || true; }

# Monitored channels: "label|SQL predicate over memories|max_age_days|severity".
# severity: required (❌, fails the job) | retired (⚠, kept visible on purpose).
# Edit as channels are added/retired. Dead/one-time sources (kiro_ide_chat, the
# static quick_desktop_* imports) are excluded to avoid false alarms. Capture API
# (manual/slack) is on-demand with no cadence, so not monitored.
CHANNELS=(
  # RETIRED, not deleted — QD doc ingest had its own channel so a doc-ingest
  # regression could not hide behind fresh synthesis output. It last produced on
  # 2026-06-04 (32,624 rows, then nothing for 111 d) because the upstream Quick
  # doc export stopped, not because qd_sync broke: qd_sync reports processed=0 with
  # no errors. A 3 d threshold on a channel whose source has been dormant for ~4
  # months is a guaranteed nightly false alarm, and it was the loudest contributor
  # to the alert fatigue this monitor caused. Kept listed and reported as ⚠ so the
  # dormancy stays visible; re-arm it (severity → required) if Quick doc export
  # ever resumes. The upstream-file check below is what would reveal that.
  "quick_desktop_doc|source_type = 'quick_desktop_doc'|3|retired"
  # Dream-cycle synthesis (qd_sync.sh hourly: migrate_quick_desktop + eventlog +
  # qd_chats), freshest-wins — answers "is QD synthesis producing anything?".
  # Sparse types (anti_pattern, people, profile...) have natural source-idle gaps
  # too long to alarm individually, but they share this pipeline so it covers
  # them. Excludes quick_desktop_doc (own channel, retired above) and
  # quick_desktop_session_event (retired — redundant with quick_desktop_chat and
  # read a dead legacy path; see git log).
  # max_age 21 (was 5, which could never pass): this channel is bounded by the
  # upstream Quick export, and the observed inter-capture gaps are 11 d
  # (2026-09-05 → 09-16) and 14 d (2026-08-19 → 09-02). 21 d = the largest real
  # gap plus a week, so it still catches a genuine multi-week ingest stall while
  # not firing on normal upstream idleness.
  "quick_desktop_synth|source_type LIKE 'quick_desktop_%' AND source_type NOT IN ('quick_desktop_doc','quick_desktop_session_event')|21|required"
  "web_articles|source_type = 'article'|9|required"
  "cli_chat|source_type = 'kiro_cli_chat'|4|required"
  "youtube|source_type = 'youtube'|9|required"
  "outlook_email|source_type = 'outlook_email'|3|required"
  "outlook_calendar|source_type = 'outlook_calendar'|5|required"
  "slack|source_type = 'slack'|4|required"
  "browser_history|source_type = 'browser_history'|4|required"
  # Codex task capture went live 2026-09-22 (com.second-brain.codex-capture, every
  # 6 h). Codex use is bursty — sessions cluster into working days, so a quiet week
  # is normal idleness, not breakage. 7 d = 28 consecutive job runs that produced
  # nothing, which is no longer explainable by "the user did not open Codex".
  "codex_task|source_type = 'codex_task'|7|required"
)

# Upstream Quick store. Our ingest can only be as fresh as this file, so its age is
# the signal that distinguishes "qd_sync broke" from "Quick stopped exporting".
# stat(2) ONLY — never read the contents; this is someone's knowledge corpus.
#   missing/unresolvable → ❌ ours: the profile moved and src/qd_profile.py no
#                             longer resolves it (that is a bug we can fix).
#   older than 7 d       → ⚠ not ours: Quick itself has written nothing for a
#                             week, so every quick_desktop_* channel is idle by
#                             cause and there is nothing to repair in this repo.
QD_MAX_AGE_DAYS="${QD_MAX_AGE_DAYS:-7}"

if ! "$PG_ISREADY_BIN" -h 127.0.0.1 -p 5432 -U "$DB_USER" &>/dev/null; then
  log "ABORT: database not reachable"
  notify "Liveness check could not reach the database"
  exit 1
fi

log "=== Capture liveness: $(date '+%Y-%m-%d %H:%M') ==="
SILENT_MSG=""
ADVISORY_MSG=""
for entry in "${CHANNELS[@]}"; do
  IFS='|' read -r label pred max_age sev <<< "$entry"
  age=$("$PSQL_BIN" -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" -tAc \
    "SELECT COALESCE(now()::date - MAX(created_at)::date, 99999) FROM memories WHERE $pred;" 2>/dev/null)
  [[ "$age" =~ ^[0-9]+$ ]] || age=99999
  if [ "$sev" = retired ]; then
    log "⚠ $label retired — dormant since 2026-06-04, last capture ${age}d ago (not monitored)"
    ADVISORY_MSG="$ADVISORY_MSG ${label}(retired)"
  elif [ "$age" -gt "$max_age" ]; then
    log "❌ $label SILENT — last capture ${age}d ago (max ${max_age}d)"
    SILENT_MSG="$SILENT_MSG ${label}(${age}d)"
  else
    log "✅ $label ok (${age}d / max ${max_age}d)"
  fi
done

# --- upstream Quick export freshness (stat only, no reads) ---
QD_DB="${QD_DB_PATH:-}"
if [ -z "$QD_DB" ]; then
  QD_DB=$("$PYTHON_BIN" -c \
    'from src.qd_profile import qd_path; print(qd_path("knowledge_storage", "knowledge_v1.db"))' \
    2>/dev/null)
fi
if [ -z "$QD_DB" ] || [ ! -f "$QD_DB" ]; then
  log "❌ quick_upstream UNRESOLVED — no knowledge_v1.db at '${QD_DB:-<unresolved>}'"
  log "   fix: the Quick profile layout changed — update src/qd_profile.py (profiles.json last_active)"
  SILENT_MSG="$SILENT_MSG quick_upstream(missing)"
else
  qd_mtime=$(stat -f %m "$QD_DB" 2>/dev/null || stat -c %Y "$QD_DB" 2>/dev/null)
  if [[ "$qd_mtime" =~ ^[0-9]+$ ]]; then
    qd_age=$(( ( $(date +%s) - qd_mtime ) / 86400 ))
    if [ "$qd_age" -gt "$QD_MAX_AGE_DAYS" ]; then
      log "⚠ quick_upstream STALE — Quick has written nothing for ${qd_age}d (max ${QD_MAX_AGE_DAYS}d)"
      log "   Quick stopped exporting; the quick_desktop_* channels are idle by cause, not broken"
      ADVISORY_MSG="$ADVISORY_MSG quick_upstream(${qd_age}d)"
    else
      log "✅ quick_upstream ok (${qd_age}d / max ${QD_MAX_AGE_DAYS}d)"
    fi
  else
    log "⚠ quick_upstream age unreadable — could not stat $QD_DB"
    ADVISORY_MSG="$ADVISORY_MSG quick_upstream(unreadable)"
  fi
fi

[ -n "$ADVISORY_MSG" ] && log "=== advisory, NOT failing the job:$ADVISORY_MSG ==="

if [ -n "$SILENT_MSG" ]; then
  log "=== SILENT CHANNELS:$SILENT_MSG ==="
  notify "Silent:$SILENT_MSG — check logs/liveness-$(date +%Y%m%d).log"
  exit 1
fi
log "=== All monitored channels live ==="
