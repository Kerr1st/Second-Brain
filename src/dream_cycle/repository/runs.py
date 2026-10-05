"""Focused Dream Cycle runs repository."""
import json
from uuid import UUID
from contextlib import contextmanager, nullcontext
from .protocol import Database
_RUN_STAT_COLUMNS = ("candidates_generated", "candidates_accepted", "candidates_rejected")


def create_run(run_type: str, backend_provenance: dict | None=None, *, db: Database) -> str:
    """Insert a new dream_cycle_runs record. Returns run UUID.

    backend_provenance: optional snapshot of the active backend profile —
        ``{role: {backend, model, effort}}`` — stored so a run's verdicts stay
        auditable across a backend swap or the Mac Mini cutover (see
        docs/MODEL-BACKENDS.md). ``None`` (the default) leaves the column NULL.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO dream_cycle_runs (run_type, backend_provenance)
                VALUES (%s, %s)
                RETURNING id
                """, (run_type, json.dumps(backend_provenance) if backend_provenance is not None else None))
            run_id = cur.fetchone()[0]
        conn.commit()
        return str(run_id)

def complete_run(run_id: str, stats: dict, digest: str, explorer_output: str | None=None, explorer_feedback_injected: str | None=None, *, db: Database) -> None:
    """Update run with completion time, stats, and digest text.

    stats keys: candidates_generated, candidates_accepted, candidates_rejected
    (own columns); any further keys (skim_docs, skim_skimmed, skim_reused,
    skim_failed, skim_cost_usd, ...) are stored as JSON in ``stats``
    (NULL when there are none).
    """
    extra = {k: v for k, v in (stats or {}).items() if k not in _RUN_STAT_COLUMNS}
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE dream_cycle_runs
                SET completed_at = now(),
                    candidates_generated = %s,
                    candidates_accepted = %s,
                    candidates_rejected = %s,
                    digest = %s,
                    explorer_output = %s,
                    explorer_feedback_injected = %s,
                    stats = %s,
                    cost_usd = %s
                WHERE id = %s
                """, (stats.get('candidates_generated', 0), stats.get('candidates_accepted', 0), stats.get('candidates_rejected', 0), digest, explorer_output, explorer_feedback_injected, json.dumps(extra, default=str) if extra else None, None if stats.get('spend_unknown') else stats.get('cost_usd', 0.0), run_id))
        conn.commit()

def store_candidate(run_id: str, candidate: dict, verdicts: dict, final_verdict: str, created_memory_id: str | None=None, *, conn=None, db: Database) -> str:
    """Insert a dream_cycle_candidates record. Returns candidate UUID.

    candidate: the Thinker's full output dict (stored as candidate_json JSONB).
               Also extracts operation, target_memory_id, schema_operation.
    verdicts: dict with keys evaluator_a_verdict, evaluator_a_reasoning,
              evaluator_b_verdict, evaluator_b_reasoning,
              evaluator_c_verdict, evaluator_c_reasoning,
              evaluator_d_verdict, evaluator_d_reasoning, and optionally
              evaluator_details ({role: {score, blocking_issues, advisory_issues}},
              migration 013).
    final_verdict: ACCEPTED or REJECTED.
    created_memory_id: UUID of the memory created for ACCEPTED candidates.
    """
    owns_connection = conn is None
    with db.get_connection() if owns_connection else nullcontext(conn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO dream_cycle_candidates (
                    run_id, candidate_json, operation, target_memory_id,
                    schema_operation,
                    evaluator_a_verdict, evaluator_a_reasoning,
                    evaluator_b_verdict, evaluator_b_reasoning,
                    evaluator_c_verdict, evaluator_c_reasoning,
                    evaluator_d_verdict, evaluator_d_reasoning,
                    final_verdict, created_memory_id, evaluator_details
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """, (run_id, json.dumps(candidate), candidate.get('operation'), _candidate_target(candidate.get('target_memory_id')), candidate.get('schema_operation'), verdicts.get('evaluator_a_verdict'), verdicts.get('evaluator_a_reasoning'), verdicts.get('evaluator_b_verdict'), verdicts.get('evaluator_b_reasoning'), verdicts.get('evaluator_c_verdict'), verdicts.get('evaluator_c_reasoning'), verdicts.get('evaluator_d_verdict'), verdicts.get('evaluator_d_reasoning'), final_verdict, created_memory_id, json.dumps(verdicts['evaluator_details'], default=str) if verdicts.get('evaluator_details') else None))
            candidate_id = cur.fetchone()[0]
        if owns_connection:
            conn.commit()
        return str(candidate_id)

