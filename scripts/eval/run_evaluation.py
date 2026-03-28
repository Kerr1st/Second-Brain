#!/usr/bin/env python3
"""Unified evaluation runner — single entry point for all retrieval benchmarks.

Imports tier functions in-process to share the embedding cache across tiers.
Trends is excluded from the default sequence (it reads past results, not the DB)
and is available via --tier trends only.

Usage:
    python scripts/eval/run_evaluation.py                    # all tiers
    python scripts/eval/run_evaluation.py --tier curated     # single tier
    python scripts/eval/run_evaluation.py --dry-run          # preview only
    python scripts/eval/run_evaluation.py --limit 5          # limit queries per tier
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval.eval_common import write_results

logger = logging.getLogger(__name__)

# Default execution order (trends excluded — it reads past results, not DB)
DEFAULT_TIERS = ["golden", "curated", "cold_warm", "ablation", "consolidation"]
ALL_TIERS = DEFAULT_TIERS + ["trends"]


def _run_golden(limit):
    from scripts.eval.golden_queries import evaluate_golden_queries, print_summary
    summary = evaluate_golden_queries(limit=limit)
    print_summary(summary)
    return summary


def _run_curated(limit):
    from scripts.eval.eval_curated import run_curated_eval
    return run_curated_eval(limit=limit)


def _run_cold_warm(limit):
    from scripts.eval.eval_cold_warm import run_cold_warm_eval
    return run_cold_warm_eval(limit=limit)


def _run_ablation(limit):
    from scripts.eval.eval_ablation import run_ablation_eval
    return run_ablation_eval(limit=limit)


def _run_consolidation(limit):
    from scripts.eval.eval_consolidation import run_consolidation_eval
    return run_consolidation_eval(limit=limit)


def _run_trends(_limit):
    from scripts.eval.eval_trends import run_trends
    # Run trends for all types
    for t in ["curated", "cold_warm", "ablation", "consolidation"]:
        run_trends(t)
    return {}


TIER_RUNNERS = {
    "golden": _run_golden,
    "curated": _run_curated,
    "cold_warm": _run_cold_warm,
    "ablation": _run_ablation,
    "consolidation": _run_consolidation,
    "trends": _run_trends,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified evaluation runner.")
    parser.add_argument("--tier", choices=ALL_TIERS, default=None, help="Run a single tier.")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would execute.")
    parser.add_argument("--limit", type=int, default=None, help="Max queries per tier.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")

    tiers = [args.tier] if args.tier else DEFAULT_TIERS

    if args.dry_run:
        print("Dry run — would execute these tiers:")
        for t in tiers:
            limit_str = f" (limit={args.limit})" if args.limit else ""
            print(f"  • {t}{limit_str}")
        return 0

    all_results = {}
    for tier in tiers:
        print(f"\n{'=' * 60}")
        print(f"  Tier: {tier}")
        print(f"{'=' * 60}")
        start = time.time()
        try:
            all_results[tier] = TIER_RUNNERS[tier](args.limit)
        except Exception as exc:
            logger.error("Tier %s failed: %s", tier, exc)
            all_results[tier] = {"error": str(exc)}
        elapsed = time.time() - start
        print(f"  [{tier} completed in {elapsed:.1f}s]")

    # Write consolidated report (only if running multiple tiers)
    if len(tiers) > 1:
        write_results("full_eval", all_results, [])

    print(f"\n{'=' * 60}")
    print("  Evaluation complete.")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
