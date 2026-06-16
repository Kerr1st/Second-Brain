"""Dream Cycle database operations.

Extends the core db.py with dream-cycle-specific queries for
dream_cycle_runs and dream_cycle_candidates tables.
"""

import json
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from src.db import get_connection


def create_run(run_type: str, backend_provenance: dict | None = None) -> str:
    """Insert a new dream_cycle_runs record. Returns run UUID.

    backend_provenance: optional snapshot of the active backend profile —
        ``{role: {backend, model, effort}}`` — stored so a run's verdicts stay
        auditable across a backend swap or the Mac Mini cutover (see
        docs/MODEL-BACKENDS.md). ``None`` (the default) leaves the column NULL.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dream_cycle_runs (run_type, backend_provenance)
                VALUES (%s, %s)
                RETURNING id
                """,
                (
                    run_type,
                    json.dumps(backend_provenance) if backend_provenance is not None else None,
                ),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        return str(run_id)


def complete_run(run_id: str, stats: dict, digest: str,
                 explorer_output: str | None = None,
                 explorer_feedback_injected: str | None = None) -> None:
    """Update run with completion time, stats, and digest text.

    stats keys: candidates_generated, candidates_accepted, candidates_rejected
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dream_cycle_runs
                SET completed_at = now(),
                    candidates_generated = %s,
                    candidates_accepted = %s,
                    candidates_deferred = 0,
                    candidates_rejected = %s,
                    digest = %s,
                    explorer_output = %s,
                    explorer_feedback_injected = %s
                WHERE id = %s
                """,
                (
                    stats.get("candidates_generated", 0),
                    stats.get("candidates_accepted", 0),
                    stats.get("candidates_rejected", 0),
                    digest,
                    explorer_output,
                    explorer_feedback_injected,
                    run_id,
                ),
            )
        conn.commit()


def store_candidate(
    run_id: str,
    candidate: dict,
    verdicts: dict,
    final_verdict: str,
    created_memory_id: str | None = None,
) -> str:
    """Insert a dream_cycle_candidates record. Returns candidate UUID.

    candidate: the Thinker's full output dict (stored as candidate_json JSONB).
               Also extracts operation, target_memory_id, schema_operation.
    verdicts: dict with keys evaluator_a_verdict, evaluator_a_reasoning,
              evaluator_b_verdict, evaluator_b_reasoning,
              evaluator_c_verdict, evaluator_c_reasoning,
              evaluator_d_verdict, evaluator_d_reasoning.
    final_verdict: ACCEPTED or REJECTED.
    created_memory_id: UUID of the memory created for ACCEPTED candidates.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dream_cycle_candidates (
                    run_id, candidate_json, operation, target_memory_id,
                    schema_operation,
                    evaluator_a_verdict, evaluator_a_reasoning,
                    evaluator_b_verdict, evaluator_b_reasoning,
                    evaluator_c_verdict, evaluator_c_reasoning,
                    evaluator_d_verdict, evaluator_d_reasoning,
                    final_verdict, created_memory_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    run_id,
                    json.dumps(candidate),
                    candidate.get("operation"),
                    candidate.get("target_memory_id"),
                    candidate.get("schema_operation"),
                    verdicts.get("evaluator_a_verdict"),
                    verdicts.get("evaluator_a_reasoning"),
                    verdicts.get("evaluator_b_verdict"),
                    verdicts.get("evaluator_b_reasoning"),
                    verdicts.get("evaluator_c_verdict"),
                    verdicts.get("evaluator_c_reasoning"),
                    verdicts.get("evaluator_d_verdict"),
                    verdicts.get("evaluator_d_reasoning"),
                    final_verdict,
                    created_memory_id,
                ),
            )
            candidate_id = cur.fetchone()[0]
        conn.commit()
        return str(candidate_id)


def get_recent_rejections(n_cycles: int = 3) -> list[dict]:
    """Query last N completed cycles' rejected candidates with evaluator reasoning.

    Returns list of dicts with: run_id, run_type, completed_at, candidate_id,
    final_verdict, evaluator_a_verdict, evaluator_a_reasoning,
    evaluator_b_verdict, evaluator_b_reasoning, evaluator_c_verdict,
    evaluator_c_reasoning, evaluator_d_verdict, evaluator_d_reasoning.

    Used for feedback injection into the Explorer prompt.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
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
                """,
                (n_cycles,),
            )
            return cur.fetchall()

def get_user_rejections(n_cycles: int = 3) -> list[dict]:
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
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
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
                """,
                (n_cycles,),
            )
            return cur.fetchall()



