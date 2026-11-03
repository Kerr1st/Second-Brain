"""Backfill the attention ledger for runs that predate migration 012.

Those runs discarded their slices' memory ids, so the best available record of
what the Explorer examined is the union of memory ids the Thinker cited in that
run's candidates (``candidate_json.source_memories``). That is a lower bound —
the Explorer handed the Thinker 10-20 memories per slice and the Thinker cited
a subset — but it is exactly what the panel judged, so it is the right baseline.

Each backfilled run gets one synthetic slice, origin='backfill', named after the
strategies its candidates reported. Runs that already have ledger rows are
skipped; re-running is idempotent.

    python scripts/backfill_attention_ledger.py [--since 2026-09-01] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from psycopg2.extras import RealDictCursor  # noqa: E402

import src.dream_cycle_db as dream_cycle_db  # noqa: E402
from src.db import get_connection  # noqa: E402
from src.dream_cycle import ledger  # noqa: E402

log = logging.getLogger("backfill_attention_ledger")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def runs_needing_backfill(since: str) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT r.id, r.run_type, r.started_at
                FROM dream_cycle_runs r
                WHERE r.completed_at IS NOT NULL AND r.started_at >= %s
                  AND NOT EXISTS (SELECT 1 FROM dream_cycle_slices s WHERE s.run_id = r.id)
                  AND EXISTS (SELECT 1 FROM dream_cycle_candidates c WHERE c.run_id = r.id)
                ORDER BY r.started_at
                """,
                (since,),
            )
            return [dict(r) for r in cur.fetchall()]


def cited_ids_and_strategies(run_id: str) -> tuple[list[str], list[str]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT candidate_json FROM dream_cycle_candidates WHERE run_id = %s ORDER BY created_at", (run_id,))
            rows = cur.fetchall()
    ids: list[str] = []
    strategies: list[str] = []
    for (cj,) in rows:
        if isinstance(cj, str):
            try:
                cj = json.loads(cj)
            except ValueError:
                cj = {}
        cj = cj or {}
        cited = cj.get("source_memories") or []
        if not cited:  # older shape: ids embedded anywhere in the record
            cited = _UUID_RE.findall(json.dumps(cj))
        for m in cited:
            m = str(m)
            if m not in ids:
                ids.append(m)
        s = cj.get("strategy_that_found_it")
        if s and s not in strategies:
            strategies.append(s)
    return ids, strategies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    runs = runs_needing_backfill(args.since)
    log.info("%d completed run(s) since %s without ledger rows", len(runs), args.since)
    for r in runs:
        ids, strategies = cited_ids_and_strategies(str(r["id"]))
        if not ids:
            log.info("%s %s: no cited memory ids, skipping", r["started_at"].date(), r["id"])
            continue
        slice_ = {
            "name": "(backfilled from candidate citations)",
            "strategy": "+".join(strategies) or "unknown",
            "hypothesis": "",
            "memory_ids": ids,
            "memory_titles": [],
        }
        if args.dry_run:
            log.info("[DRY] %s %s: %d cited ids, strategies=%s", r["started_at"].date(), r["id"], len(ids), strategies)
            continue
        metrics = ledger.record_run_slices(str(r["id"]), r["run_type"], [slice_], started_at=r["started_at"],
                                           origins={0: "backfill"})
        log.info("%s %s: %d ids -> arrivals %.0f%%, %d source type(s), concentration %.0f%%, episodic %.0f%%",
                 r["started_at"].date(), str(r["id"])[:8], len(ids), 100 * metrics.get("arrival_share", 0),
                 metrics.get("source_types", 0), 100 * metrics.get("concentration", 0),
                 100 * metrics.get("episodic_share", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
