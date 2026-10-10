#!/bin/bash
# Scheduled dream cycle (synthesis only). launchd → job_wrapper.sh → this → python.
#
# The wrapper (src/operations/jobs.py) owns PATH, AWS_PROFILE, cwd, the per-job
# lock, the deadline (SECOND_BRAIN_JOB_TIMEOUT_DREAM_CYCLE in the plist, ≥ 6 h) and
# the log. On deadline it sends SIGTERM to the process group, waits
# SECOND_BRAIN_JOB_GRACE_S (120 s) and only then SIGKILLs. The orchestrator turns
# SIGTERM into RunInterrupted and completes the run row as aborted with real counts
# (src/dream_cycle/orchestrator.py, R3). `exec` below makes the Python process the
# group leader so there is no bash in between to die first.
#
# Model backends for the scheduled run (see config/backends.toml):
#   controlled = every seat on Bedrock in the user's own account; the corpus never
#   reaches Kiro or a `fable` model (docs/PLAN-panel-acceptance.md §3).
# Cost controls, two layers:
#   - Real-time (here): SECOND_BRAIN_RUN_COST_CAP_USD=9 is the per-run budget (a
#     10-candidate run with the grounding audit costs ~$8.50; runs Mon/Wed/Fri
#     only, see the plist). Exceeding it aborts the rest of the panel — candidates
#     already accepted are kept, the digest is skipped. BEDROCK_MAX_TOKENS bounds a
#     single Astra call at ~$0.90.
#   - Monthly/daily (AWS Budgets, emailed): `second-brain-bedrock-daily` $6/day,
#     `second-brain-bedrock-monthly` $100 with alerts at 50/80/100% + forecast,
#     `account-monthly-total` $175; Cost Anomaly Detection for anomalies ≥ $10.
#     Billing data lags 8–24h, so the daily email is next-day confirmation of a
#     cap trip, not the thing that stops it.
#   Emergency stop: SECOND_BRAIN_KILL_SWITCH=1 refuses to start a run.
#
# Express push (delivery) stays DISABLED (2026-09-24, PLAN-refactor.md Phase 0,
# item 0.2; F-001): email has never been configured, so every result was
# discarded. Re-enable only after the editor route is gated on email_configured().
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$HOME/.toolbox/bin:/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
# shellcheck source=scripts/jobs/_preflight.sh
. "$REPO_ROOT/scripts/jobs/_preflight.sh"

export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"
export SECOND_BRAIN_RUN_COST_CAP_USD="${SECOND_BRAIN_RUN_COST_CAP_USD:-9}"
export BEDROCK_MAX_TOKENS="${BEDROCK_MAX_TOKENS:-16384}"
export BEDROCK_REGION="${BEDROCK_REGION:-us-east-1}"

cd "$REPO_ROOT" || exit 2
require_postgres
exec "${SECOND_BRAIN_PYTHON:-$REPO_ROOT/.venv/bin/python}" scripts/dream_cycle_run.py --run-type scheduled "$@"
