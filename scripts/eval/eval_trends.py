#!/usr/bin/env python3
"""Longitudinal trend analysis — track retrieval quality over time.

Reads past evaluation result files and outputs chronological metric summaries.

Usage:
    python scripts/eval/eval_trends.py --type curated
    python scripts/eval/eval_trends.py --type cold_warm
    python scripts/eval/eval_trends.py --type ablation
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

RESULTS_DIR = Path("evaluations/results")


def run_trends(eval_type: str) -> list[dict]:
    """Read all result files of a given type and output chronological summary."""
    if not RESULTS_DIR.exists():
        logger.info("No results directory found.")
        return []

    files = sorted(RESULTS_DIR.glob(f"{eval_type}_*.json"))
    if not files:
        logger.info("No %s result files found.", eval_type)
        return []

    rows = []
    for f in files:
        with open(f) as fh:
            data = json.load(fh)
        meta = data.get("metadata", {})
        summary = data.get("summary", {})
        rows.append({
            "date": meta.get("timestamp", "unknown"),
            "corpus_size": meta.get("corpus_size"),
            "total": summary.get("total", summary.get("total_queries", 0)),
            "mrr": summary.get("mrr", summary.get("warm", {}).get("mrr")),
            "hit_at_1": summary.get("hit_at_1", summary.get("warm", {}).get("hit_at_1")),
            "hit_at_3": summary.get("hit_at_3", summary.get("warm", {}).get("hit_at_3")),
            "hit_at_5": summary.get("hit_at_5", summary.get("warm", {}).get("hit_at_5")),
            "hit_at_10": summary.get("hit_at_10", summary.get("warm", {}).get("hit_at_10")),
            "file": f.name,
        })

    # Print summary
    print(f"\n{'Date':<28} {'Corpus':>7} {'Queries':>8} {'MRR':>8} {'H@1':>7} {'H@3':>7} {'H@10':>7}")
    print("-" * 80)
    prev = None
    for row in rows:
        mrr_str = f"{row['mrr']:.4f}" if row["mrr"] is not None else "   N/A"
        h1 = f"{row['hit_at_1']:.1%}" if row["hit_at_1"] is not None else "  N/A"
        h3 = f"{row['hit_at_3']:.1%}" if row["hit_at_3"] is not None else "  N/A"
        h10 = f"{row['hit_at_10']:.1%}" if row["hit_at_10"] is not None else "  N/A"
        print(f"{row['date']:<28} {row['corpus_size'] or '':>7} {row['total']:>8} {mrr_str:>8} {h1:>7} {h3:>7} {h10:>7}")

        if prev and prev["mrr"] is not None and row["mrr"] is not None:
            delta = row["mrr"] - prev["mrr"]
            print(f"{'':>28} {'':>7} {'':>8} {delta:>+8.4f}")
        prev = row

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Longitudinal trend analysis.")
    parser.add_argument("--type", required=True,
                        choices=["curated", "cold_warm", "ablation", "consolidation", "golden", "full_eval"],
                        help="Evaluation type to analyze.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    run_trends(args.type)
    return 0


if __name__ == "__main__":
    sys.exit(main())