def get_accepted_dissents(n_cycles: int = 3) -> list[dict]:
    """Query accepted candidates with at least one REJECT verdict from recent cycles.

    Returns rows where final_verdict = 'ACCEPTED' and at least one of
    evaluator_a/b/c/d_verdict = 'REJECT'. Used for feedback injection
    to surface dissenting concerns on accepted insights.
    Requirements: 8.1.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
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
                """,
                (n_cycles,),
            )
            return cur.fetchall()


def get_last_briefing_time() -> datetime | None:
    """Get the most recent session_start run's completed_at timestamp.

    Returns None if no session_start runs exist.
    Used for 24-hour frequency cap (Requirement 11.5).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT completed_at
                FROM dream_cycle_runs
                WHERE run_type = 'session_start'
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """,
            )
            row = cur.fetchone()
            return row[0] if row else None


def should_run_briefing() -> bool:
    """Check whether a session_start briefing should run.

    Both conditions must hold:
      (a) 24-hour gap since the last session_start run's completed_at
      (b) New memories exist OR a dream cycle ran since the last session

    If no previous session_start exists, returns True (first run always allowed).
    Requirements: 11.5, 11.6.
    """
    last_briefing = get_last_briefing_time()

    # First run is always allowed
    if last_briefing is None:
        return True

    # Condition (a): 24-hour gap
    now = datetime.now(timezone.utc)
    if (now - last_briefing).total_seconds() < 86400:
        return False

    # Condition (b): new memories OR a dream cycle ran since last session
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Check for new memories since last briefing
            cur.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM memories
                    WHERE created_at > %s
                )
                """,
                (last_briefing,),
            )
            new_memories = cur.fetchone()[0]

            if new_memories:
                return True

            # Check for a dream cycle run completed since last briefing
            cur.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM dream_cycle_runs
                    WHERE completed_at > %s
                )
                """,
                (last_briefing,),
            )
            dream_cycle_ran = cur.fetchone()[0]

            return dream_cycle_ran


def get_memory_stats() -> dict:
    """Aggregate stats for Explorer context.

    Returns dict with:
      - total_count: int
      - date_range: dict with min and max created_at
      - type_distribution: dict of type -> count
      - recent_activity: count of memories created in last 7 days
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Total count and date range
            cur.execute(
                """
                SELECT count(*), min(created_at), max(created_at)
                FROM memories
                """,
            )
            row = cur.fetchone()
            total_count = row[0]
            date_range = {"min": row[1], "max": row[2]}

            # Type distribution
            cur.execute(
                """
                SELECT type, count(*)
                FROM memories
                GROUP BY type
                ORDER BY count(*) DESC
                """,
            )
            type_distribution = {r[0]: r[1] for r in cur.fetchall()}

            # Recent activity (last 7 days)
            cur.execute(
                """
                SELECT count(*)
                FROM memories
                WHERE created_at > now() - interval '7 days'
                """,
            )
            recent_activity = cur.fetchone()[0]

        return {
            "total_count": total_count,
            "date_range": date_range,
            "type_distribution": type_distribution,
            "recent_activity": recent_activity,
        }


def mark_user_rejected(candidate_id: str, reason: str) -> None:
    """Mark an accepted insight as user-rejected post-hoc.

    Updates dream_cycle_candidates with rejection timestamp and reason.
    Also sets the created memory's status to 'user_rejected' if one exists.
    Both updates happen in a single transaction.
    Requirements: 14.5, 15.4.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Update the candidate record
            cur.execute(
                """
                UPDATE dream_cycle_candidates
                SET user_rejected_at = now(),
                    user_rejection_reason = %s
                WHERE id = %s
                RETURNING created_memory_id
                """,
                (reason, candidate_id),
            )
            row = cur.fetchone()

            # Update the memory status if a memory was created
            if row and row[0] is not None:
                cur.execute(
                    """
                    UPDATE memories
                    SET status = 'user_rejected'
                    WHERE id = %s
                    """,
                    (row[0],),
                )
        conn.commit()

