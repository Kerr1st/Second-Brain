#!/bin/bash
# Per-source liveness check — alerts when an active capture channel goes silent.
#
# A job can exit 0 yet capture nothing (e.g. the YouTube/bookmark breakages of
# 2026-05), so liveness is judged by the DB itself: the most recent capture
# (MAX(created_at)) per channel. A channel older than its max-age is "silent".
#
# Run daily 09:00 via com.second-brain.liveness (through job_wrapper.sh).
#
# 2026-09-23 — thresholds corrected so this monitor can actually be trusted. It
# exited 1 every night on the two Quick channels, and a monitor that always fails
# is a monitor nobody reads (that is how Slack and Outlook stayed broken for days).
# The Quick breaches were real *silence* but the wrong *diagnosis*: our hourly
# qd_sync was fully caught up; the upstream Quick export had stopped. Measuring our
# own table can never tell those two apart, so the upstream sqlite file is stat'd
# directly (below) and the channel thresholds are set from observed cadence.
#
# 2026-10-06 (R5) — the channel table, thresholds and severities moved to
# config/liveness.toml (unchanged values), evaluated by `python -m
# src.operations.liveness`, which ADDS: a generic "channel that ever produced
# output has produced zero for longer than its own cadence tolerates" predicate
# (the rule that would have caught kiro_ide_chat's death and the 2026-10-03/04
# Slack rate-limit gap), dream_cycle_runs completed_at freshness + 0-candidate
# detection, known-disabled producers (distilled_chat, weekly_digest) as ADVISORY
# by explicit config, and a sweep of logs/jobs/*.json (last exit non-zero, no
# status update for longer than the job's expected interval). Every finding is
# written to logs/jobs/liveness-report.json — headless-safe; osascript is only a
# supplement.
#
# Output vocabulary — the exit code follows the ❌ column only:
#   ✅ fresh        ⚠ advisory / retired / not-ours-to-fix (never fatal)
#   ❌ a required channel/job is genuinely broken → notify + exit 1
set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")/../.."

# Connection target. Defaults are the production database exactly as before;
# tests point these at a disposable cluster so the monitor never reads memory_bank.
export DB_HOST="${DB_HOST:-127.0.0.1}"
export DB_PORT="${DB_PORT:-5432}"
export DB_USER="${DB_USER:-memory_bank}"
export DB_NAME="${DB_NAME:-memory_bank}"
PG_ISREADY_BIN="${PG_ISREADY_BIN:-pg_isready}"
PYTHON_BIN="${SECOND_BRAIN_PYTHON:-.venv/bin/python}"
STATUS_DIR="${SECOND_BRAIN_STATUS_DIR:-logs/jobs}"
REPORT="$STATUS_DIR/liveness-report.json"

log() { echo "[$(date '+%H:%M:%S')] $1"; }
notify() { osascript -e "display notification \"$1\" with title \"Second Brain — capture silent\" sound name \"Basso\"" 2>/dev/null || true; }

# Upstream Quick store. Our ingest can only be as fresh as this file, so its age is
# the signal that distinguishes "qd_sync broke" from "Quick stopped exporting".
# stat(2) ONLY — never read the contents; this is someone's knowledge corpus.
#   missing/unresolvable → ❌ ours: the profile moved and src/qd_profile.py no
#                             longer resolves it (that is a bug we can fix).
#   older than 7 d       → ⚠ not ours: Quick itself has written nothing for a
#                             week, so every quick_desktop_* channel is idle by
#                             cause and there is nothing to repair in this repo.
QD_MAX_AGE_DAYS="${QD_MAX_AGE_DAYS:-7}"

log "=== Capture liveness: $(date '+%Y-%m-%d %H:%M') ==="
SILENT_MSG=""
ADVISORY_MSG=""

if ! "$PG_ISREADY_BIN" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" &>/dev/null; then
  # The database is the thing being measured; without it the sweep below still
  # runs (--skip-db) so a dead job is reported alongside the dead database.
  log "❌ database not reachable at $DB_HOST:$DB_PORT (pg_isready) — channel ages cannot be measured"
  SILENT_MSG="$SILENT_MSG database(unreachable)"
  SKIP_DB="--skip-db"
else
  SKIP_DB=""
fi

# --- channels, zero-output, dream cycle, job status sweep (config/liveness.toml) ---
POLICY_OUT="$("$PYTHON_BIN" -m src.operations.liveness --status-dir "$STATUS_DIR" --report "$REPORT" $SKIP_DB 2>&1)"
POLICY_RC=$?
while IFS= read -r line; do [ -n "$line" ] && log "$line"; done <<< "$POLICY_OUT"
# Lines are "<mark> [<check>:<subject>] <message>"; the summary lists check:subject.
if [ "$POLICY_RC" -ne 0 ]; then
  failed="$(printf '%s\n' "$POLICY_OUT" | sed -n 's/^❌ \[\([^]]*\)\].*/\1/p' | tr '\n' ' ')"
  failed="${failed% }"
  [ -z "$failed" ] && failed="policy-exit-$POLICY_RC"
  SILENT_MSG="$SILENT_MSG $failed"
fi
advisory="$(printf '%s\n' "$POLICY_OUT" | sed -n 's/^⚠ \[[a-z_]*:\([^]]*\)\].*/\1/p' | tr '\n' ' ')"
[ -n "$advisory" ] && ADVISORY_MSG="$ADVISORY_MSG ${advisory% }"

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
  log "    durable report: $REPORT"
  notify "Silent:$SILENT_MSG — check logs/liveness-$(date +%Y%m%d).log"
  exit 1
fi
log "=== All monitored channels live ==="
