"""Shared evaluation utilities for the retrieval quality benchmark suite.

Provides query loading, embedding caching, metric computation, rerank
override mechanism, and result persistence. All eval scripts import from here.

Eval scripts are READ-ONLY against the database — they call hybrid_search()
and rerank() directly (SELECT only). The mutation path (increment_access_count)
is only triggered by the MCP server's memory_search tool, which eval scripts bypass.
"""

import json
import logging
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import get_connection
from src.dream_cycle_db import get_golden_queries
from src.embeddings import generate_embedding
from src.rerank_weights import COEFFICIENTS, REINFORCEMENT_COEFF
from src.search import hybrid_search, rerank

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Production weights — DERIVED from the canonical source src/rerank_weights.py.
#
# The coefficient signals (rrf through depth) and reinforcement_coeff come
# straight from the canonical module, so a weight change there propagates here
# automatically. type_boost, mem_class_boost, project_penalty,
# superseded_penalty, and staleness_penalty are MULTIPLIERS applied to the raw
# additive signal values that rerank() already stored on each result
# (_type_boost, _mem_class_boost, etc.), not the signal values themselves. At
# baseline (1.0) the raw values pass through unchanged; at 0.0 the signal is
# zeroed (used by the ablation/override mechanism below).
# ---------------------------------------------------------------------------
PRODUCTION_WEIGHTS = {
    **COEFFICIENTS,               # rrf, overlap, title_overlap, context_overlap, recency, length, depth
    "type_boost": 1.0,            # multiplier on raw _type_boost (0.0 or 0.06)
    "mem_class_boost": 1.0,       # multiplier on raw _mem_class_boost (0.0/0.02/0.04)
    "reinforcement_coeff": REINFORCEMENT_COEFF,
    "project_penalty": 1.0,       # multiplier on raw _project_penalty (0.0 or -0.15)
    "superseded_penalty": 1.0,    # multiplier on raw _superseded_penalty (0.0 or -0.20)
    "staleness_penalty": 1.0,     # multiplier on raw _staleness_penalty (0.0 to -0.05)
}

# Cold mode: neutralize V2 signals, preserve V1 reinforcement.
# spacing_bonus=1.0 means reinforcement = 0.03 * log1p(access) * 1.0
COLD_OVERRIDES = {
    "depth": 0.0,
    "mem_class_boost": 0.0,
    "spacing_bonus": 1.0,
    "project_penalty": 0.0,
}

# Ablation conditions — one signal disabled per condition.
# minus_spacing and minus_reinforcement are DIFFERENT:
#   minus_spacing: forces spacing_bonus=1.0 (reinforcement applies uniformly)
#   minus_reinforcement: zeros the 0.03 coefficient (kills entire term)
ABLATION_CONDITIONS = {
    "minus_mem_class":      {"mem_class_boost": 0.0},
    "minus_depth":          {"depth": 0.0},
    "minus_spacing":        {"spacing_bonus": 1.0},
    "minus_project":        {"project_penalty": 0.0},
    "minus_type_boost":     {"type_boost": 0.0},
    "minus_reinforcement":  {"reinforcement_coeff": 0.0},
    "minus_superseded":     {"superseded_penalty": 0.0},
    "minus_context":        {"context_overlap": 0.0},
    "minus_staleness":      {"staleness_penalty": 0.0},
}

# ---------------------------------------------------------------------------
# Embedding cache (persists across tiers when unified runner imports in-process)
# ---------------------------------------------------------------------------
_embedding_cache: dict[str, list[float]] = {}


def generate_and_cache_embeddings(queries: list[str]) -> dict[str, list[float]]:
    """Generate embeddings for unique query strings, caching duplicates."""
    unique = [q for q in set(queries) if q not in _embedding_cache]
    for q in unique:
        _embedding_cache[q] = generate_embedding(q)
        logger.debug("Cached embedding for: %s", q[:60])
    return {q: _embedding_cache[q] for q in queries}


# ---------------------------------------------------------------------------
# Query loading
# ---------------------------------------------------------------------------

def load_query_sets(directory: str = "evaluations/query_sets") -> list[dict]:
    """Load and validate all curated query set JSON files from *directory*."""
    results = []
    path = Path(directory)
    if not path.exists():
        logger.warning("Query set directory not found: %s", directory)
        return results
    for f in sorted(path.glob("*.json")):
        with open(f) as fh:
            entries = json.load(fh)
        for entry in entries:
            # Validate required fields
            missing = [k for k in ("query", "expected_memory_id", "category") if k not in entry]
            if missing:
                logger.warning("Skipping entry in %s: missing fields %s", f.name, missing)
                continue
            if entry["expected_memory_id"].startswith("00000000"):
                continue  # skip placeholder entries
            results.append(entry)
    return results


def validate_memory_ids(entries: list[dict]) -> list[dict]:
    """Warn about entries whose expected_memory_id is missing from the DB."""
    ids = [e["expected_memory_id"] for e in entries]
    if not ids:
        return entries
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM memories WHERE id = ANY(%s::uuid[])", (ids,))
            found = {row[0] for row in cur.fetchall()}
    for e in entries:
        if e["expected_memory_id"] not in found:
            logger.warning("Memory not found: %s (query: %s)", e["expected_memory_id"], e["query"][:60])
    return entries