def get_golden_queries() -> list[dict]:
    """Extract 'Questions this answers' from accepted dream cycle insights.

    Queries dream_cycle_candidates where final_verdict = 'ACCEPTED' and
    created_memory_id IS NOT NULL. Parses the candidate_json JSONB to
    extract the content field, then looks for lines starting with
    'Questions this answers:' and extracts the queries.

    Returns list of dicts with: candidate_id, memory_id, queries (list[str]).
    Used for Tier 3 metrics (Requirement 18.3).
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, created_memory_id, candidate_json
                FROM dream_cycle_candidates
                WHERE final_verdict = 'ACCEPTED'
                  AND created_memory_id IS NOT NULL
                """,
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            candidate_json = row["candidate_json"]
            if isinstance(candidate_json, str):
                candidate_json = json.loads(candidate_json)

            content = candidate_json.get("content", "")
            queries = extract_golden_queries(content)

            if queries:
                results.append(
                    {
                        "candidate_id": str(row["id"]),
                        "memory_id": str(row["created_memory_id"]),
                        "queries": queries,
                    }
                )

        return results


def extract_golden_queries(content: str) -> list[str]:
    """Parse 'Questions this answers:' section from insight content.

    Looks for a line starting with 'Questions this answers:' and extracts
    subsequent lines that start with '- ' as individual queries.
    """
    lines = content.split("\n")
    in_section = False
    queries = []

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("questions this answers:"):
            in_section = True
            # Check if queries are on the same line after the colon
            after_colon = stripped.split(":", 1)[1].strip()
            if after_colon:
                queries.append(after_colon)
            continue

        if in_section:
            if stripped.startswith("- "):
                queries.append(stripped[2:].strip())
            elif stripped.startswith("* "):
                queries.append(stripped[2:].strip())
            elif stripped == "":
                # Empty line ends the section
                if queries:
                    break
            else:
                # Non-list line after section start — could be end of section
                if queries:
                    break

    return queries


