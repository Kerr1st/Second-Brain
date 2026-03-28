#!/usr/bin/env python3
"""Golden Queries — Tier 3 downstream impact metrics.

Extracts "Questions this answers" from accepted dream cycle insights,
runs hybrid_search + rerank for each query, and measures whether the
originating insight appears in the top results.

This benchmarks retrieval quality: if the dream cycle creates an insight
that claims to answer certain questions, those questions should actually
retrieve that insight.

Usage:
    python scripts/eval/golden_queries.py
    python scripts/eval/golden_queries.py --limit 5

Requirements: 18.3
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure imports work when run from the scripts directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dream_cycle_db import get_golden_queries
from src.embeddings import generate_embedding
from src.search import hybrid_search, rerank

logger = logging.getLogger(__name__)


def evaluate_golden_queries(limit: int | None = None) -> dict:
    """Run retrieval quality benchmark on golden queries.

    For each golden query extracted from accepted dream cycle insights:
      1. Generate an embedding
      2. Run hybrid_search (BM25 + vector + RRF)
      3. Rerank results
      4. Find the rank position of the originating insight

    Returns a summary dict with per-query results and aggregate metrics.
    """
    entries = get_golden_queries()
    if not entries:
        logger.info("No golden queries found — no accepted insights with 'Questions this answers'.")
        return {"total_queries": 0, "results": []}

    all_results = []
    ranks = []

    for entry in entries:
        memory_id = entry["memory_id"]
        candidate_id = entry["candidate_id"]
        queries = entry["queries"]

        if limit is not None and len(all_results) >= limit:
            break

        for query_text in queries:
            if limit is not None and len(all_results) >= limit:
                break

            logger.info("Query: %s (memory_id=%s)", query_text, memory_id)

            # Step 1: Generate embedding for the query
            embedding = generate_embedding(query_text)

            # Step 2: Hybrid search
            search_results = hybrid_search(query_text, embedding, limit=10)

            # Step 3: Rerank
            reranked = rerank(search_results, query_text)

            # Step 4: Find rank position of the originating insight
            rank_position = None
            co_results = []
            for i, r in enumerate(reranked):
                rid = str(r["id"])
                if rid == memory_id:
                    rank_position = i + 1  # 1-indexed
                else:
                    co_results.append({
                        "rank": i + 1,
                        "id": rid,
                        "title": r.get("title", ""),
                        "rerank_score": round(r.get("rerank_score", 0), 4),
                    })

            result = {
                "query": query_text,
                "memory_id": memory_id,
                "candidate_id": candidate_id,
                "rank_position": rank_position,  # None if not in top 10
                "rerank_score": None,
                "co_results": co_results[:5],  # top 5 co-results for brevity
            }

            if rank_position is not None:
                # Get the rerank score for the insight
                result["rerank_score"] = round(
                    reranked[rank_position - 1].get("rerank_score", 0), 4
                )
                ranks.append(rank_position)
                logger.info("  → Found at rank %d (score=%.4f)", rank_position, result["rerank_score"])
            else:
                logger.info("  → NOT in top 10 results")

            all_results.append(result)

    # Aggregate metrics
    total = len(all_results)
    found = len(ranks)
    hit_at_1 = sum(1 for r in ranks if r == 1)
    hit_at_3 = sum(1 for r in ranks if r <= 3)
    hit_at_5 = sum(1 for r in ranks if r <= 5)
    hit_at_10 = found  # all found are within top 10
    mrr = sum(1.0 / r for r in ranks) / total if total > 0 else 0.0

    summary = {
        "total_queries": total,
        "found_in_top_10": found,
        "not_found": total - found,
        "hit_rate_at_1": hit_at_1 / total if total > 0 else 0.0,
        "hit_rate_at_3": hit_at_3 / total if total > 0 else 0.0,
        "hit_rate_at_5": hit_at_5 / total if total > 0 else 0.0,
        "hit_rate_at_10": hit_at_10 / total if total > 0 else 0.0,
        "mrr": mrr,
        "results": all_results,
    }

    return summary


def print_summary(summary: dict) -> None:
    """Print a human-readable summary of retrieval quality metrics."""
    total = summary["total_queries"]
    if total == 0:
        print("No golden queries to evaluate.")
        return

    print(f"\n{'=' * 60}")
    print("  Golden Queries — Retrieval Quality Benchmark")
    print(f"{'=' * 60}\n")

    print(f"  Total queries evaluated:  {total}")
    print(f"  Found in top 10:          {summary['found_in_top_10']}")
    print(f"  Not found:                {summary['not_found']}")
    print()
    print(f"  Hit rate @1:   {summary['hit_rate_at_1']:.1%}")
    print(f"  Hit rate @3:   {summary['hit_rate_at_3']:.1%}")
    print(f"  Hit rate @5:   {summary['hit_rate_at_5']:.1%}")
    print(f"  Hit rate @10:  {summary['hit_rate_at_10']:.1%}")
    print(f"  MRR:           {summary['mrr']:.4f}")
    print()

    # Per-query detail
    print(f"{'─' * 60}")
    print("  Per-Query Results")
    print(f"{'─' * 60}")
    for r in summary["results"]:
        rank = r["rank_position"]
        rank_str = f"rank {rank}" if rank is not None else "NOT FOUND"
        score_str = f" (score={r['rerank_score']})" if r["rerank_score"] is not None else ""
        print(f"\n  Q: {r['query']}")
        print(f"     Memory: {r['memory_id']}")
        print(f"     Result: {rank_str}{score_str}")
        if r["co_results"]:
            print("     Co-results:")
            for co in r["co_results"][:3]:
                print(f"       #{co['rank']}: {co['title'][:50]} (score={co['rerank_score']})")

    print(f"\n{'=' * 60}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Tier 3 golden query retrieval benchmark.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of queries to evaluate (default: all).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        summary = evaluate_golden_queries(limit=args.limit)
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 2

    print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
