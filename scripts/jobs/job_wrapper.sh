#!/bin/bash
# Wrapper for Second Brain scheduled jobs.
# Runs the given script, logs output, and sends a macOS notification on failure.
# Usage: job_wrapper.sh <script_path> <job_name>

set -uo pipefail

# launchd uses a minimal PATH — ensure Homebrew binaries are available
export PATH="/opt/homebrew/bin:$PATH"

# PostgreSQL runs natively (Homebrew postgresql@17); per-job scripts ensure it is up.
# (Removed the legacy Docker Desktop auto-start — unused since the native-Postgres
#  migration, and it wasted up to ~30s + spiked load at job start when Docker's
#  org sign-in blocks the daemon.)

SCRIPT="$1"
JOB_NAME="${2:-$(basename "$SCRIPT")}"
LOGFILE="/path/to/second-brain/logs/${JOB_NAME}-$(date +%Y%m%d).log"

mkdir -p "$(dirname "$LOGFILE")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting $JOB_NAME" >> "$LOGFILE"
# Run Python scripts under the project venv (deps live there); shell scripts run directly.
if [[ "$SCRIPT" == *.py ]]; then
  /path/to/second-brain/.venv/bin/python "$SCRIPT" >> "$LOGFILE" 2>&1
else
  "$SCRIPT" >> "$LOGFILE" 2>&1
fi
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  osascript -e "display notification \"$JOB_NAME failed (exit $EXIT_CODE). Check $LOGFILE\" with title \"Second Brain\" sound name \"Basso\""
  echo "[$(date '+%H:%M:%S')] FAILED (exit $EXIT_CODE)" >> "$LOGFILE"
else
  echo "[$(date '+%H:%M:%S')] OK" >> "$LOGFILE"
fi

# Prune logs older than 30 days
find /path/to/second-brain/logs/ -type f -name "*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
