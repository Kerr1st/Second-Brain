#!/usr/bin/env python3
"""Backfill mem_class column for all memories where it is NULL.

Reads memories in batches, classifies each using classify_memory(),
and updates the mem_class column. Idempotent — re-running skips
already-classified rows.

Usage:
    python scripts/backfill_mem_class.py [--batch-size N] [--dry-run] [--limit N]
"""

import argparse
import sys

from src.classify import classify_memory
from src.db import get_connection


def _parse_backfill_args(description: str = "Backfill mem_class column for all memories.",
                         default_batch_size: int = 500) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--batch-size", type=int, default=default_batch_size,
                        help="Rows per batch/transaction (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute but don't commit writes")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap total rows processed")
    return parser.parse_args()


def _log(msg: str, dry_run: bool = False) -> None:
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}{msg}", flush=True)


def main() -> int:
    args = _parse_backfill_args()
    dry_run = args.dry_run
    batch_size = args.batch_size
    limit = args.limit

    total_processed = 0
    total_errors = 0
    breakdown = {"semantic": 0, "episodic": 0, "procedural": 0}

    conn = get_connection()
    try:
        while True:
            # Respect --limit: only fetch what we still need
            fetch_size = batch_size
            if limit is not None:
                remaining_budget = limit - total_processed
                if remaining_budget <= 0:
                    break
                fetch_size = min(batch_size, remaining_budget)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, type, source_type, content "
                    "FROM memories WHERE mem_class IS NULL "
                    "ORDER BY id LIMIT %s",
                    (fetch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                break

            updates = []  # list of (mem_class, id) tuples
            batch_errors = 0

            for row in rows:
                mem_id, mem_type, source_type, content = row
                try:
                    cls = classify_memory(mem_type, source_type, content)
                    updates.append((cls, mem_id))
                    breakdown[cls] = breakdown.get(cls, 0) + 1
                except Exception as exc:
                    _log(f"ERROR classifying memory {mem_id}: {exc}", dry_run)
                    total_errors += 1
                    batch_errors += 1

            # Batch UPDATE
            if updates:
                with conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE memories SET mem_class = %s WHERE id = %s",
                        updates,
                    )

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            batch_processed = len(updates)
            total_processed += batch_processed

            # Count remaining NULL rows for progress log
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM memories WHERE mem_class IS NULL")
                remaining = cur.fetchone()[0]

            _log(
                f"Batch: {batch_processed} processed, "
                f"{batch_errors} errors, {remaining} remaining",
                dry_run,
            )

    finally:
        conn.close()

    # Final summary
    _log("--- Summary ---", dry_run)
    _log(f"Total processed: {total_processed}", dry_run)
    _log(f"Total skipped: 0", dry_run)
    _log(f"Total errors: {total_errors}", dry_run)
    _log(f"Breakdown: semantic={breakdown['semantic']}, "
         f"episodic={breakdown['episodic']}, "
         f"procedural={breakdown['procedural']}", dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
