#!/usr/bin/env python3
"""Signal ablation testing — disable one reranking signal at a time.

Measures each V2 signal's contribution to retrieval quality by comparing
baseline MRR against MRR with that signal zeroed.

Usage:
    python scripts/eval/eval_ablation.py
    python scripts/eval/eval_ablation.py --limit 10
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval.eval_common import (
    ABLATION_CONDITIONS,
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


def run_ablation_eval(limit: int | None = None) -> dict:
    """Run ablation testing. Returns summary dict."""
    entries = get_golden_queries_as_eval_entries() + load_query_sets()
    if not entries:
        logger.info("No queries found.")
        return {}
    if limit:
        entries = entries[:limit]

    queries = [e["query"] for e in entries]
    embeddings = generate_and_cache_embeddings(queries)

    # Baseline: run all queries with production rerank
    baseline_results = []
    all_reranked = {}  # cache full reranked lists for re-scoring
    for entry in entries:
        result = run_single_query(entry["query"], embeddings[entry["query"]], entry["expected_memory_id"])
        baseline_results.append(result)
        all_reranked[entry["query"]] = result["results"]

    baseline_metrics = compute_metrics(baseline_results)

    # Ablation conditions
    condition_metrics = {}
    for condition_name, overrides in ABLATION_CONDITIONS.items():
        ablation_results = []
        for entry in entries:
            # Re-score cached results with this condition's overrides
            results_copy = [dict(r) for r in all_reranked[entry["query"]]]
            reranked = rerank_with_overrides(results_copy, overrides)
            rank = _find_rank(reranked, entry["expected_memory_id"])
            ablation_results.append({"rank_position": rank, "category": entry.get("category")})
        condition_metrics[condition_name] = compute_metrics(ablation_results)

    # Build impact table
    impacts = []
    for name, metrics in condition_metrics.items():
        delta = round(baseline_metrics["mrr"] - metrics["mrr"], 4)
        impacts.append({"signal": name, "baseline_mrr": baseline_metrics["mrr"],
                        "ablated_mrr": metrics["mrr"], "delta_mrr": delta})
    impacts.sort(key=lambda x: abs(x["delta_mrr"]), reverse=True)
    for i, imp in enumerate(impacts):
        imp["impact_rank"] = i + 1

    summary = {"baseline": baseline_metrics, "conditions": condition_metrics, "impacts": impacts}
    write_results("ablation", summary, impacts)

    print(f"\n{'Signal':<25} {'Baseline':>10} {'Ablated':>10} {'Delta':>10} {'Rank':>6}")
    print("-" * 65)
    for imp in impacts:
        print(f"{imp['signal']:<25} {imp['baseline_mrr']:>10.4f} {imp['ablated_mrr']:>10.4f} "
              f"{imp['delta_mrr']:>+10.4f} {imp['impact_rank']:>6}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal ablation testing.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    run_ablation_eval(args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
