#!/usr/bin/env python3
"""Backfill depth_score in metadata JSONB for all memories where it is NULL.

Reads memories in batches, computes depth score using compute_depth_score(),
merges the score into the existing metadata dict, and writes back the full
JSONB column. Idempotent — re-running skips rows that already have a
depth_score key in metadata.

Usage:
    python scripts/backfill_depth_score.py [--batch-size N] [--dry-run] [--limit N]
"""

import argparse
import json
import sys

from src.depth import compute_depth_score
from src.db import get_connection


def _parse_backfill_args(description: str = "Backfill depth_score in metadata for all memories.",
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
    all_scores: list[float] = []

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
                    "SELECT id, content, metadata "
                    "FROM memories WHERE metadata->>'depth_score' IS NULL "
                    "ORDER BY id LIMIT %s",
                    (fetch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                break

            updates = []  # list of (metadata_json, id) tuples
            batch_errors = 0

            for row in rows:
                mem_id, content, metadata_raw = row
                try:
                    score = compute_depth_score(content)

                    # Python-side JSONB merge: read existing dict, add depth_score
                    if isinstance(metadata_raw, str):
                        metadata = json.loads(metadata_raw)
                    elif isinstance(metadata_raw, dict):
                        metadata = metadata_raw
                    else:
                        metadata = {}
                    metadata["depth_score"] = score

                    updates.append((json.dumps(metadata), mem_id))
                    all_scores.append(score)
                except Exception as exc:
                    _log(f"ERROR computing depth_score for memory {mem_id}: {exc}", dry_run)
                    total_errors += 1
                    batch_errors += 1

            # Batch UPDATE
            if updates:
                with conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE memories SET metadata = %s WHERE id = %s",
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
                cur.execute("SELECT count(*) FROM memories WHERE metadata->>'depth_score' IS NULL")
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

    if all_scores:
        min_score = min(all_scores)
        max_score = max(all_scores)
        mean_score = sum(all_scores) / len(all_scores)
        _log(f"Depth scores: min={min_score:.4f}, max={max_score:.4f}, mean={mean_score:.4f}", dry_run)
    else:
        _log("Depth scores: no scores computed", dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
