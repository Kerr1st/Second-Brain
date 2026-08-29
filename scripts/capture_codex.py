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


def main(argv=None, *, now=None, capture=run_codex_capture) -> int:
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
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
