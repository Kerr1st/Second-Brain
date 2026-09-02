#!/usr/bin/env python3
"""Fill the active local embedding space from preserved Titan rows in resumable batches."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg2.extras import execute_values

from src.db import get_connection
from src.embeddings import active_embedding_space, generate_embeddings_batch


EmbedBatch = Callable[[list[str]], list[list[float]]]


def _eligible_count() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM memories
                WHERE legacy_embedding IS NOT NULL AND embedding IS NULL
                """
            )
            return cur.fetchone()[0]


def reembed_memories(
    *,
    limit: int | None = None,
    batch_size: int = 32,
    embed_batch: EmbedBatch = generate_embeddings_batch,
) -> dict[str, int]:
    """Fill missing active vectors, committing each batch so interruption is resumable."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    eligible = _eligible_count()
    target = eligible if limit is None else min(eligible, limit)
    processed = 0
    space = active_embedding_space()

    while processed < target:
        take = min(batch_size, target - processed)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text, content
                    FROM memories
                    WHERE legacy_embedding IS NOT NULL AND embedding IS NULL
                    ORDER BY CASE WHEN type = 'source' THEN 1 ELSE 0 END,
                             created_at,
                             id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (take,),
                )
                rows = cur.fetchall()
                if not rows:
                    conn.rollback()
                    break
                vectors = embed_batch([row[1] or "" for row in rows])
                if len(vectors) != len(rows):
                    raise ValueError(
                        f"embedding batch returned {len(vectors)} vectors for {len(rows)} rows"
                    )
                execute_values(
                    cur,
                    """
                    UPDATE memories AS m
                    SET embedding = data.embedding::vector,
                        embedding_space = data.space,
                        updated_at = now()
                    FROM (VALUES %s) AS data(id, embedding, space)
                    WHERE m.id = data.id::uuid AND m.embedding IS NULL
                    """,
                    [
                        (memory_id, str(vector), space)
                        for (memory_id, _), vector in zip(rows, vectors, strict=True)
                    ],
                )
            conn.commit()
        processed += len(rows)

    return {
        "eligible": eligible,
        "processed": processed,
        "remaining": _eligible_count(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the number of legacy rows awaiting local vectors without writing",
    )
    args = parser.parse_args()

    if args.dry_run:
        eligible = _eligible_count()
        result = {"eligible": eligible, "processed": 0, "remaining": eligible}
    else:
        result = reembed_memories(limit=args.limit, batch_size=args.batch_size)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