def get_tier1_metrics(n_cycles: int = 10) -> dict:
    """Compute Tier 1 process metrics from dream_cycle_runs and dream_cycle_candidates.

    Returns dict with:
      - acceptance_rate: float (candidates_accepted / candidates_generated across last n_cycles)
      - acceptance_rate_trend: list[float] (acceptance rate per cycle, oldest first)
      - non_unanimous_acceptance_rate: float (accepted candidates with 3/4 verdict / total accepted)
      - strategy_diversity: int (distinct strategies used across last n_cycles)
      - cost_efficiency: float (candidates_accepted / estimated agent invocations)

    All derivable via SQL aggregates on dream_cycle_runs and dream_cycle_candidates.
    Requirements: 18.1.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get the last n_cycles completed runs
            cur.execute(
                """
                SELECT id, candidates_generated, candidates_accepted,
                       candidates_rejected
                FROM dream_cycle_runs
                WHERE completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT %s
                """,
                (n_cycles,),
            )
            runs = cur.fetchall()

            if not runs:
                return {
                    "acceptance_rate": 0.0,
                    "acceptance_rate_trend": [],
                    "non_unanimous_acceptance_rate": 0.0,
                    "strategy_diversity": 0,
                    "cost_efficiency": 0.0,
                }

            run_ids = [r["id"] for r in runs]

            # Acceptance rate: total accepted / total generated across all runs
            total_generated = sum(r["candidates_generated"] or 0 for r in runs)
            total_accepted = sum(r["candidates_accepted"] or 0 for r in runs)
            acceptance_rate = total_accepted / total_generated if total_generated > 0 else 0.0

            # Acceptance rate trend: per-cycle rate, oldest first
            acceptance_rate_trend = []
            for r in reversed(runs):
                gen = r["candidates_generated"] or 0
                acc = r["candidates_accepted"] or 0
                acceptance_rate_trend.append(acc / gen if gen > 0 else 0.0)

            # Non-unanimous acceptance rate
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE final_verdict = 'ACCEPTED') AS total_accepted,
                    count(*) FILTER (
                        WHERE final_verdict = 'ACCEPTED'
                          AND (evaluator_a_verdict = 'REJECT'
                               OR evaluator_b_verdict = 'REJECT'
                               OR evaluator_c_verdict = 'REJECT'
                               OR evaluator_d_verdict = 'REJECT')
                    ) AS non_unanimous
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s)
                """,
                (run_ids,),
            )
            row = cur.fetchone()
            t_accepted = row["total_accepted"]
            non_unanimous = row["non_unanimous"]
            non_unanimous_acceptance_rate = non_unanimous / t_accepted if t_accepted > 0 else 0.0

            # Strategy diversity
            cur.execute(
                """
                SELECT count(DISTINCT candidate_json->>'strategy_that_found_it') AS diversity
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s)
                  AND candidate_json->>'strategy_that_found_it' IS NOT NULL
                """,
                (run_ids,),
            )
            strategy_diversity = cur.fetchone()["diversity"]

            # Cost efficiency
            total_invocations = 0
            for r in runs:
                gen = r["candidates_generated"] or 0
                estimated_slices = max(1, -(-gen // 2))
                explorer_calls = 1
                thinker_calls = estimated_slices
                evaluator_calls = 4 * gen
                total_invocations += explorer_calls + thinker_calls + evaluator_calls

            cost_efficiency = total_accepted / total_invocations if total_invocations > 0 else 0.0

        return {
            "acceptance_rate": acceptance_rate,
            "acceptance_rate_trend": acceptance_rate_trend,
            "non_unanimous_acceptance_rate": non_unanimous_acceptance_rate,
            "strategy_diversity": strategy_diversity,
            "cost_efficiency": cost_efficiency,
        }


def get_tier2_metrics(n_cycles: int = 10) -> dict:
    """Compute Tier 2 engagement metrics.

    Returns dict with:
      - user_rejection_rate: float (count of user_rejected_at IS NOT NULL / total accepted)
      - rejection_reason_clusters: dict of reason -> count (group by user_rejection_reason)
      - insight_citation_rate: float (average access_count on memories tagged 'dream-cycle')

    Requirements: 18.2.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get the last n_cycles completed run IDs
            cur.execute(
                """
                SELECT id
                FROM dream_cycle_runs
                WHERE completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT %s
                """,
                (n_cycles,),
            )
            run_ids = [r["id"] for r in cur.fetchall()]

            if not run_ids:
                return {
                    "user_rejection_rate": 0.0,
                    "rejection_reason_clusters": {},
                    "insight_citation_rate": 0.0,
                }

            # User rejection rate
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE final_verdict = 'ACCEPTED') AS total_accepted,
                    count(*) FILTER (WHERE final_verdict = 'ACCEPTED' AND user_rejected_at IS NOT NULL) AS total_rejected
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s)
                """,
                (run_ids,),
            )
            row = cur.fetchone()
            total_accepted = row["total_accepted"]
            total_rejected = row["total_rejected"]
            user_rejection_rate = total_rejected / total_accepted if total_accepted > 0 else 0.0

            # Rejection reason clusters
            cur.execute(
                """
                SELECT user_rejection_reason, count(*) AS cnt
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s)
                  AND user_rejected_at IS NOT NULL
                  AND user_rejection_reason IS NOT NULL
                GROUP BY user_rejection_reason
                ORDER BY cnt DESC
                """,
                (run_ids,),
            )
            rejection_reason_clusters = {
                r["user_rejection_reason"]: r["cnt"] for r in cur.fetchall()
            }

            # Insight citation rate
            cur.execute(
                """
                SELECT coalesce(avg(access_count), 0) AS avg_access
                FROM memories
                WHERE 'dream-cycle' = ANY(tags)
                """,
            )
            insight_citation_rate = float(cur.fetchone()["avg_access"])

        return {
            "user_rejection_rate": user_rejection_rate,
            "rejection_reason_clusters": rejection_reason_clusters,
            "insight_citation_rate": insight_citation_rate,
        }


def get_evaluator_verdicts_for_run(run_id: str) -> dict[str, dict]:
    """Get evaluator verdicts for accepted candidates in a run.

    Extracted from generate_digest() inline SQL.

    Args:
        run_id: The dream cycle run UUID.

    Returns:
        Dict keyed by candidate title, each value a dict with keys
        'skeptic', 'advocate', 'epistemologist', 'methodologist', each containing
        {"verdict": str, "reasoning": str}.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT candidate_json,
                       evaluator_a_verdict, evaluator_a_reasoning,
                       evaluator_b_verdict, evaluator_b_reasoning,
                       evaluator_c_verdict, evaluator_c_reasoning,
                       evaluator_d_verdict, evaluator_d_reasoning
                FROM dream_cycle_candidates
                WHERE run_id = %s AND final_verdict = 'ACCEPTED'
                """,
                (run_id,),
            )
            verdicts_by_title = {}
            for row in cur.fetchall():
                cj = row[0] or {}
                if isinstance(cj, str):
                    try:
                        cj = json.loads(cj)
                    except (json.JSONDecodeError, ValueError):
                        cj = {}
                title = cj.get("title", "")
                verdicts_by_title[title] = {
                    "skeptic": {"verdict": row[1], "reasoning": row[2]},
                    "advocate": {"verdict": row[3], "reasoning": row[4]},
                    "epistemologist": {"verdict": row[5], "reasoning": row[6]},
                    "methodologist": {"verdict": row[7], "reasoning": row[8]},
                }
            return verdicts_by_title