def get_evaluator_verdicts_for_run(run_id: str, *, db: Database) -> dict[str, dict]:
    """Get evaluator verdicts for accepted candidates in a run.

    Extracted from generate_digest() inline SQL.

    Args:
        run_id: The dream cycle run UUID.

    Returns:
        Dict keyed by candidate title, each value a dict with keys
        'skeptic', 'advocate', 'epistemologist', 'methodologist', each containing
        {"verdict": str, "reasoning": str}.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT candidate_json,
                       evaluator_a_verdict, evaluator_a_reasoning,
                       evaluator_b_verdict, evaluator_b_reasoning,
                       evaluator_c_verdict, evaluator_c_reasoning,
                       evaluator_d_verdict, evaluator_d_reasoning
                FROM dream_cycle_candidates
                WHERE run_id = %s AND final_verdict = 'ACCEPTED'
                """, (run_id,))
            verdicts_by_title = {}
            for row in cur.fetchall():
                cj = row[0] or {}
                if isinstance(cj, str):
                    try:
                        cj = json.loads(cj)
                    except (json.JSONDecodeError, ValueError):
                        cj = {}
                title = cj.get('title', '')
                verdicts_by_title[title] = {'skeptic': {'verdict': row[1], 'reasoning': row[2]}, 'advocate': {'verdict': row[3], 'reasoning': row[4]}, 'epistemologist': {'verdict': row[5], 'reasoning': row[6]}, 'methodologist': {'verdict': row[7], 'reasoning': row[8]}}
            return verdicts_by_title

def was_feedback_injected(run_id: str, *, db: Database) -> bool:
    """Check if feedback was injected for a given run.

    Extracted from generate_digest() inline SQL.

    Args:
        run_id: The dream cycle run UUID.

    Returns:
        True if explorer_feedback_injected is non-empty for this run.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT explorer_feedback_injected FROM dream_cycle_runs WHERE id = %s', (run_id,))
            row = cur.fetchone()
            return bool(row and row[0])


@contextmanager
def run_lock(*, db: Database):
    """One active Dream Cycle per database, held on one dedicated pooled session.

    The session lock spans model calls without holding a database transaction.
    It is always unlocked before the session is returned to the pool.
    """
    with db.get_connection() as conn:
        acquired = False
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_lock(1935829364, 1)')
                acquired = bool(cur.fetchone()[0])
            conn.commit()
            yield acquired
        finally:
            conn.rollback()
            if acquired:
                try:
                    with conn.cursor() as cur:
                        cur.execute('SELECT pg_advisory_unlock(1935829364, 1)')
                    conn.commit()
                except Exception:
                    conn.close()  # Never return a pooled session that still owns the lock.
                    raise

def _candidate_target(value):
    """Keep malformed model targets in JSON, never coerce them into SQL UUIDs."""
    try:
        return str(UUID(str(value))) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def verdict_columns(verdicts) -> dict:
    """Map independent seat results to stable ledger columns without changing votes."""
    result = {}
    for seat, verdict in zip("abcd", verdicts):
        result[f"evaluator_{seat}_verdict"] = verdict.verdict
        result[f"evaluator_{seat}_reasoning"] = verdict.reasoning
    result["evaluator_details"] = {v.role: {"score": v.score,
        "blocking_issues": v.blocking_issues, "advisory_issues": v.advisory_issues} for v in verdicts}
    return result
