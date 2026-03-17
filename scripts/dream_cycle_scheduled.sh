#!/bin/bash
# Thin wrapper for launchd-scheduled dream cycle runs.
# job_wrapper.sh expects a single script path — this script
# forwards to dream_cycle_run.py with the correct --run-type flag.

exec python3 /Users/kerrfir/second-brain/scripts/dream_cycle_run.py --run-type scheduled