def was_feedback_injected(run_id: str) -> bool:
    """Check if feedback was injected for a given run.

    Extracted from generate_digest() inline SQL.

    Args:
        run_id: The dream cycle run UUID.

    Returns:
        True if explorer_feedback_injected is non-empty for this run.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT explorer_feedback_injected FROM dream_cycle_runs WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            return bool(row and row[0])


def get_previous_run_id(run_type: str) -> str | None:
    """Find the most recent completed run of the same type.

    Extracted from DreamCycleOrchestrator._get_previous_run_id().

    Args:
        run_type: One of 'scheduled', 'post_learn', 'session_start', 'user_triggered'.

    Returns:
        The run UUID as string, or None if no previous completed run exists.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM dream_cycle_runs
                WHERE run_type = %s
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (run_type,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None


def get_strategy_usage(n_cycles: int = 10) -> dict[str, int]:
    """Count strategy usage across the last N completed dream cycles.

    Extracts 'strategy_that_found_it' from candidate_json JSONB for all
    candidates in recent completed runs. Returns dict of strategy_name -> count.

    Used for UCB1 exploration bonus (Auer et al. 2002) to pressure the
    Explorer toward underused strategies.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT candidate_json->>'strategy_that_found_it' AS strategy,
                       count(*) AS cnt
                FROM dream_cycle_candidates c
                JOIN dream_cycle_runs r ON r.id = c.run_id
                WHERE r.completed_at IS NOT NULL
                  AND candidate_json->>'strategy_that_found_it' IS NOT NULL
                  AND r.id IN (
                      SELECT id FROM dream_cycle_runs
                      WHERE completed_at IS NOT NULL
                      ORDER BY completed_at DESC
                      LIMIT %s
                  )
                GROUP BY candidate_json->>'strategy_that_found_it'
                ORDER BY cnt DESC
                """,
                (n_cycles,),
            )
            return {row["strategy"]: row["cnt"] for row in cur.fetchall()}


def expire_stale_relationships(days_threshold: int = 90) -> int:
    """Set expired_at on relationships where neither endpoint was accessed recently.

    A relationship is considered stale if both source and target memories have
    last_accessed_at older than days_threshold (or NULL) and the relationship
    itself has no expired_at set yet.

    Returns the number of relationships expired.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memory_relationships mr
                SET expired_at = now()
                WHERE mr.expired_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM memories m
                      WHERE m.id IN (mr.source_id, mr.target_id)
                        AND m.last_accessed_at > now() - make_interval(days => %s)
                  )
                """,
                (days_threshold,),
            )
            count = cur.rowcount
        conn.commit()
        return count
