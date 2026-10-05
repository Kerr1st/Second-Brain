"""Focused Dream Cycle feedback repository."""
from psycopg2.extras import RealDictCursor
from .protocol import Database
_RUN_STAT_COLUMNS = ("candidates_generated", "candidates_accepted", "candidates_rejected")


def get_recent_rejections(n_cycles: int=3, *, db: Database) -> list[dict]:
    """Query last N completed cycles' rejected candidates with evaluator reasoning.

    Returns list of dicts with: run_id, run_type, completed_at, candidate_id,
    final_verdict, evaluator_a_verdict, evaluator_a_reasoning,
    evaluator_b_verdict, evaluator_b_reasoning, evaluator_c_verdict,
    evaluator_c_reasoning, evaluator_d_verdict, evaluator_d_reasoning.

    Used for feedback injection into the Explorer prompt.
    """
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    r.id AS run_id,
                    r.run_type,
                    r.completed_at,
                    c.id AS candidate_id,
                    c.final_verdict,
                    c.evaluator_a_verdict,
                    c.evaluator_a_reasoning,
                    c.evaluator_b_verdict,
                    c.evaluator_b_reasoning,
                    c.evaluator_c_verdict,
                    c.evaluator_c_reasoning,
                    c.evaluator_d_verdict,
                    c.evaluator_d_reasoning
                FROM dream_cycle_candidates c
                JOIN dream_cycle_runs r ON r.id = c.run_id
                WHERE r.completed_at IS NOT NULL
                  AND c.final_verdict = 'REJECTED'
                  AND r.id IN (
                      SELECT id FROM dream_cycle_runs
                      WHERE completed_at IS NOT NULL
                      ORDER BY completed_at DESC
                      LIMIT %s
                  )
                ORDER BY r.completed_at DESC, c.created_at DESC
                """, (n_cycles,))
            return cur.fetchall()

def get_user_rejections(n_cycles: int=3, *, db: Database) -> list[dict]:
    """Query user-rejected candidates from the last N completed cycles.

    User-rejected candidates have final_verdict = 'ACCEPTED' but
    user_rejected_at IS NOT NULL. These are insights that passed consensus
    but were later rejected by the user post-hoc.

    Returns list of dicts with: candidate_id, user_rejection_reason,
    candidate_json (for extracting the title).

    Used for feedback injection into the Explorer prompt alongside
    evaluator rejections.
    Requirements: 14.5, 15.1, 15.4.
    """
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    c.id AS candidate_id,
                    c.user_rejection_reason,
                    c.candidate_json
                FROM dream_cycle_candidates c
                JOIN dream_cycle_runs r ON r.id = c.run_id
                WHERE r.completed_at IS NOT NULL
                  AND c.final_verdict = 'ACCEPTED'
                  AND c.user_rejected_at IS NOT NULL
                  AND r.id IN (
                      SELECT id FROM dream_cycle_runs
                      WHERE completed_at IS NOT NULL
                      ORDER BY completed_at DESC
                      LIMIT %s
                  )
                ORDER BY c.user_rejected_at DESC
                """, (n_cycles,))
            return cur.fetchall()

def get_accepted_dissents(n_cycles: int=3, *, db: Database) -> list[dict]:
    """Query accepted candidates with at least one REJECT verdict from recent cycles.

    Returns rows where final_verdict = 'ACCEPTED' and at least one of
    evaluator_a/b/c/d_verdict = 'REJECT'. Used for feedback injection
    to surface dissenting concerns on accepted insights.
    Requirements: 8.1.
    """
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    c.id AS candidate_id,
                    c.candidate_json,
                    c.evaluator_a_verdict,
                    c.evaluator_a_reasoning,
                    c.evaluator_b_verdict,
                    c.evaluator_b_reasoning,
                    c.evaluator_c_verdict,
                    c.evaluator_c_reasoning,
                    c.evaluator_d_verdict,
                    c.evaluator_d_reasoning,
                    c.final_verdict
                FROM dream_cycle_candidates c
                JOIN dream_cycle_runs r ON r.id = c.run_id
                WHERE r.completed_at IS NOT NULL
                  AND c.final_verdict = 'ACCEPTED'
                  AND (c.evaluator_a_verdict = 'REJECT'
                       OR c.evaluator_b_verdict = 'REJECT'
                       OR c.evaluator_c_verdict = 'REJECT'
                       OR c.evaluator_d_verdict = 'REJECT')
                  AND r.id IN (
                      SELECT id FROM dream_cycle_runs
                      WHERE completed_at IS NOT NULL
                      ORDER BY completed_at DESC
                      LIMIT %s
                  )
                ORDER BY r.completed_at DESC, c.created_at DESC
                """, (n_cycles,))
            return cur.fetchall()

def mark_user_rejected(candidate_id: str, reason: str, *, db: Database) -> None:
    """Mark an accepted insight as user-rejected post-hoc.

    Updates dream_cycle_candidates with rejection timestamp and reason.
    Also sets the created memory's status to 'user_rejected' if one exists.
    Both updates happen in a single transaction.
    Requirements: 14.5, 15.4.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE dream_cycle_candidates
                SET user_rejected_at = now(),
                    user_rejection_reason = %s
                WHERE id = %s
                RETURNING created_memory_id
                """, (reason, candidate_id))
            row = cur.fetchone()
            if row and row[0] is not None:
                cur.execute("""
                    UPDATE memories
                    SET status = 'user_rejected'
                    WHERE id = %s
                    """, (row[0],))
        conn.commit()
