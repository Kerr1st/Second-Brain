#!/usr/bin/env python3
"""Dream Cycle CLI entry point.

Triggers a dream cycle run with the specified execution mode.
Wraps DreamCycleOrchestrator.run() with argument parsing and exit codes.

Usage:
    python scripts/dream_cycle_run.py --run-type scheduled
    python scripts/dream_cycle_run.py --run-type user_triggered --topic "database patterns"
    python scripts/dream_cycle_run.py --run-type post_learn --memory-ids uuid1,uuid2
    python scripts/dream_cycle_run.py --run-type session_start

Exit codes: 0 = pipeline ran (any accept/reject mix is healthy); 2 = failed to run
(circuit breaker / crash / DB unreachable). Candidate rejections do NOT signal
failure — quality is tracked via metrics, not the exit code.

Requirements: 19.2, 19.3, 19.4, 19.5
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure imports work when run from the scripts directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dream_cycle import DreamCycleOrchestrator

VALID_RUN_TYPES = ("scheduled", "post_learn", "session_start", "user_triggered")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a dream cycle pipeline.",
    )
    parser.add_argument(
        "--run-type",
        required=True,
        choices=VALID_RUN_TYPES,
        help="Execution mode for the dream cycle.",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Topic scope for user_triggered mode.",
    )
    parser.add_argument(
        "--memory-ids",
        default=None,
        help="Comma-separated memory UUIDs for post_learn mode.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Build scope dict from arguments
    scope: dict | None = None
    if args.run_type == "user_triggered" and args.topic:
        scope = {"topic": args.topic}
    elif args.run_type == "post_learn" and args.memory_ids:
        scope = {"memory_ids": args.memory_ids.split(",")}

    try:
        orch = DreamCycleOrchestrator()
        result = orch.run(run_type=args.run_type, scope=scope)
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 2

    # Print summary
    print(f"Run ID:     {result.run_id or '(none)'}")
    print(f"Run type:   {result.run_type}")
    print(f"Generated:  {result.candidates_generated}")
    print(f"Accepted:   {result.candidates_accepted}")
    print(f"Rejected:   {result.candidates_rejected}")
    if result.digest_path:
        print(f"Digest:     {result.digest_path}")
    if result.aborted_early:
        print("Status:     aborted early")

    return exit_code_for(result)


def exit_code_for(result) -> int:
    """Map a DreamCycleResult to a process exit code.

    The exit code signals whether the pipeline *ran*, not how many candidates the
    panel accepted. Candidate rejections — and even zero acceptances — are normal,
    healthy outcomes (the BFT panel doing its job), so they exit 0. Only a genuine
    failure-to-run (circuit breaker / crash / DB unreachable → aborted_early) is
    non-zero, so job_wrapper's failure alert fires only on real failures. Quality is
    tracked via the metrics trends, not this code.
    """
    return 2 if result.aborted_early else 0


if __name__ == "__main__":
    sys.exit(main())
