# Implementation Plan: Evaluation Framework

## Overview

Multi-tier evaluation system for the Second Brain's retrieval pipeline. Adds curated query benchmarks, cold/warm comparison, signal ablation, consolidation quality metrics, longitudinal tracking, and a unified runner. The only production code change is exposing intermediate signal values in `src/search.py` `rerank()`. Everything else is new scripts and data files.

## Tasks

- [x] 1. Expose intermediate rerank signals and create shared evaluation utilities
  - [x] 1.1 Modify `src/search.py` `rerank()` to store intermediate signal values
    - Add underscore-prefixed keys to each result dict: `_overlap`, `_title_overlap`, `_recency`, `_length_score`, `_depth_score`, `_type_boost`, `_mem_class_boost`, `_reinforcement`, `_spacing_bonus`, `_project_penalty`
    - Note: `rrf_score` (no underscore) is already set by `hybrid_search()` — do not duplicate it
    - These are set during the existing computation — no new calculations, just storing what's already computed
    - Existing `rerank_score` remains unchanged
    - _Requirements: 2.2, 3.2_

  - [x] 1.2 Create `scripts/eval_common.py` with shared evaluation utilities
    - `load_query_sets()`: load and validate JSON files from `evaluations/query_sets/`
    - `get_golden_queries_as_eval_entries()`: convert `get_golden_queries()` output to eval format
    - `generate_and_cache_embeddings()`: generate embeddings once, cache by query text
    - `run_single_query()`: `hybrid_search()` + `rerank()` for one query, return rank info
    - `compute_metrics()`: MRR and Hit@k from result list, with optional category breakdown
    - `rerank_with_overrides()`: re-score results using overridden weights from intermediate values
    - `write_results()`: write timestamped JSON to `evaluations/results/`
    - `get_eval_metadata()`: build metadata dict (timestamp, corpus_size, git_commit)
    - Define `PRODUCTION_WEIGHTS`, `COLD_OVERRIDES` (spacing_bonus=1.0 constant, not reinforcement zeroed), `ABLATION_CONDITIONS` (minus_spacing forces spacing_bonus=1.0; minus_reinforcement zeros 0.03 coeff — these are distinct conditions)
    - _Requirements: 1.4, 1.5, 2.1, 2.2, 3.2, 3.5, 5.1, 5.5_

  - [x] 1.3 Create `evaluations/` directory structure
    - `evaluations/query_sets/` — for curated query set JSON files
    - `evaluations/results/` — for timestamped evaluation output
    - Add `evaluations/results/` to `.gitignore` (results are local, query sets are committed)
    - _Requirements: 1.2, 5.1_

