#!/bin/bash
# Thin wrapper for launchd-scheduled dream cycle runs.
# Runs the dream cycle (synthesis), then chains the Express push (delivery, P2)
# so the freshest synthesis can reach the user. job_wrapper.sh expects a single
# script path.

# Ensure PostgreSQL container is running
if ! docker exec second-brain-db pg_isready -U memory_bank &>/dev/null; then
  docker start second-brain-db &>/dev/null
  sleep 5
fi

PY=/Users/kerrfir/second-brain/.venv/bin/python

# 1. The dream cycle (synthesis).
"$PY" /Users/kerrfir/second-brain/scripts/dream_cycle_run.py --run-type scheduled
dc_status=$?

# 2. Express push (delivery): gated high by should_push() — sends only on a new
#    cross-project synthesis or contradiction, else a benign no-op. Best-effort:
#    never let it mask the dream cycle's own exit status.
"$PY" /Users/kerrfir/second-brain/scripts/express_push.py || true

exit $dc_status
