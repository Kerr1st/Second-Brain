#!/usr/bin/env bash
# Deterministic checks only. See docs/TESTING.md for real AI outcome evaluation.
set -euo pipefail
cd "$(dirname "$0")/.."
layer="${1:-all}"
if [[ $# -gt 0 ]]; then shift; fi
python_bin="${SECOND_BRAIN_PYTHON:-.venv/bin/python}"
case "$layer" in
  behavior) exec "$python_bin" -m pytest -m behavior "$@" ;;
  integration|all)
    export TEST_DB_NAME="${TEST_DB_NAME:-second_brain_codex_test}"
    case "$TEST_DB_NAME" in
      memory_bank_test|second_brain_codex_test) ;;
      *) echo "Refusing non-disposable TEST_DB_NAME: $TEST_DB_NAME" >&2; exit 2 ;;
    esac
    export DB_NAME="$TEST_DB_NAME"
    if [[ "$layer" == integration ]]; then
      exec "$python_bin" -m pytest -m integration "$@"
    fi
    exec "$python_bin" -m pytest "$@"
    ;;
  *) echo "Usage: scripts/test.sh {behavior|integration|all} [pytest options]" >&2; exit 2 ;;
esac
