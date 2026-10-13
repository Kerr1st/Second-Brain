#!/usr/bin/env python3
"""Run the single Codex Desktop Task capture path."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.capture.codex import run_codex_capture


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture Codex Tasks idle for at least six complete hours."
    )
    parser.add_argument(
        "--task-id",
        help="process only one native Codex Task ID (including an archived Task)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="include archived eligible Codex Tasks through the same capture path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and count eligible Tasks without database, model, or embedding writes",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="override the Codex data directory (defaults to ~/.codex)",
    )
    return parser


#: Liveness backstop for contract misses, which no longer fail the run: the
#: backlog sidecar <status-dir>/codex-capture.backlog.json (src.operations.backlog)
#: counts consecutive runs with semantic_contract_failed > 0, and
#: ``[backlog.codex-capture]`` in config/liveness.toml turns a streak into a
#: liveness advisory (liveness reads ``consecutive_deferred_runs``).
JOB_NAME = "codex-capture"


def streak_update(report):
    """How this run moves the contract-miss streak: "increment", "reset" or None.

    * misses > 0 -> increment (``deferred`` = this run's misses).
    * no misses and ``semantic_processed > 0`` -> reset: a model answer passed
      the contract, which is the only proof the contract works again.
    * anything else -> None, the sidecar is left untouched: a run with no model
      call (nothing new, every batch already abandoned), and outage or
      budget-stopped runs (``infrastructure_failed > 0`` or
      ``semantic_spend_incomplete``) prove nothing either way. Those last two
      are skipped even if they also had misses, since the run did not finish
      its semantic pass.

    The advisory therefore survives "misses, misses, abandon, quiet run" until
    a run actually processes a batch cleanly (2026-10-13 review E1).
    """
    if report.infrastructure_failed > 0 or report.semantic_spend_incomplete:
        return None
    if report.semantic_contract_failed > 0:
        return "increment"
    if report.semantic_processed > 0:
        return "reset"
    return None


def report_semantic_contract(report, *, status_dir=None) -> None:
    """Record this run's contract misses; warn once the streak hits the threshold.

    Bookkeeping must never change the run's outcome, so every error is reported
    on stderr and swallowed. stdout stays the one-line JSON summary.
    """
    misses = report.semantic_contract_failed
    update = streak_update(report)
    if update is None:
        return
    try:
        from src.operations import backlog
        state = backlog.record_run(JOB_NAME, misses if update == "increment" else 0, status_dir)
        limit = backlog.threshold(JOB_NAME)
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"WARNING: could not record the codex-capture semantic-contract state: "
              f"{type(exc).__name__}", file=sys.stderr)
        return
    streak = state["consecutive_deferred_runs"]
    if misses and streak >= limit:
        print(f"WARNING: codex-capture semantic contract misses persist — {misses} this run "
              f"({report.semantic_contract_retrying} retrying, {report.semantic_contract_abandoned} "
              f"abandoned), {streak} consecutive run(s) (threshold {limit}); see failures[].message",
              file=sys.stderr)


def main(argv=None, *, now=None, capture=run_codex_capture, status_dir=None) -> int:
    args = _parser().parse_args(argv)
    if args.codex_home is not None:
        os.environ["CODEX_HOME"] = str(args.codex_home.expanduser())
    report = capture(
        now or datetime.now(tz=UTC),
        task_id=args.task_id,
        backfill=args.backfill,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(report), sort_keys=True))
    # Only full scheduled-shape runs may update the streak: a dry run makes no
    # model call and a single --task-id run must not reset a systematic break.
    # streak_update() decides whether this run moves it at all.
    if not args.dry_run and args.task_id is None:
        report_semantic_contract(report, status_dir=status_dir)
    # Every semantic-contract miss is already recorded (bounded attempt,
    # verbatim task kept) and, since 2026-10-13, never fails the run whether or
    # not attempts remain; only infrastructure failures make it non-zero.
    return 1 if report.infrastructure_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
