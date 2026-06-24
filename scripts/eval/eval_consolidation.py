#!/usr/bin/env python3
"""Consolidation quality — do dream-cycle insights outrank their source memories?

For each accepted insight with source_memories in candidate_json, runs the
insight's golden queries and compares the insight's rank against its sources.

Usage:
    python scripts/eval/eval_consolidation.py
    python scripts/eval/eval_consolidation.py --limit 5
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg2.extras import RealDictCursor

from scripts.eval.eval_common import (
    generate_and_cache_embeddings,
    write_results,
)
from src.db import get_connection
from src.dream_cycle_db import extract_golden_queries
from src.search import hybrid_search, rerank

logger = logging.getLogger(__name__)


def _get_insights_with_sources() -> list[dict]:
    """Get accepted insights that have source_memories in candidate_json."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT c.id AS candidate_id, c.created_memory_id, c.candidate_json
                FROM dream_cycle_candidates c
                WHERE c.final_verdict = 'ACCEPTED'
                  AND c.created_memory_id IS NOT NULL
                  AND c.candidate_json IS NOT NULL
            """)
            rows = cur.fetchall()

    results = []
    for row in rows:
        cj = row["candidate_json"]
        if isinstance(cj, str):
            cj = json.loads(cj)
        source_memories = cj.get("source_memories", [])
        if not source_memories:
            continue
        content = cj.get("content", "")
        queries = extract_golden_queries(content)
        if not queries:
            continue
        results.append({
            "candidate_id": str(row["candidate_id"]),
            "memory_id": str(row["created_memory_id"]),
            "source_memory_ids": source_memories,
            "queries": queries,
        })
    return results


def _find_rank(reranked: list[dict], memory_id: str) -> int | None:
    for i, r in enumerate(reranked):
        if str(r["id"]) == memory_id:
            return i + 1
    return None


def _find_score(reranked: list[dict], memory_id: str) -> float | None:
    for r in reranked:
        if str(r["id"]) == memory_id:
            return r.get("rerank_score")
    return None


def run_consolidation_eval(limit: int | None = None) -> dict:
    """Run consolidation quality evaluation. Returns summary dict."""
    insights = _get_insights_with_sources()
    if not insights:
        logger.info("No insights with source memories found.")
        return {"total_insights": 0}

    if limit:
        insights = insights[:limit]

    total_queries = 0
    insight_outranks_all = 0
    rank_improvements = []
    score_improvements = []
    per_insight = []

    for insight in insights:
        mid = insight["memory_id"]
        source_ids = insight["source_memory_ids"]

        # Cache embeddings for all queries in this insight
        embeddings = generate_and_cache_embeddings(insight["queries"])

        for query_text in insight["queries"]:
            total_queries += 1
            embedding = embeddings[query_text]
            reranked = rerank(hybrid_search(query_text, embedding, limit=20), query_text)

            insight_rank = _find_rank(reranked, mid)
            insight_score = _find_score(reranked, mid)

            source_ranks = []
            source_scores = []
            for sid in source_ids:
                sr = _find_rank(reranked, sid)
                ss = _find_score(reranked, sid)
                if sr is not None:
                    source_ranks.append(sr)
                if ss is not None:
                    source_scores.append(ss)

            outranks = (insight_rank is not None and source_ranks
                        and all(insight_rank < sr for sr in source_ranks))
            if outranks:
                insight_outranks_all += 1

            if insight_rank and source_ranks:
                avg_source_rank = sum(source_ranks) / len(source_ranks)
                rank_improvements.append(avg_source_rank - insight_rank)
            if insight_score is not None and source_scores:
                avg_source_score = sum(source_scores) / len(source_scores)
                score_improvements.append(insight_score - avg_source_score)

            per_insight.append({
                "query": query_text[:80],
                "insight_id": mid,
                "insight_rank": insight_rank,
                "source_ranks": source_ranks,
                "outranks_all": outranks,
            })

    summary = {
        "total_insights": len(insights),
        "total_queries": total_queries,
        "insight_outranks_all_fraction": round(insight_outranks_all / max(1, total_queries), 4),
        "avg_rank_improvement": round(sum(rank_improvements) / max(1, len(rank_improvements)), 2),
        "avg_score_improvement": round(sum(score_improvements) / max(1, len(score_improvements)), 4),
    }

    write_results("consolidation", summary, per_insight)

    print(f"\nConsolidation Quality ({summary['total_insights']} insights, {total_queries} queries)")
    print(f"  Insight outranks all sources: {summary['insight_outranks_all_fraction']:.1%}")
    print(f"  Avg rank improvement:         {summary['avg_rank_improvement']:+.1f} positions")
    print(f"  Avg score improvement:        {summary['avg_score_improvement']:+.4f}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidation quality evaluation.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    run_consolidation_eval(args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
