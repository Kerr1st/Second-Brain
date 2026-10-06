#!/bin/bash
# Shared preflight helpers for scripts/jobs/*.sh (source this file; do not execute).
#
# require_postgres — exits 69 (EX_UNAVAILABLE) with one unambiguous log line when
# PostgreSQL does not answer pg_isready at DB_HOST:DB_PORT.
#
# Deliberately NO `brew services start postgresql@17` here (it was in backup.sh,
# dream_cycle_scheduled.sh and weekly_digest.sh until R5). Why it was removed:
#   * `brew services start` is not a read: it (re)writes a LaunchAgent under
#     ~/Library/LaunchAgents and bootstraps it — a system-state side effect from a
#     backup job, racing the login-time start of the same service.
#   * It masked the real incident. Postgres down at 02:00 means the service failed
#     or the Mac rebooted without it; silently restarting it from whichever job ran
#     first hid that for months. Now the job fails with exit 69, the wrapper records
#     it in logs/jobs/<job>.json, and the 09:00 liveness sweep reports every job
#     that hit it ("last exit non-zero") plus "database unreachable" itself.
#   * Homebrew already starts postgresql@17 at login (`brew services` LaunchAgent);
#     the operator restarts it with `brew services restart postgresql@17`.
# Exit 69 is distinct from a job's own failures (1), a missing tool (127) and the
# wrapper's deadline (124), so a log reader classifies it at a glance.

require_postgres() {
  local host="${DB_HOST:-127.0.0.1}" port="${DB_PORT:-5432}" user="${DB_USER:-memory_bank}"
  if ! "${PG_ISREADY_BIN:-pg_isready}" -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] ABORT (exit 69): PostgreSQL is not reachable at $host:$port — not auto-started; run 'brew services restart postgresql@17' and re-run the job" >&2
    exit 69
  fi
}
