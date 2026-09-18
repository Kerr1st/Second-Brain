#!/bin/bash
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
# Thin wrapper for launchd-scheduled dream cycle runs.
# Runs the dream cycle (synthesis), then chains the Express push (delivery, P2)
# so the freshest synthesis can reach the user. job_wrapper.sh expects a single
# script path.

# Ensure PostgreSQL container is running
if ! pg_isready -h 127.0.0.1 -p 5432 -U memory_bank &>/dev/null; then
  brew services start postgresql@17 &>/dev/null
  sleep 5
fi

PY=/Users/kerrfir/second-brain/.venv/bin/python

# Model backends for the nightly run (see config/backends.toml):
#   hybrid = Claude roles on Kiro (credits), only the two GPT-6 Astra evaluator
#   seats metered on Bedrock (≈ $2.30/run measured). Bedrock auth is the CLI
#   `default` profile (kiro-admin-session SSO; boto3 refreshes the token).
# Cost controls, two layers:
#   - Real-time (here): SECOND_BRAIN_RUN_COST_CAP_USD=6 is the per-run budget
#     (≈ 2.6x a typical $2.30 run; a p95 night is ≈ $5). Exceeding it aborts
#     the rest of the panel — candidates already accepted are kept, the digest
#     is skipped. BEDROCK_MAX_TOKENS bounds a single Astra call at ~$0.90.
#   - Monthly/daily (AWS Budgets, emailed): `second-brain-bedrock-daily` $6/day,
#     `second-brain-bedrock-monthly` $100 with alerts at 50/80/100% + forecast,
#     `account-monthly-total` $175; Cost Anomaly Detection for anomalies ≥ $10.
#     Billing data lags 8–24h, so the daily email is next-day confirmation of
#     a cap trip, not the thing that stops it.
#   Emergency stop: SECOND_BRAIN_KILL_SWITCH=1 refuses to start a run.
export SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"
export SECOND_BRAIN_RUN_COST_CAP_USD="${SECOND_BRAIN_RUN_COST_CAP_USD:-6}"
export BEDROCK_MAX_TOKENS="${BEDROCK_MAX_TOKENS:-16384}"
export BEDROCK_REGION="${BEDROCK_REGION:-us-east-1}"

# 1. The dream cycle (synthesis).
"$PY" /Users/kerrfir/second-brain/scripts/dream_cycle_run.py --run-type scheduled
dc_status=$?

# 2. Express push (delivery): gated high by should_push() — sends only on a new
#    cross-project synthesis or contradiction, else a benign no-op. Best-effort:
#    never let it mask the dream cycle's own exit status.
"$PY" /Users/kerrfir/second-brain/scripts/express_push.py || true

exit $dc_status