def get_golden_queries_as_eval_entries() -> list[dict]:
    """Convert golden queries to the same format as curated entries."""
    entries = []
    for gq in get_golden_queries():
        for query_text in gq["queries"]:
            entries.append({
                "query": query_text,
                "expected_memory_id": gq["memory_id"],
                "category": "golden",
            })
    return entries


# ---------------------------------------------------------------------------
# Single-query evaluation
# ---------------------------------------------------------------------------

def run_single_query(query_text: str, embedding: list[float],
                     expected_memory_id: str, limit: int = 10) -> dict:
    """Run hybrid_search + rerank for one query. Returns result with rank info."""
    search_results = hybrid_search(query_text, embedding, limit=limit)
    reranked = rerank(search_results, query_text)
    rank_position = None
    rerank_score = None
    for i, r in enumerate(reranked):
        if str(r["id"]) == expected_memory_id:
            rank_position = i + 1
            rerank_score = r.get("rerank_score")
            break
    return {
        "query": query_text,
        "expected_memory_id": expected_memory_id,
        "rank_position": rank_position,
        "rerank_score": round(rerank_score, 4) if rerank_score is not None else None,
        "results": reranked,
    }


# ---------------------------------------------------------------------------
# Rerank override mechanism
# ---------------------------------------------------------------------------

def rerank_with_overrides(results: list[dict], overrides: dict) -> list[dict]:
    """Re-score already-reranked results using overridden weights.

    Reads underscore-prefixed intermediate values set by rerank() and
    recomputes rerank_score. Results must have been through rerank() first.
    """
    if not results:
        return results

    w = {**PRODUCTION_WEIGHTS, **overrides}

    for r in results:
        rrf = r.get("rrf_score", 0)
        overlap = r.get("_overlap", 0)
        title_overlap = r.get("_title_overlap", 0)
        context_overlap = r.get("_context_overlap", 0)
        recency = r.get("_recency", 0)
        length_score = r.get("_length_score", 0)
        depth_score = r.get("_depth_score", 0)
        type_boost = r.get("_type_boost", 0)
        mem_class_boost = r.get("_mem_class_boost", 0)
        project_penalty = r.get("_project_penalty", 0)
        superseded_penalty = r.get("_superseded_penalty", 0)
        staleness_penalty = r.get("_staleness_penalty", 0)
        spacing_bonus = r.get("_spacing_bonus", 1.0)
        access_count = r.get("access_count") or 0

        # Override spacing_bonus if specified
        if "spacing_bonus" in overrides:
            spacing_bonus = overrides["spacing_bonus"]

        reinforcement = w["reinforcement_coeff"] * math.log1p(access_count) * spacing_bonus

        r["rerank_score"] = (
            w["rrf"] * rrf
            + w["overlap"] * overlap
            + w["title_overlap"] * title_overlap
            + w["context_overlap"] * context_overlap
            + w["recency"] * recency
            + w["length"] * length_score
            + w["depth"] * depth_score
            + w["type_boost"] * type_boost
            + w["mem_class_boost"] * mem_class_boost
            + reinforcement
            + w["project_penalty"] * project_penalty
            + w["superseded_penalty"] * superseded_penalty
            + w["staleness_penalty"] * staleness_penalty
        )

    results.sort(key=lambda r: r["rerank_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_metrics(results: list[dict], k_values: tuple = (1, 3, 5, 10),
                    _skip_categories: bool = False) -> dict:
    """Compute MRR and Hit@k from a list of query results.

    Each result must have 'rank_position' (int or None).
    Returns aggregate metrics and optional per-category breakdown.
    """
    total = len(results)
    if total == 0:
        return {"total": 0, "mrr": 0.0, **{f"hit_at_{k}": 0.0 for k in k_values}}

    ranks = [r["rank_position"] for r in results if r["rank_position"] is not None]
    mrr = sum(1.0 / r for r in ranks) / total if total > 0 else 0.0

    metrics = {
        "total": total,
        "found": len(ranks),
        "not_found": total - len(ranks),
        "mrr": round(mrr, 4),
    }
    for k in k_values:
        hits = sum(1 for r in ranks if r <= k)
        metrics[f"hit_at_{k}"] = round(hits / total, 4)

    # Per-category breakdown (only at top level)
    if not _skip_categories:
        categories = {r.get("category") for r in results if r.get("category")}
        if categories:
            metrics["by_category"] = {}
            for cat in sorted(categories):
                cat_results = [r for r in results if r.get("category") == cat]
                metrics["by_category"][cat] = compute_metrics(cat_results, k_values, _skip_categories=True)

    return metrics


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------

def get_eval_metadata() -> dict:
    """Build metadata dict: timestamp, corpus_size, git_commit."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories")
            corpus_size = cur.fetchone()[0]

    git_commit = None
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "corpus_size": corpus_size,
        "script_version": "1.0.0",
        "git_commit": git_commit,
    }


def write_results(eval_type: str, summary: dict, results: list,
                  metadata: dict | None = None) -> Path:
    """Write timestamped JSON to evaluations/results/. Returns the file path."""
    if metadata is None:
        metadata = get_eval_metadata()
    metadata["eval_type"] = eval_type

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path("evaluations/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{eval_type}_{ts}.json"

    payload = {"metadata": metadata, "summary": summary, "results": results}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    logger.info("Results written to %s", path)
    return path