- [x] 2. Curated query benchmark
  - [x] 2.1 Create `evaluations/query_sets/seed.json` template with schema and placeholder entries
    - JSON array with at least 5 entries per category: `factual`, `conceptual`, `procedural`, `cross_project`, `recent`, `deep`
    - Each entry: `query`, `expected_memory_id` (placeholder UUID `00000000-0000-0000-0000-000000000000`), `category`, `notes` explaining what kind of memory to find
    - Include a comment-style `_instructions` key at the top explaining how to populate real IDs
    - _Requirements: 1.1, 1.6_

  - [ ] 2.2 **MANUAL STEP**: Populate `seed.json` with real memory IDs from the production database
    - Query the production database to find representative memories for each category
    - Replace placeholder UUIDs with real memory IDs
    - Verify each query actually retrieves its expected memory (sanity check)
    - This task cannot be automated — it requires human judgment about which memories are good test cases
    - _Requirements: 1.6_

  - [x] 2.3 Create `scripts/eval_curated.py`
    - Load query sets via `load_query_sets()`
    - Validate `expected_memory_id` exists in DB (warn on missing, don't fail)
    - Run each query via `run_single_query()`
    - Compute aggregate + per-category metrics via `compute_metrics()`
    - Write results via `write_results("curated", ...)`
    - CLI: `python scripts/eval_curated.py [--query-set PATH] [--limit N]`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 3. Cold vs warm comparison
  - [x] 3.1 Create `scripts/eval_cold_warm.py`
    - Load all queries (golden + curated)
    - Generate and cache embeddings once
    - Run each query twice: warm (production `rerank()`) and cold (`rerank_with_overrides(COLD_OVERRIDES)`)
    - Compute metrics for both conditions
    - Output side-by-side summary: warm vs cold MRR, Hit@k, deltas
    - Output per-query detail: improved/degraded/unchanged
    - Write results via `write_results("cold_warm", ...)`
    - CLI: `python scripts/eval_cold_warm.py [--limit N]`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 4. Signal ablation testing
  - [x] 4.1 Create `scripts/eval_ablation.py`
    - Load all queries (golden + curated)
    - Generate and cache embeddings once (reuse across all 7 conditions)
    - Run baseline (all signals) + 6 ablation conditions (one signal zeroed each)
    - minus_spacing: force spacing_bonus=1.0 (reinforcement still applies uniformly)
    - minus_reinforcement: zero the 0.03 coefficient (kills entire reinforcement term)
    - For each condition, use `rerank_with_overrides()` with the appropriate override dict
    - Compute MRR for each condition
    - Output summary table: signal, MRR with, MRR without, delta, impact rank
    - Write results via `write_results("ablation", ...)`
    - CLI: `python scripts/eval_ablation.py [--limit N]`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 5. Property-based tests for evaluation utilities
  - [x] 5.1 Create `tests/test_eval_properties.py` with Hypothesis property tests
    - **Property 1: compute_metrics MRR bounds** — For any list of rank positions (positive ints or None), MRR is always in [0.0, 1.0]
    - **Property 2: compute_metrics Hit@k monotonicity** — For any result set, Hit@1 ≤ Hit@3 ≤ Hit@5 ≤ Hit@10
    - **Property 3: rerank_with_overrides all-zero produces lower-or-equal scores** — For any results with intermediate values, zeroing all additive signals produces scores ≤ baseline
    - **Property 4: rerank_with_overrides identity** — Passing empty overrides dict produces identical scores to baseline
    - _Requirements: 3.2, 1.5_

- [x] 6. Consolidation quality metrics
  - [x] 6.1 Create `scripts/eval_consolidation.py`
    - Query `dream_cycle_candidates` for accepted insights with `candidate_json->'source_memories'` non-empty
    - For each insight, get its golden queries
    - Run each query and record rank/score of the insight AND each source memory
    - Compute: fraction where insight outranks all sources, average rank improvement, average score improvement
    - Skip insights with no source memories (log warning)
    - Write results via `write_results("consolidation", ...)`
    - CLI: `python scripts/eval_consolidation.py [--limit N]`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 7. Longitudinal tracking
  - [x] 7.1 Create `scripts/eval_trends.py`
    - Read all result files of a given type from `evaluations/results/`
    - Extract aggregate metrics + metadata from each
    - Output chronological summary: date, total queries, MRR, Hit@1/3/5/10, corpus_size
    - Compute deltas between consecutive runs
    - CLI: `python scripts/eval_trends.py --type <curated|cold_warm|ablation|consolidation|golden>`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 8. Unified evaluation runner
  - [x] 8.1 Create `scripts/run_evaluation.py`
    - Import tier functions in-process (not subprocess) to share embedding cache across tiers
    - Orchestrate all tiers in order: golden → curated → cold_warm → ablation → consolidation
    - Trends is excluded from default sequence (reads past results, not DB) — available via `--tier trends` only
    - `--tier <name>` flag to run a single tier
    - `--dry-run` flag to report what would execute without running
    - Consolidated summary to stdout after all tiers complete
    - Write consolidated report via `write_results("full_eval", ...)`
    - CLI: `python scripts/run_evaluation.py [--tier NAME] [--dry-run] [--limit N]`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_
