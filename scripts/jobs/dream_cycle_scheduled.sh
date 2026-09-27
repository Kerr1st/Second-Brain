#!/bin/bash
# Scheduled synthesis only. Delivery remains disabled until configured/deployed.
# Shared job_wrapper.sh owns the environment, logs, lock and timeout.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"
export SECOND_BRAIN_RUN_COST_CAP_USD="${SECOND_BRAIN_RUN_COST_CAP_USD:-9}"
export BEDROCK_MAX_TOKENS="${BEDROCK_MAX_TOKENS:-16384}"
export BEDROCK_REGION="${BEDROCK_REGION:-us-east-1}"
cd "$REPO_ROOT" || exit 2
exec "${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}" scripts/dream_cycle_run.py --run-type scheduled "$@"
