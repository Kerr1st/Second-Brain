"""Focused Dream Cycle metrics repository."""
import json
from psycopg2.extras import RealDictCursor
from .protocol import Database
from src.dream_cycle.golden_queries import extract_golden_queries
_RUN_STAT_COLUMNS = ("candidates_generated", "candidates_accepted", "candidates_rejected")


def get_golden_queries(*, db: Database) -> list[dict]:
    """Extract 'Questions this answers' from accepted dream cycle insights.

    Queries dream_cycle_candidates where final_verdict = 'ACCEPTED' and
    created_memory_id IS NOT NULL. Parses the candidate_json JSONB to
    extract the content field, then looks for lines starting with
    'Questions this answers:' and extracts the queries.

    Returns list of dicts with: candidate_id, memory_id, queries (list[str]).
    Used for Tier 3 metrics (Requirement 18.3).
    """
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, created_memory_id, candidate_json
                FROM dream_cycle_candidates
                WHERE final_verdict = 'ACCEPTED'
                  AND created_memory_id IS NOT NULL
                """)
            rows = cur.fetchall()
        results = []
        for row in rows:
            candidate_json = row['candidate_json']
            if isinstance(candidate_json, str):
                candidate_json = json.loads(candidate_json)
            content = candidate_json.get('content', '')
            queries = extract_golden_queries(content)
            if queries:
                results.append({'candidate_id': str(row['id']), 'memory_id': str(row['created_memory_id']), 'queries': queries})
        return results

def get_tier1_metrics(n_cycles: int=10, *, db: Database) -> dict:
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
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, candidates_generated, candidates_accepted,
                       candidates_rejected
                FROM dream_cycle_runs
                WHERE completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT %s
                """, (n_cycles,))
            runs = cur.fetchall()
            if not runs:
                return {'acceptance_rate': 0.0, 'acceptance_rate_trend': [], 'non_unanimous_acceptance_rate': 0.0, 'strategy_diversity': 0, 'cost_efficiency': 0.0}
            run_ids = [r['id'] for r in runs]
            total_generated = sum((r['candidates_generated'] or 0 for r in runs))
            total_accepted = sum((r['candidates_accepted'] or 0 for r in runs))
            acceptance_rate = total_accepted / total_generated if total_generated > 0 else 0.0
            acceptance_rate_trend = []
            for r in reversed(runs):
                gen = r['candidates_generated'] or 0
                acc = r['candidates_accepted'] or 0
                acceptance_rate_trend.append(acc / gen if gen > 0 else 0.0)
            cur.execute("""
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
                WHERE run_id = ANY(%s::uuid[])
                """, (run_ids,))
            row = cur.fetchone()
            t_accepted = row['total_accepted']
            non_unanimous = row['non_unanimous']
            non_unanimous_acceptance_rate = non_unanimous / t_accepted if t_accepted > 0 else 0.0
            cur.execute("""
                SELECT count(DISTINCT candidate_json->>'strategy_that_found_it') AS diversity
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s::uuid[])
                  AND candidate_json->>'strategy_that_found_it' IS NOT NULL
                """, (run_ids,))
            strategy_diversity = cur.fetchone()['diversity']
            total_invocations = 0
            for r in runs:
                gen = r['candidates_generated'] or 0
                estimated_slices = max(1, -(-gen // 2))
                explorer_calls = 1
                thinker_calls = estimated_slices
                evaluator_calls = 4 * gen
                total_invocations += explorer_calls + thinker_calls + evaluator_calls
            cost_efficiency = total_accepted / total_invocations if total_invocations > 0 else 0.0
        return {'acceptance_rate': acceptance_rate, 'acceptance_rate_trend': acceptance_rate_trend, 'non_unanimous_acceptance_rate': non_unanimous_acceptance_rate, 'strategy_diversity': strategy_diversity, 'cost_efficiency': cost_efficiency}

def get_tier2_metrics(n_cycles: int=10, *, db: Database) -> dict:
    """Compute Tier 2 engagement metrics.

    Returns dict with:
      - user_rejection_rate: float (count of user_rejected_at IS NOT NULL / total accepted)
      - rejection_reason_clusters: dict of reason -> count (group by user_rejection_reason)
      - insight_citation_rate: float (average access_count on memories tagged 'dream-cycle')

    Requirements: 18.2.
    """
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id
                FROM dream_cycle_runs
                WHERE completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT %s
                """, (n_cycles,))
            run_ids = [r['id'] for r in cur.fetchall()]
            if not run_ids:
                return {'user_rejection_rate': 0.0, 'rejection_reason_clusters': {}, 'insight_citation_rate': 0.0}
            cur.execute("""
                SELECT
                    count(*) FILTER (WHERE final_verdict = 'ACCEPTED') AS total_accepted,
                    count(*) FILTER (WHERE final_verdict = 'ACCEPTED' AND user_rejected_at IS NOT NULL) AS total_rejected
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s::uuid[])
                """, (run_ids,))
            row = cur.fetchone()
            total_accepted = row['total_accepted']
            total_rejected = row['total_rejected']
            user_rejection_rate = total_rejected / total_accepted if total_accepted > 0 else 0.0
            cur.execute("""
                SELECT user_rejection_reason, count(*) AS cnt
                FROM dream_cycle_candidates
                WHERE run_id = ANY(%s::uuid[])
                  AND user_rejected_at IS NOT NULL
                  AND user_rejection_reason IS NOT NULL
                GROUP BY user_rejection_reason
                ORDER BY cnt DESC
                """, (run_ids,))
            rejection_reason_clusters = {r['user_rejection_reason']: r['cnt'] for r in cur.fetchall()}
            cur.execute("""
                SELECT coalesce(avg(access_count), 0) AS avg_access
                FROM memories
                WHERE 'dream-cycle' = ANY(tags)
                """)
            insight_citation_rate = float(cur.fetchone()['avg_access'])
        return {'user_rejection_rate': user_rejection_rate, 'rejection_reason_clusters': rejection_reason_clusters, 'insight_citation_rate': insight_citation_rate}

def get_strategy_usage(n_cycles: int=10, *, db: Database) -> dict[str, int]:
    """Count strategy usage across the last N completed dream cycles.

    Extracts 'strategy_that_found_it' from candidate_json JSONB for all
    candidates in recent completed runs. Returns dict of strategy_name -> count.

    Used for UCB1 exploration bonus (Auer et al. 2002) to pressure the
    Explorer toward underused strategies.
    """
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
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
                """, (n_cycles,))
            return {row['strategy']: row['cnt'] for row in cur.fetchall()}
