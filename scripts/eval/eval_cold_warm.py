#!/usr/bin/env python3
"""Cold vs warm retrieval comparison.

Runs each query twice — with production V2 signals ("warm") and with V2
signals neutralized ("cold") — to quantify the value added by mem_class,
depth_score, spacing_bonus, and project penalty.

Usage:
    python scripts/eval/eval_cold_warm.py
    python scripts/eval/eval_cold_warm.py --limit 10
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval.eval_common import (
    COLD_OVERRIDES,
    compute_metrics,
    generate_and_cache_embeddings,
    get_golden_queries_as_eval_entries,
    load_query_sets,
    rerank_with_overrides,
    run_single_query,
    write_results,
)

logger = logging.getLogger(__name__)


def _find_rank(results: list[dict], memory_id: str) -> int | None:
    for i, r in enumerate(results):
        if str(r["id"]) == memory_id:
            return i + 1
    return None


def run_cold_warm_eval(limit: int | None = None) -> dict:
    """Run cold vs warm comparison. Returns summary dict."""
    entries = get_golden_queries_as_eval_entries() + load_query_sets()
    if not entries:
        logger.info("No queries found.")
        return {}
    if limit:
        entries = entries[:limit]

    queries = [e["query"] for e in entries]
    embeddings = generate_and_cache_embeddings(queries)

    warm_results = []
    cold_results = []
    per_query = []

    for entry in entries:
        emb = embeddings[entry["query"]]
        mid = entry["expected_memory_id"]

        # Warm: production rerank
        warm = run_single_query(entry["query"], emb, mid)
        warm_results.append(warm)

        # Cold: re-score with V2 signals neutralized (deep copy to avoid mutating warm results)
        cold_reranked = rerank_with_overrides([dict(r) for r in warm["results"]], COLD_OVERRIDES)
        cold_rank = _find_rank(cold_reranked, mid)
        cold_results.append({"rank_position": cold_rank, "category": entry.get("category")})

        delta = "same"
        if warm["rank_position"] and cold_rank:
            if warm["rank_position"] < cold_rank:
                delta = "improved"
            elif warm["rank_position"] > cold_rank:
                delta = "degraded"
        elif warm["rank_position"] and not cold_rank:
            delta = "improved"
        elif not warm["rank_position"] and cold_rank:
            delta = "degraded"

        per_query.append({
            "query": entry["query"][:80],
            "warm_rank": warm["rank_position"],
            "cold_rank": cold_rank,
            "delta": delta,
        })

    warm_metrics = compute_metrics(warm_results)
    cold_metrics = compute_metrics(cold_results)

    summary = {
        "warm": warm_metrics,
        "cold": cold_metrics,
        "deltas": {
            "mrr": round(warm_metrics["mrr"] - cold_metrics["mrr"], 4),
            **{f"hit_at_{k}": round(warm_metrics.get(f"hit_at_{k}", 0) - cold_metrics.get(f"hit_at_{k}", 0), 4)
               for k in (1, 3, 5, 10)},
        },
        "improved": sum(1 for p in per_query if p["delta"] == "improved"),
        "degraded": sum(1 for p in per_query if p["delta"] == "degraded"),
        "same": sum(1 for p in per_query if p["delta"] == "same"),
    }

    write_results("cold_warm", summary, per_query)

    print(f"\n{'Metric':<15} {'Warm':>8} {'Cold':>8} {'Delta':>8}")
    print("-" * 42)
    print(f"{'MRR':<15} {warm_metrics['mrr']:>8.4f} {cold_metrics['mrr']:>8.4f} {summary['deltas']['mrr']:>+8.4f}")
    for k in (1, 3, 5, 10):
        key = f"hit_at_{k}"
        print(f"{'Hit@' + str(k):<15} {warm_metrics.get(key, 0):>8.1%} {cold_metrics.get(key, 0):>8.1%} {summary['deltas'][key]:>+8.4f}")
    print(f"\nImproved: {summary['improved']}  Degraded: {summary['degraded']}  Same: {summary['same']}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Cold vs warm retrieval comparison.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    run_cold_warm_eval(args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
