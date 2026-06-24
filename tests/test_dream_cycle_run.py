"""Exit-code semantics for the dream-cycle runner (scripts/dream_cycle_run.py).

Regression guard: a healthy run with rejected candidates must exit 0, not 1.
Before 2026-06-13 the runner returned 1 whenever the BFT panel rejected any
candidate, so job_wrapper fired a false "dream-cycle failed" alert on healthy
runs (e.g., 7 accepted / 1 rejected — and, by that logic, the Jun-6 baseline too).
The exit code now signals only whether the pipeline *ran*; quality is tracked
via metrics, not the exit code.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.models import DreamCycleResult

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "dream_cycle_run", _REPO / "scripts" / "dream_cycle_run.py"
)
dream_cycle_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dream_cycle_run)


def _result(generated, accepted, rejected, aborted_early=False):
    now = datetime.now(timezone.utc)
    return DreamCycleResult(
        run_id="r",
        run_type="scheduled",
        started_at=now,
        completed_at=now,
        candidates_generated=generated,
        candidates_accepted=accepted,
        candidates_rejected=rejected,
        digest_path=None,
        aborted_early=aborted_early,
    )


@pytest.mark.parametrize(
    "gen,acc,rej",
    [
        (8, 7, 1),   # the run that false-failed on 2026-06-13
        (9, 8, 1),   # the Jun-6 baseline shape
        (8, 8, 0),   # all accepted
        (3, 0, 3),   # all rejected — still a healthy run
        (0, 0, 0),   # nothing generated, but not aborted
    ],
)
def test_completed_run_exits_zero(gen, acc, rej):
    assert dream_cycle_run.exit_code_for(_result(gen, acc, rej)) == 0


def test_aborted_run_exits_two():
    assert dream_cycle_run.exit_code_for(_result(0, 0, 0, aborted_early=True)) == 2
