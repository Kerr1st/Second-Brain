#!/usr/bin/env python3
"""Weight optimization — find optimal reranking weights via Bayesian optimization.

Uses scipy.optimize.minimize (Nelder-Mead) to search the weight space,
evaluating each candidate weight vector against golden queries + curated
query sets using negative MRR as the objective function.

The optimizer respects the structure of the existing rerank formula:
weighted signals (rrf, overlap, title_overlap, recency, length, depth)
are coefficients on [0, 1] signals, while type_boost, mem_class_boost,
and project_penalty are multipliers on small additive values.

Usage:
    python scripts/eval/eval_optimize.py                  # full optimization
    python scripts/eval/eval_optimize.py --iterations 50  # quick run
    python scripts/eval/eval_optimize.py --dry-run        # show baseline only
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, differential_evolution

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.eval.eval_common import (
    PRODUCTION_WEIGHTS,
    compute_metrics,
    generate_and_cache_embeddings,
    get_golden_queries_as_eval_entries,
    load_query_sets,
    rerank_with_overrides,
    run_single_query,
    write_results,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weight vector ↔ array conversion
# ---------------------------------------------------------------------------

# The 6 weighted signals that are coefficients on normalized [0,1] signals.
# These are the primary optimization targets.
WEIGHTED_SIGNAL_NAMES = ["rrf", "overlap", "title_overlap", "context_overlap", "recency", "length", "depth"]

# Additive/multiplier signals optimized separately with wider bounds.
ADDITIVE_SIGNAL_NAMES = ["type_boost", "mem_class_boost", "reinforcement_coeff", "project_penalty", "superseded_penalty", "staleness_penalty"]

ALL_SIGNAL_NAMES = WEIGHTED_SIGNAL_NAMES + ADDITIVE_SIGNAL_NAMES


def weights_to_array(weights: dict) -> np.ndarray:
    """Convert a weights dict to a numpy array in canonical order."""
    return np.array([weights[name] for name in ALL_SIGNAL_NAMES])


def array_to_weights(arr: np.ndarray) -> dict:
    """Convert a numpy array back to a weights dict."""
    return {name: float(arr[i]) for i, name in enumerate(ALL_SIGNAL_NAMES)}


# Bounds for each signal during optimization.
# Weighted signals: [0.0, 0.5] — they're coefficients, shouldn't dominate.
# type_boost/mem_class_boost: [0.0, 3.0] — multipliers on small values (0.06, 0.04).
# reinforcement_coeff: [0.0, 0.10] — coefficient on log1p(access) * spacing.
# project_penalty: [0.0, 3.0] — multiplier on -0.15.
BOUNDS = (
    [(0.01, 0.50)] * len(WEIGHTED_SIGNAL_NAMES)  # rrf through depth
    + [(0.0, 3.0)]   # type_boost multiplier
    + [(0.0, 3.0)]   # mem_class_boost multiplier
    + [(0.0, 0.10)]  # reinforcement_coeff
    + [(0.0, 3.0)]   # project_penalty multiplier
    + [(0.0, 3.0)]   # superseded_penalty multiplier
    + [(0.0, 3.0)]   # staleness_penalty multiplier
)


# ---------------------------------------------------------------------------
# Objective function
# ---------------------------------------------------------------------------

_eval_count = 0


def build_objective(entries: list[dict], cached_results: dict[str, list[dict]]):
    """Build the objective function that scores a weight vector.

    Returns a callable f(weight_array) -> negative MRR.
    We minimize negative MRR (equivalent to maximizing MRR).

    Uses cached hybrid_search + rerank results to avoid repeated DB queries.
    Only the re-scoring via rerank_with_overrides is repeated per evaluation.
    """
    def objective(weight_array: np.ndarray) -> float:
        global _eval_count
        _eval_count += 1

        weights = array_to_weights(weight_array)
        query_results = []

        for entry in entries:
            query = entry["query"]
            expected_id = entry["expected_memory_id"]

            # Deep copy cached results (rerank_with_overrides mutates in place)
            results_copy = [dict(r) for r in cached_results[query]]
            reranked = rerank_with_overrides(results_copy, weights)

            # Find rank of expected memory
            rank_position = None
            for i, r in enumerate(reranked):
                if str(r["id"]) == expected_id:
                    rank_position = i + 1
                    break

            query_results.append({
                "rank_position": rank_position,
                "category": entry.get("category"),
            })

        metrics = compute_metrics(query_results)
        neg_mrr = -metrics["mrr"]

        if _eval_count % 25 == 0:
            logger.info(
                "  eval #%d: MRR=%.4f (weights: %s)",
                _eval_count,
                -neg_mrr,
                {k: round(v, 3) for k, v in weights.items()},
            )

        return neg_mrr

    return objective


# ---------------------------------------------------------------------------
# Main optimization routine
# ---------------------------------------------------------------------------

def run_optimization(
    method: str = "differential_evolution",
    max_iterations: int = 200,
    limit: int | None = None,
) -> dict:
    """Run weight optimization and return results.

    Steps:
    1. Load all evaluation entries (golden + curated).
    2. Run hybrid_search + rerank once per query to cache results.
    3. Optimize weights using the cached results.
    4. Report baseline vs optimized MRR.

    Args:
        method: "nelder_mead" or "differential_evolution".
        max_iterations: Max function evaluations.
        limit: Optional limit on number of queries.

    Returns:
        Summary dict with baseline, optimized weights, and metrics.
    """
    global _eval_count
    _eval_count = 0

    # Step 1: Load entries
    entries = get_golden_queries_as_eval_entries() + load_query_sets()
    if not entries:
        logger.error("No evaluation entries found. Need golden queries or curated query sets.")
        return {"error": "No evaluation entries"}
    if limit:
        entries = entries[:limit]
    logger.info("Loaded %d evaluation entries", len(entries))

    # Step 2: Cache hybrid_search + rerank results for all queries
    queries = [e["query"] for e in entries]
    embeddings = generate_and_cache_embeddings(queries)

    logger.info("Running baseline search + rerank for %d queries...", len(entries))
    cached_results: dict[str, list[dict]] = {}
    baseline_results = []

    for entry in entries:
        query = entry["query"]
        expected_id = entry["expected_memory_id"]

        if query not in cached_results:
            result = run_single_query(query, embeddings[query], expected_id)
            cached_results[query] = result["results"]

        # Baseline rank
        rank_position = None
        for i, r in enumerate(cached_results[query]):
            if str(r["id"]) == expected_id:
                rank_position = i + 1
                break
        baseline_results.append({
            "rank_position": rank_position,
            "category": entry.get("category"),
        })

    baseline_metrics = compute_metrics(baseline_results)
    logger.info("Baseline MRR: %.4f", baseline_metrics["mrr"])

    # Step 3: Optimize
    objective = build_objective(entries, cached_results)
    x0 = weights_to_array(PRODUCTION_WEIGHTS)

    logger.info("Starting optimization (method=%s, max_iter=%d)...", method, max_iterations)
    start_time = time.time()

    if method == "nelder_mead":
        result = minimize(
            objective,
            x0,
            method="Nelder-Mead",
            options={
                "maxfev": max_iterations,
                "xatol": 0.005,
                "fatol": 0.0001,
                "adaptive": True,
            },
        )
        best_weights = array_to_weights(result.x)
        best_mrr = -result.fun
        converged = result.success
        n_evals = result.nfev

    elif method == "differential_evolution":
        result = differential_evolution(
            objective,
            bounds=BOUNDS,
            x0=x0,
            maxiter=max_iterations,
            tol=0.0001,
            seed=42,
            polish=True,
            init="sobol",
            workers=1,  # DB connections aren't thread-safe
        )
        best_weights = array_to_weights(result.x)
        best_mrr = -result.fun
        converged = result.success
        n_evals = result.nfev

    else:
        raise ValueError(f"Unknown method: {method}")

    elapsed = time.time() - start_time
    logger.info("Optimization complete in %.1fs (%d evaluations)", elapsed, n_evals)
    logger.info("Optimized MRR: %.4f (baseline: %.4f, delta: %+.4f)",
                best_mrr, baseline_metrics["mrr"], best_mrr - baseline_metrics["mrr"])

    # Step 4: Compute full metrics with optimized weights
    optimized_results = []
    for entry in entries:
        results_copy = [dict(r) for r in cached_results[entry["query"]]]
        reranked = rerank_with_overrides(results_copy, best_weights)
        rank_position = None
        for i, r in enumerate(reranked):
            if str(r["id"]) == entry["expected_memory_id"]:
                rank_position = i + 1
                break
        optimized_results.append({
            "rank_position": rank_position,
            "category": entry.get("category"),
        })

    optimized_metrics = compute_metrics(optimized_results)

    # Round weights for readability
    best_weights_rounded = {k: round(v, 4) for k, v in best_weights.items()}
    production_weights_rounded = {k: round(v, 4) for k, v in PRODUCTION_WEIGHTS.items()}

    summary = {
        "method": method,
        "max_iterations": max_iterations,
        "n_evaluations": n_evals,
        "converged": converged,
        "elapsed_seconds": round(elapsed, 1),
        "n_queries": len(entries),
        "baseline": {
            "weights": production_weights_rounded,
            "metrics": baseline_metrics,
        },
        "optimized": {
            "weights": best_weights_rounded,
            "metrics": optimized_metrics,
        },
        "improvement": {
            "mrr_delta": round(best_mrr - baseline_metrics["mrr"], 4),
            "mrr_pct_change": round(
                (best_mrr - baseline_metrics["mrr"]) / max(baseline_metrics["mrr"], 0.0001) * 100, 1
            ),
        },
        "weight_deltas": {
            k: round(best_weights[k] - PRODUCTION_WEIGHTS[k], 4)
            for k in ALL_SIGNAL_NAMES
        },
    }

    write_results("optimization", summary, [])
    return summary


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def print_summary(summary: dict) -> None:
    """Print optimization results in a human-readable format."""
    if "error" in summary:
        print(f"Error: {summary['error']}")
        return

    print(f"\n{'=' * 70}")
    print("  Weight Optimization Results")
    print(f"{'=' * 70}\n")

    print(f"  Method:       {summary['method']}")
    print(f"  Evaluations:  {summary['n_evaluations']}")
    print(f"  Converged:    {summary['converged']}")
    print(f"  Elapsed:      {summary['elapsed_seconds']}s")
    print(f"  Queries:      {summary['n_queries']}")

    print(f"\n{'─' * 70}")
    print("  Metrics Comparison")
    print(f"{'─' * 70}")
    bl = summary["baseline"]["metrics"]
    op = summary["optimized"]["metrics"]
    print(f"  {'Metric':<20} {'Baseline':>10} {'Optimized':>10} {'Delta':>10}")
    print(f"  {'─' * 50}")
    print(f"  {'MRR':<20} {bl['mrr']:>10.4f} {op['mrr']:>10.4f} {summary['improvement']['mrr_delta']:>+10.4f}")
    for k in (1, 3, 5, 10):
        key = f"hit_at_{k}"
        delta = op.get(key, 0) - bl.get(key, 0)
        print(f"  {f'Hit@{k}':<20} {bl.get(key, 0):>10.4f} {op.get(key, 0):>10.4f} {delta:>+10.4f}")

    print(f"\n{'─' * 70}")
    print("  Weight Comparison")
    print(f"{'─' * 70}")
    print(f"  {'Signal':<25} {'Production':>12} {'Optimized':>12} {'Delta':>10}")
    print(f"  {'─' * 60}")
    for name in ALL_SIGNAL_NAMES:
        prod = summary["baseline"]["weights"][name]
        opt = summary["optimized"]["weights"][name]
        delta = summary["weight_deltas"][name]
        marker = " ◀" if abs(delta) > 0.02 else ""
        print(f"  {name:<25} {prod:>12.4f} {opt:>12.4f} {delta:>+10.4f}{marker}")

    print(f"\n{'─' * 70}")
    print("  Code snippet (paste into src/search.py rerank()):")
    print(f"{'─' * 70}")
    w = summary["optimized"]["weights"]
    print(f"""
        r["rerank_score"] = (
            {w['rrf']:.4f} * r.get("rrf_score", 0)
            + {w['overlap']:.4f} * overlap
            + {w['title_overlap']:.4f} * title_overlap
            + {w['recency']:.4f} * recency
            + {w['length']:.4f} * length_score
            + {w['depth']:.4f} * depth_score
            + type_boost
            + mem_class_boost
            + reinforcement
            + project_penalty
        )""")
    print(f"\n  Note: type_boost multiplier={w['type_boost']:.4f}, "
          f"mem_class_boost multiplier={w['mem_class_boost']:.4f}, "
          f"reinforcement_coeff={w['reinforcement_coeff']:.4f}, "
          f"project_penalty multiplier={w['project_penalty']:.4f}")
    print(f"\n{'=' * 70}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Optimize reranking weights via Bayesian optimization.")
    parser.add_argument("--method", choices=["nelder_mead", "differential_evolution"],
                        default="differential_evolution",
                        help="Optimization method (default: differential_evolution)")
    parser.add_argument("--iterations", type=int, default=200,
                        help="Max iterations/evaluations (default: 200)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max queries to use (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show baseline metrics only, don't optimize")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.dry_run:
        entries = get_golden_queries_as_eval_entries() + load_query_sets()
        print(f"Would optimize over {len(entries)} queries using {args.method}")
        print(f"Current production weights: {PRODUCTION_WEIGHTS}")
        return 0

    summary = run_optimization(
        method=args.method,
        max_iterations=args.iterations,
        limit=args.limit,
    )
    print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
