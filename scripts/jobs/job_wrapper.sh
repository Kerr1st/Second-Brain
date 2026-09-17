#!/bin/bash
# Wrapper for Second Brain scheduled jobs.
# Runs the given script, logs output, and sends a macOS notification on failure.
# Usage: job_wrapper.sh <script_path> <job_name>

set -uo pipefail

# launchd uses a minimal PATH — ensure Homebrew binaries are available
export PATH="/opt/homebrew/bin:$PATH"
# Unattended jobs use a least-privilege IAM user (Bedrock InvokeModel only), not the
# interactive SSO session: the 8h Identity Center session is what silently killed
# every job on 2026-09-08. Override by exporting AWS_PROFILE before invoking.
export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"

# PostgreSQL runs natively (Homebrew postgresql@17); per-job scripts ensure it is up.
# (Removed the legacy Docker Desktop auto-start — unused since the native-Postgres
#  migration, and it wasted up to ~30s + spiked load at job start when Docker's
#  org sign-in blocks the daemon.)

SCRIPT="$1"
JOB_NAME="${2:-$(basename "$SCRIPT")}"
LOGFILE="/Users/kerrfir/second-brain/logs/${JOB_NAME}-$(date +%Y%m%d).log"

mkdir -p "$(dirname "$LOGFILE")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting $JOB_NAME" >> "$LOGFILE"
# Run Python scripts under the project venv (deps live there); shell scripts run directly.
# caffeinate -i holds a "prevent idle sleep" assertion for the job's lifetime. On
# battery this Mac sleeps after 1 idle minute and only surfaces for ~45s Power Nap
# slices; on 2026-09-17 every nightly job was suspended mid-run that way.
if [[ "$SCRIPT" == *.py ]]; then
  /usr/bin/caffeinate -i /Users/kerrfir/second-brain/.venv/bin/python "$SCRIPT" >> "$LOGFILE" 2>&1
else
  /usr/bin/caffeinate -i "$SCRIPT" >> "$LOGFILE" 2>&1
fi
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  osascript -e "display notification \"$JOB_NAME failed (exit $EXIT_CODE). Check $LOGFILE\" with title \"Second Brain\" sound name \"Basso\""
  echo "[$(date '+%H:%M:%S')] FAILED (exit $EXIT_CODE)" >> "$LOGFILE"
else
  echo "[$(date '+%H:%M:%S')] OK" >> "$LOGFILE"
fi

# Prune logs older than 30 days
find /Users/kerrfir/second-brain/logs/ -type f -name "*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
