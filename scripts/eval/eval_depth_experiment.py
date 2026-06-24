#!/usr/bin/env python3
"""Depth weight experiment — test depth_score at higher weights.

Hypothesis: depth is underweighted at 0.05. Craik & Lockhart's levels-of-
processing research suggests depth of encoding is one of the strongest
predictors of retrievability. This experiment tests depth at 0.05 (baseline),
0.08, 0.10, 0.12, and 0.15, measuring MRR and hit rates at each level.

Usage:
    python scripts/eval/eval_depth_experiment.py
    python scripts/eval/eval_depth_experiment.py --limit 10
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.eval.eval_common import (
    compute_metrics,
    generate_and_cache_embeddings,
    get_golden_queries_as_eval_entries,
    load_query_sets,
    rerank_with_overrides,
    run_single_query,
    write_results,
)

logger = logging.getLogger(__name__)

DEPTH_VALUES = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]


def _find_rank(results: list[dict], memory_id: str) -> int | None:
    for i, r in enumerate(results):
        if str(r["id"]) == memory_id:
            return i + 1
    return None


def run_depth_experiment(limit: int | None = None) -> dict:
    """Run depth weight experiment across multiple depth values."""
    entries = get_golden_queries_as_eval_entries() + load_query_sets()
    if not entries:
        logger.info("No evaluation entries found.")
        return {}
    if limit:
        entries = entries[:limit]

    queries = [e["query"] for e in entries]
    embeddings = generate_and_cache_embeddings(queries)

    # Run baseline search + rerank once, cache results
    cached_results: dict[str, list[dict]] = {}
    for entry in entries:
        query = entry["query"]
        if query not in cached_results:
            result = run_single_query(query, embeddings[query], entry["expected_memory_id"])
            cached_results[query] = result["results"]

    # Test each depth value
    condition_metrics = {}
    for depth_val in DEPTH_VALUES:
        results_for_condition = []
        for entry in entries:
            results_copy = [dict(r) for r in cached_results[entry["query"]]]
            reranked = rerank_with_overrides(results_copy, {"depth": depth_val})
            rank = _find_rank(reranked, entry["expected_memory_id"])
            results_for_condition.append({
                "rank_position": rank,
                "category": entry.get("category"),
            })
        metrics = compute_metrics(results_for_condition)
        condition_metrics[f"depth_{depth_val}"] = metrics

    # Find best
    best_key = max(condition_metrics, key=lambda k: condition_metrics[k]["mrr"])
    best_val = float(best_key.split("_")[1])

    summary = {
        "n_queries": len(entries),
        "depth_values_tested": DEPTH_VALUES,
        "conditions": condition_metrics,
        "best_depth": best_val,
        "best_mrr": condition_metrics[best_key]["mrr"],
        "baseline_mrr": condition_metrics["depth_0.05"]["mrr"],
        "improvement": round(
            condition_metrics[best_key]["mrr"] - condition_metrics["depth_0.05"]["mrr"], 4
        ),
    }

    write_results("depth_experiment", summary, [])
    return summary


def print_summary(summary: dict) -> None:
    if not summary:
        print("No results.")
        return

    print(f"\n{'=' * 60}")
    print("  Depth Weight Experiment")
    print(f"{'=' * 60}\n")
    print(f"  Queries: {summary['n_queries']}")
    print(f"  Baseline (depth=0.05) MRR: {summary['baseline_mrr']:.4f}")
    print(f"  Best depth value: {summary['best_depth']}")
    print(f"  Best MRR: {summary['best_mrr']:.4f}")
    print(f"  Improvement: {summary['improvement']:+.4f}")

    print(f"\n{'─' * 60}")
    print(f"  {'Depth Weight':<15} {'MRR':>8} {'Hit@1':>8} {'Hit@3':>8} {'Hit@5':>8} {'Delta':>8}")
    print(f"  {'─' * 55}")
    baseline_mrr = summary["baseline_mrr"]
    for depth_val in summary["depth_values_tested"]:
        key = f"depth_{depth_val}"
        m = summary["conditions"][key]
        delta = m["mrr"] - baseline_mrr
        marker = " ◀ best" if depth_val == summary["best_depth"] else ""
        print(f"  {depth_val:<15.2f} {m['mrr']:>8.4f} {m.get('hit_at_1', 0):>8.4f} "
              f"{m.get('hit_at_3', 0):>8.4f} {m.get('hit_at_5', 0):>8.4f} {delta:>+8.4f}{marker}")

    print(f"\n{'=' * 60}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Depth weight experiment.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    summary = run_depth_experiment(args.limit)
    print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
