#!/usr/bin/env python3
"""Curated query benchmark — evaluate retrieval quality on hand-crafted query sets.

Usage:
    python scripts/eval/eval_curated.py
    python scripts/eval/eval_curated.py --limit 10
    python scripts/eval/eval_curated.py --query-set evaluations/query_sets/seed.json
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval.eval_common import (
    compute_metrics,
    generate_and_cache_embeddings,
    load_query_sets,
    run_single_query,
    validate_memory_ids,
    write_results,
)

logger = logging.getLogger(__name__)


def run_curated_eval(query_set_path: str | None = None, limit: int | None = None) -> dict:
    """Run curated query benchmark. Returns summary dict."""
    if query_set_path:
        with open(query_set_path) as fh:
            import json
            all_entries = json.load(fh)
        entries = [e for e in all_entries
                   if not e.get("expected_memory_id", "").startswith("00000000")
                   and "query" in e and "expected_memory_id" in e]
    else:
        entries = load_query_sets()

    if not entries:
        logger.info("No curated queries found.")
        return {"total": 0}

    entries = validate_memory_ids(entries)
    if limit:
        entries = entries[:limit]

    queries = [e["query"] for e in entries]
    embeddings = generate_and_cache_embeddings(queries)

    results = []
    for entry in entries:
        result = run_single_query(
            entry["query"], embeddings[entry["query"]], entry["expected_memory_id"]
        )
        result["category"] = entry.get("category")
        results.append(result)
        rank = result["rank_position"]
        logger.info("  %s → %s", entry["query"][:50], f"rank {rank}" if rank else "NOT FOUND")

    summary = compute_metrics(results)
    write_results("curated", summary, [{k: v for k, v in r.items() if k != "results"} for r in results])

    print(f"\nCurated Benchmark: MRR={summary['mrr']:.4f}  "
          f"Hit@1={summary.get('hit_at_1', 0):.1%}  Hit@3={summary.get('hit_at_3', 0):.1%}  "
          f"Hit@10={summary.get('hit_at_10', 0):.1%}  ({summary['total']} queries)")
    if "by_category" in summary:
        for cat, m in summary["by_category"].items():
            print(f"  {cat:15s}: MRR={m['mrr']:.4f}  Hit@1={m.get('hit_at_1', 0):.1%}  ({m['total']} queries)")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run curated query retrieval benchmark.")
    parser.add_argument("--query-set", type=str, default=None, help="Path to a specific query set JSON.")
    parser.add_argument("--limit", type=int, default=None, help="Max queries to evaluate.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    run_curated_eval(args.query_set, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
