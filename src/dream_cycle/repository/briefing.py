"""Focused Dream Cycle briefing repository."""
from datetime import datetime
from .protocol import Database
_RUN_STAT_COLUMNS = ("candidates_generated", "candidates_accepted", "candidates_rejected")


def get_last_briefing_time(*, db: Database) -> datetime | None:
    """Get the most recent session_start run's completed_at timestamp.

    Returns None if no session_start runs exist.
    Used for 24-hour frequency cap (Requirement 11.5).
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT completed_at
                FROM dream_cycle_runs
                WHERE run_type = 'session_start'
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """)
            row = cur.fetchone()
            return row[0] if row else None

def get_memory_stats(*, db: Database) -> dict:
    """Aggregate stats for Explorer context.

    Returns dict with:
      - total_count: int
      - date_range: dict with min and max created_at
      - recent_activity: count of memories created in last 7 days
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*), min(created_at), max(created_at)
                FROM memories
                """)
            row = cur.fetchone()
            total_count = row[0]
            date_range = {'min': row[1], 'max': row[2]}
            cur.execute("""
                SELECT count(*)
                FROM memories
                WHERE created_at > now() - interval '7 days'
                """)
            recent_activity = cur.fetchone()[0]
        return {'total_count': total_count, 'date_range': date_range, 'recent_activity': recent_activity}


def has_activity_since(since, *, db: Database) -> bool:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM memories WHERE created_at > %s)", (since,))
        if cur.fetchone()[0]:
            return True
        cur.execute("SELECT EXISTS(SELECT 1 FROM dream_cycle_runs WHERE completed_at > %s)", (since,))
        return bool(cur.fetchone()[0])
