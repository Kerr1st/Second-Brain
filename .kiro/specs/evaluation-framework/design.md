# Design Document: Evaluation Framework

## Overview

A multi-tier evaluation system for the Second Brain's retrieval pipeline. Measures retrieval accuracy, quantifies the value of V2 reranking signals, validates dream-cycle consolidation quality, and tracks trends over time.

All evaluation scripts are read-only against the database — they query memories and run search but never mutate data (no `INSERT`, `UPDATE`, or `DELETE` on `memories`). The only write path is to the `evaluations/` directory on the filesystem.

**Why read-only:** Eval scripts call `hybrid_search()` + `rerank()` from `src/search.py` directly. These functions only execute SELECT queries. The mutation path (`increment_access_count()`) is only called by the MCP server's `memory_search` tool in `src/mcp_server.py`, which eval scripts bypass. If eval scripts are ever refactored to call through the MCP tool layer instead of the search functions directly, this read-only guarantee would break — `increment_access_count()` would inflate `access_count` and `last_accessed_at` on every evaluated memory, corrupting the spacing bonus signal.

## Architecture

```
scripts/
├── run_evaluation.py          # Unified runner (Req 6)
├── golden_queries.py          # Existing Tier 3 (unchanged)
├── eval_curated.py            # Curated query benchmark (Req 1)
├── eval_cold_warm.py          # Cold vs warm comparison (Req 2)
├── eval_ablation.py           # Signal ablation testing (Req 3)
├── eval_consolidation.py      # Consolidation quality (Req 4)
└── eval_trends.py             # Longitudinal trend analysis (Req 5)

evaluations/
├── query_sets/
│   └── seed.json              # Seed curated query set (Req 1.6)
└── results/
    ├── curated_YYYYMMDD_HHMMSS.json
    ├── cold_warm_YYYYMMDD_HHMMSS.json
    ├── ablation_YYYYMMDD_HHMMSS.json
    ├── consolidation_YYYYMMDD_HHMMSS.json
    └── full_eval_YYYYMMDD_HHMMSS.json
```

## Key Design Decisions

### 1. Rerank override via wrapper function, not monkey-patching

The cold/warm and ablation tests need to modify rerank behavior without changing `src/search.py`. The approach: a thin wrapper function that takes the `rerank()` output and re-computes scores with modified weights. This avoids:
- Monkey-patching `rerank()` (fragile, hard to reason about)
- Adding evaluation-specific parameters to `rerank()` (pollutes production code)
- Duplicating the rerank formula (drift risk)

The wrapper calls `rerank()` normally (to get all intermediate values), then re-scores using the ablation config:

```python
def rerank_with_overrides(results, query_text, query_project=None, overrides=None):
    """Run production rerank(), then re-score with overridden weights.

    overrides: dict of signal_name -> weight_override. Signals not in
    overrides keep their production values. Example:
        {"mem_class_boost": 0.0, "depth_weight": 0.0}  # cold mode
    """
```

This requires `rerank()` to expose intermediate signal values on each result dict. Currently `rerank()` only sets `rerank_score`. The design adds intermediate values (`_overlap`, `_title_overlap`, `_recency`, `_length_score`, `_depth_score`, `_type_boost`, `_mem_class_boost`, `_reinforcement`, `_spacing_bonus`, `_project_penalty`) as underscore-prefixed keys on each result dict. This is a minimal, backward-compatible change to `rerank()` — existing consumers ignore unknown keys. Note: `rrf_score` (without underscore) is already set by `hybrid_search()` before `rerank()` runs, so it does not need to be re-stored.

### 2. Embedding caching across ablation conditions

Ablation testing runs the same queries 7 times (baseline + 6 ablation conditions). Generating embeddings via Bedrock for each run would be wasteful. The design:
- Generate embeddings once per unique query string
- Cache in a `dict[str, list[float]]` keyed by query text
- Pass cached embeddings to `hybrid_search()` directly
- This is already how `golden_queries.py` works (generates embedding, passes to `hybrid_search`)

