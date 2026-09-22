"""Remove byte-identical duplicate source documents (docs/PLAN-mcp-review.md, Phase 2).

Background: until 2026-09-22 the Crawlee parser re-ingested the same transcript
42 times a night (6,160 copies of one YouTube video) and a handful of articles
4–8 times each. This script keeps, for every ``(source_url, md5(content))`` group
of parent-level ``article``/``youtube`` rows, the oldest parent and its chunks,
and removes the rest together with their chunks.

Safety:
* ``--dry-run`` (default) prints counts only and writes nothing.
* ``--apply`` first copies every doomed ``memories`` row into
  ``memories_dedup_backup_<stamp>`` and every ``memory_relationships`` row that
  touches one into ``memory_relationships_dedup_backup_<stamp>`` (same columns),
  then deletes in one transaction. Cascades take care of ``memory_relationships``,
  ``memory_entities`` and ``memory_skim``.
* Rows referenced by ``dream_cycle_candidates.created_memory_id`` (no cascade) or
  cited in an ACCEPTED candidate's ``source_memories`` are exempt: they are the
  evidence behind an accepted insight and stay.

Restore: ``INSERT INTO memories SELECT * FROM memories_dedup_backup_<stamp>;`` then
the same for the relationships table.

Usage:
    .venv/bin/python scripts/cleanup_duplicate_sources.py            # dry run
    .venv/bin/python scripts/cleanup_duplicate_sources.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db import get_connection  # noqa: E402

GROUP_SQL = """
WITH p AS (
    SELECT id, source_url, md5(content) AS h, created_at
    FROM memories
    WHERE parent_id IS NULL AND source_type IN ('article', 'youtube') AND source_url IS NOT NULL
),
ranked AS (
    SELECT id, source_url, h,
           row_number() OVER (PARTITION BY source_url, h ORDER BY created_at, id) AS rn,
           count(*) OVER (PARTITION BY source_url, h) AS n
    FROM p
)
SELECT id, source_url, n FROM ranked WHERE n > 1 AND rn > 1
"""

EXEMPT_SQL = """
SELECT DISTINCT x FROM (
    SELECT created_memory_id::text AS x FROM dream_cycle_candidates WHERE created_memory_id IS NOT NULL
    UNION
    SELECT jsonb_array_elements_text(candidate_json->'source_memories')
    FROM dream_cycle_candidates
    WHERE final_verdict = 'ACCEPTED' AND jsonb_typeof(candidate_json->'source_memories') = 'array'
) s
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually back up and delete (default: dry run)")
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H%M")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(GROUP_SQL)
            rows = cur.fetchall()
            doomed_parents = {str(r[0]) for r in rows}
            groups = {}
            for _id, url, n in rows:
                groups[url] = n
            cur.execute(EXEMPT_SQL)
            exempt = {r[0] for r in cur.fetchall()}
            kept_as_evidence = doomed_parents & exempt
            doomed_parents -= exempt

            if doomed_parents:
                cur.execute("SELECT id::text, parent_id::text FROM memories WHERE parent_id::text = ANY(%s)", (list(doomed_parents),))
                child_rows = cur.fetchall()
                # A chunk cited as evidence keeps its parent too (parent_id FK has no cascade).
                parents_with_kept_children = {p for c, p in child_rows if c in exempt}
                kept_as_evidence |= parents_with_kept_children
                doomed_parents -= parents_with_kept_children
                doomed_chunks = {c for c, p in child_rows if p in doomed_parents} - exempt
            else:
                doomed_chunks = set()
            doomed = doomed_parents | doomed_chunks

            cur.execute(
                "SELECT count(*) FROM memory_relationships WHERE source_id::text = ANY(%s) OR target_id::text = ANY(%s)",
                (list(doomed), list(doomed)),
            )
            n_rel = cur.fetchone()[0]
            cur.execute("SELECT count(*), coalesce(sum(length(content)),0) FROM memories WHERE id::text = ANY(%s)", (list(doomed),))
            n_rows, n_chars = cur.fetchone()

            print(f"duplicate groups: {len(groups)}  (largest: {max(groups.values()) if groups else 0} copies)")
            print(f"doomed parents: {len(doomed_parents)}  doomed chunks: {len(doomed_chunks)}  total rows: {n_rows}  ({n_chars/1e6:.1f} MB of text)")
            print(f"kept as evidence (cited by dream-cycle candidates): {len(kept_as_evidence)}")
            print(f"relationships touching doomed rows (cascade-deleted, backed up): {n_rel}")
            if not doomed:
                print("nothing to do"); return 0
            if not args.apply:
                print("dry run — re-run with --apply to back up and delete"); return 0

            bk_mem = f"memories_dedup_backup_{stamp}"
            bk_rel = f"memory_relationships_dedup_backup_{stamp}"
            cur.execute(f"CREATE TABLE {bk_mem} AS SELECT * FROM memories WHERE id::text = ANY(%s)", (list(doomed),))
            cur.execute(
                f"CREATE TABLE {bk_rel} AS SELECT * FROM memory_relationships WHERE source_id::text = ANY(%s) OR target_id::text = ANY(%s)",
                (list(doomed), list(doomed)),
            )
            cur.execute(f"SELECT count(*) FROM {bk_mem}"); backed = cur.fetchone()[0]
            if backed != n_rows:
                conn.rollback(); print(f"ABORT: backup holds {backed} rows, expected {n_rows}"); return 1
            # chunks first (parent_id FK has no cascade), then parents
            cur.execute("DELETE FROM memories WHERE id::text = ANY(%s)", (list(doomed_chunks),))
            cur.execute("DELETE FROM memories WHERE id::text = ANY(%s)", (list(doomed_parents),))
        conn.commit()
        print(f"deleted {n_rows} rows; backups: {bk_mem} ({backed} rows), {bk_rel} ({n_rel} rows)")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE memories")
        print("VACUUM ANALYZE memories done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
