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
#: liveness advisory.
JOB_NAME = "codex-capture"


def report_semantic_contract(report, *, status_dir=None) -> None:
    """Record this run's contract misses; warn once the streak hits the threshold.

    Bookkeeping must never change the run's outcome, so every error is reported
    on stderr and swallowed. stdout stays the one-line JSON summary.
    """
    misses = report.semantic_contract_failed
    try:
        from src.operations import backlog
        state = backlog.record_run(JOB_NAME, misses, status_dir)
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
    # Only full scheduled-shape runs update the streak: a dry run makes no model
    # call and a single --task-id run must not reset a systematic break.
    if not args.dry_run and args.task_id is None:
        report_semantic_contract(report, status_dir=status_dir)
    # Every semantic-contract miss is already recorded (bounded attempt,
    # verbatim task kept) and, since 2026-10-13, never fails the run whether or
    # not attempts remain; only infrastructure failures make it non-zero.
    return 1 if report.infrastructure_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