### 3. Curated query set format

JSON array, one file per query set. Categories enable per-category metric breakdowns:

```json
[
  {
    "query": "What is the spacing effect in memory research?",
    "expected_memory_id": "uuid-here",
    "category": "conceptual",
    "notes": "Tests retrieval of Bjork 1975 research grounding"
  }
]
```

Categories: `factual` (specific facts), `conceptual` (principles/theories), `procedural` (how-to), `cross_project` (queries that should retrieve from a specific project), `recent` (recently added memories), `deep` (memories with high depth_score).

The seed query set (Req 1.6) must be authored by examining actual memories in the database. This is a manual step — the developer runs queries, picks representative memories, and writes the JSON. The spec provides the schema; the content requires human judgment.

### 4. Consolidation quality measurement

For each accepted dream-cycle insight with `source_memories` in `candidate_json`:
1. Get the insight's golden queries (from "Questions this answers:")
2. For each query, run `hybrid_search()` + `rerank()`
3. Record rank and score of the insight AND each source memory
4. Compare: does the insight outrank its sources?

This directly tests the dream cycle's value proposition: consolidated insights should be more retrievable than the raw episodic memories they distill.

**Data path note:** Source memory UUIDs exist in two places: (a) `dream_cycle_candidates.candidate_json.source_memories` (the Thinker's declared sources) and (b) `memories.metadata.source_memories` (copied onto the created memory at acceptance time). The consolidation script uses path (a) because it starts from candidates and joins to their created memories — this is the authoritative source since it's the Thinker's original declaration before any post-processing.

### 5. Result file format

Every evaluation output includes a `metadata` section for longitudinal tracking:

```json
{
  "metadata": {
    "timestamp": "2025-03-28T22:00:00Z",
    "eval_type": "curated",
    "corpus_size": 74523,
    "script_version": "1.0.0",
    "git_commit": "abc1234"
  },
  "summary": { ... },
  "results": [ ... ]
}
```

### 6. No database schema changes

The evaluation framework is purely additive — scripts + data files. No migrations, no new tables, no changes to `src/db.py` or `src/search.py` beyond the intermediate signal exposure in `rerank()`.

Exception: `rerank()` gets underscore-prefixed intermediate values added to each result dict. This is the only production code change.

### 7. Trends excluded from default execution order

The unified runner's default sequence is: golden → curated → cold_warm → ablation → consolidation. Trends analysis (`eval_trends.py`) is deliberately excluded from this sequence because it reads past result files from `evaluations/results/`, not the database. Running it immediately after the other tiers would only analyze the results just written moments ago — not useful for longitudinal tracking. It's available via `--tier trends` for on-demand retrospective analysis.

### 8. Unified runner imports tier functions in-process (no subprocesses)

The unified runner (`run_evaluation.py`) imports each tier's main function directly (e.g., `from scripts.eval_curated import run_curated_eval`) rather than spawning separate Python processes. This is critical for embedding cache sharing: `generate_and_cache_embeddings()` in `eval_common.py` maintains an in-memory `dict[str, list[float]]` that persists across all tiers within a single runner invocation. Without in-process execution, each tier would regenerate embeddings independently, multiplying Bedrock API costs by the number of tiers. Individual tier scripts remain independently runnable via CLI for single-tier use.

## Data Flow

```
                    ┌─────────────────┐
                    │ run_evaluation.py│
                    │  (unified runner)│
                    └────────┬────────┘
                             │
            ┌────────────────┼────────────────┐
            │                │                │
    ┌───────▼──────┐ ┌──────▼───────┐ ┌──────▼───────┐
    │golden_queries│ │eval_curated  │ │eval_cold_warm│  ...
    │  (existing)  │ │              │ │              │
    └───────┬──────┘ └──────┬───────┘ └──────┬───────┘
            │                │                │
            ▼                ▼                ▼
    ┌─────────────────────────────────────────────┐
    │           src/search.py                      │
    │  hybrid_search() → rerank()                  │
    │  (read-only queries against PostgreSQL)       │
    └──────────────────┬──────────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────────┐
    │           evaluations/results/                │
    │  Timestamped JSON files                       │
    └─────────────────────────────────────────────┘
```

## Shared Utilities

A shared module `scripts/eval_common.py` provides:

```python
def load_query_sets(directory="evaluations/query_sets") -> list[dict]:
    """Load and validate all curated query set JSON files."""

def get_golden_queries_as_eval_entries() -> list[dict]:
    """Convert golden queries to the same format as curated entries."""

def generate_and_cache_embeddings(queries: list[str]) -> dict[str, list[float]]:
    """Generate embeddings for unique queries, caching duplicates."""

def run_single_query(query_text, embedding, query_project=None, rerank_fn=None) -> dict:
    """Run hybrid_search + rerank for a single query. Returns result with rank info."""

def compute_metrics(results: list[dict], k_values=(1, 3, 5, 10)) -> dict:
    """Compute MRR and Hit@k from a list of query results."""

def write_results(eval_type: str, summary: dict, results: list, metadata: dict = None):
    """Write timestamped JSON to evaluations/results/."""

def get_eval_metadata() -> dict:
    """Build metadata dict: timestamp, corpus_size, git_commit."""
```

## Rerank Override Mechanism

The `rerank_with_overrides()` function in `eval_common.py`:

```python
# Production weights (from src/search.py rerank())
PRODUCTION_WEIGHTS = {
    "rrf": 0.30,
    "overlap": 0.18,
    "title_overlap": 0.18,
    "recency": 0.12,
    "length": 0.08,
    "depth": 0.05,
    "type_boost": 0.06,       # additive, not weighted
    "mem_class_boost": 0.04,  # max value for semantic
    "reinforcement_coeff": 0.03,
    "project_penalty": -0.15,
}

# Cold mode: neutralize V2 signals, preserve V1 reinforcement
# spacing_bonus forced to 1.0 means reinforcement = 0.03 * log1p(access_count) * 1.0
# (V1 behavior: uniform reinforcement without spacing modulation)
COLD_OVERRIDES = {
    "depth": 0.0,
    "mem_class_boost": 0.0,
    "spacing_bonus": 1.0,     # constant — removes spacing modulation, keeps reinforcement
    "project_penalty": 0.0,
}

# Ablation conditions: one signal disabled per condition
# Note: minus_spacing and minus_reinforcement are DIFFERENT:
#   minus_spacing: forces spacing_bonus=1.0 (reinforcement applies uniformly)
#   minus_reinforcement: zeros the 0.03 coefficient (kills entire reinforcement term)
ABLATION_CONDITIONS = {
    "minus_mem_class":      {"mem_class_boost": 0.0},
    "minus_depth":          {"depth": 0.0},
    "minus_spacing":        {"spacing_bonus": 1.0},          # constant, reinforcement still applies
    "minus_project":        {"project_penalty": 0.0},
    "minus_type_boost":     {"type_boost": 0.0},
    "minus_reinforcement":  {"reinforcement_coeff": 0.0},    # kills entire reinforcement term
}
```

The wrapper reads the underscore-prefixed intermediate values from each result and recomputes `rerank_score` using the overridden weights. This means `hybrid_search()` results (RRF fusion) are identical across all conditions — only the reranking changes.

## Interaction with Existing Code

| Module | Change | Reason |
|--------|--------|--------|
| `src/search.py` `rerank()` | Add underscore-prefixed intermediate signal values to each result dict | Enables ablation re-scoring without duplicating formula |
| `scripts/golden_queries.py` | No changes | Continues to work as-is; unified runner calls it |
| `src/dream_cycle_db.py` | No changes | `get_golden_queries()` and `get_tier1/2_metrics()` used read-only |
| `src/db.py` | No changes | `get_connection()` used read-only by eval scripts |
| `src/embeddings.py` | No changes | `generate_embedding()` called with caching layer |
