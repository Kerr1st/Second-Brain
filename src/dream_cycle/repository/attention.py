"""Focused Dream Cycle attention repository."""
import json
from psycopg2.extras import RealDictCursor
from .protocol import Database
_RUN_STAT_COLUMNS = ("candidates_generated", "candidates_accepted", "candidates_rejected")


def record_slices(run_id: str, slices: list[dict], *, db: Database) -> None:
    """Persist the run's slices with their memory ids and per-slice metrics.

    ``slices`` items: {name, strategy, hypothesis, memory_ids, memory_titles,
    origin, metrics}. Replaces any rows already recorded for the run so a retry
    within the same run does not duplicate positions.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM dream_cycle_slices WHERE run_id = %s', (run_id,))
            for pos, s in enumerate(slices):
                cur.execute("""
                    INSERT INTO dream_cycle_slices
                        (run_id, position, name, strategy, hypothesis, memory_ids,
                         memory_titles, origin, metrics)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (run_id, pos, (s.get('name') or '')[:500], (s.get('strategy') or '')[:100], (s.get('hypothesis') or '')[:2000], [str(m) for m in s.get('memory_ids') or []], [str(t)[:300] for t in s.get('memory_titles') or []], (s.get('origin') or 'explorer')[:40], json.dumps(s['metrics']) if s.get('metrics') is not None else None))
        conn.commit()

def get_previous_run_started_at(run_id: str, run_type: str | None=None, *, db: Database):
    """started_at of the most recent *completed* run before ``run_id``.

    Restricted to ``run_type`` when given (a scheduled run's arrival window
    starts at the previous scheduled run, not at a user-triggered deep dive
    ten minutes earlier). Returns None when there is no such run.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT started_at FROM dream_cycle_runs
                WHERE completed_at IS NOT NULL
                  AND id <> %s
                  AND started_at < (SELECT started_at FROM dream_cycle_runs WHERE id = %s)
                  AND (%s::text IS NULL OR run_type = %s)
                ORDER BY started_at DESC LIMIT 1
                """, (run_id, run_id, run_type, run_type))
            row = cur.fetchone()
    return row[0] if row else None

def get_recent_slice_memory_ids(before_run_id: str, n_runs: int=3, *, db: Database) -> set[str]:
    """Memory ids that appeared in slices of the ``n_runs`` runs before ``before_run_id``.

    Used for the revisit-rate metric. Backfilled baseline rows count too: a
    memory the Thinker cited last week is a memory that was looked at.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT unnest(s.memory_ids)
                FROM dream_cycle_slices s
                WHERE s.run_id IN (
                    SELECT id FROM dream_cycle_runs
                    WHERE id <> %s
                      AND started_at < (SELECT started_at FROM dream_cycle_runs WHERE id = %s)
                    ORDER BY started_at DESC LIMIT %s
                )
                """, (before_run_id, before_run_id, n_runs))
            return {r[0] for r in cur.fetchall()}

def get_slices_for_run(run_id: str, *, db: Database) -> list[dict]:
    """All slice rows for a run, in position order (for digests and audits)."""
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT position, name, strategy, hypothesis, memory_ids, memory_titles,
                       origin, metrics
                FROM dream_cycle_slices WHERE run_id = %s ORDER BY position
                """, (run_id,))
            return [dict(r) for r in cur.fetchall()]

def get_recent_parent_memories(since, limit: int=2000, *, db: Database) -> list[dict]:
    """Parent-level (document) memories created since ``since``, newest first —
    the candidate pool for the guaranteed arrivals slice."""
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, title, type, source_type, created_at
                FROM memories
                WHERE created_at >= %s AND parent_id IS NULL
                  AND status IS DISTINCT FROM 'superseded'
                ORDER BY created_at DESC
                LIMIT %s
                """, (since, limit))
            return [dict(r) for r in cur.fetchall()]
