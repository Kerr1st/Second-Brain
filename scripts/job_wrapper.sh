#!/bin/bash
# Wrapper for Second Brain scheduled jobs.
# Runs the given script, logs output, and sends a macOS notification on failure.
# Usage: job_wrapper.sh <script_path> <job_name>

set -uo pipefail

SCRIPT="$1"
JOB_NAME="${2:-$(basename "$SCRIPT")}"
LOGFILE="/Users/kerrfir/second-brain/logs/${JOB_NAME}-$(date +%Y%m%d).log"

mkdir -p "$(dirname "$LOGFILE")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting $JOB_NAME" >> "$LOGFILE"
"$SCRIPT" >> "$LOGFILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  osascript -e "display notification \"$JOB_NAME failed (exit $EXIT_CODE). Check $LOGFILE\" with title \"Second Brain\" sound name \"Basso\""
  echo "[$(date '+%H:%M:%S')] FAILED (exit $EXIT_CODE)" >> "$LOGFILE"
else
  echo "[$(date '+%H:%M:%S')] OK" >> "$LOGFILE"
fi

exit $EXIT_CODE
