# Requirements Document: Evaluation Framework

## Introduction

The Second Brain has 74K+ memories, a hybrid retrieval pipeline (BM25 + pgvector + RRF + utility reranking), and a weekly dream cycle that autonomously generates insights. The only retrieval quality measurement today is `scripts/golden_queries.py`, which checks whether dream-cycle insights appear in top search results for their declared "Questions this answers" queries. Current baseline: MRR 0.7833, Hit@1 60%, Hit@3 100%.

This is insufficient. There is no way to:
- Benchmark retrieval accuracy across different memory types and query patterns beyond dream-cycle golden queries
- Compare retrieval quality with V2 reranking signals enabled vs disabled ("warm" vs "cold")
- Measure whether dream-cycle insights are actually better than the raw episodic memories they distill
- Determine which individual reranking signals (mem_class, depth_score, spacing_bonus, project penalty) contribute most to retrieval quality
- Track retrieval quality trends over time as the corpus grows and the dream cycle runs

This spec defines a multi-tier evaluation system that addresses each gap. All evaluation scripts live in `scripts/` and output results to `evaluations/`. The system reuses the existing connection pooling (`src/db.py` `get_connection()`) and search pipeline (`src/search.py` `hybrid_search()` + `rerank()`).

## Glossary

- **Golden Query**: A natural-language question extracted from the "Questions this answers:" section of an accepted dream-cycle insight. The insight's `created_memory_id` is the expected retrieval target.
- **Curated Query Set**: A manually authored JSON file mapping query strings to expected memory IDs, covering memory types and query patterns not represented by golden queries.
- **Cold Retrieval**: Running `hybrid_search()` + `rerank()` with V2 signals neutralized: `mem_class` boost = 0, `depth_score` weight = 0, `spacing_bonus` forced to 1.0 (constant, so reinforcement applies uniformly as in V1: `0.03 * log1p(access_count) * 1.0`), and `project` penalty = 0. This preserves V1 reinforcement behavior while removing all V2 additions.
- **Warm Retrieval**: Running `hybrid_search()` + `rerank()` with the production V2 signal weights as defined in `src/search.py`.
- **Ablation Test**: Running retrieval with exactly one V2 signal disabled while keeping all others at production values, to isolate that signal's contribution.
- **MRR (Mean Reciprocal Rank)**: Average of 1/rank for each query where the expected result appears in the top-k. Queries where the expected result is absent contribute 0.
- **Hit@k**: Fraction of queries where the expected result appears in the top k results.
- **Consolidation Quality**: A comparison metric measuring whether a dream-cycle insight retrieves better (higher rank, higher rerank score) than the raw episodic memories it was distilled from, for the same query set.
- **Evaluation Run**: A single execution of an evaluation script, producing a timestamped JSON results file in `evaluations/`.
- **Rerank Formula**: The scoring function in `src/search.py` `rerank()`: `0.30*rrf + 0.18*overlap + 0.18*title_overlap + 0.12*recency + 0.08*length + 0.05*depth + type_boost + mem_class_boost + reinforcement + project_penalty`.

## Requirements

### Requirement 1: Curated Query Set Infrastructure

**User Story:** As a developer, I want to define and maintain hand-crafted query sets with known expected results so that retrieval quality can be measured beyond the auto-generated golden queries from the dream cycle.

#### Acceptance Criteria

1. WHEN a curated query set file is loaded, THE file SHALL be a JSON array where each entry contains: `query` (string), `expected_memory_id` (UUID string), `category` (string — one of: `factual`, `conceptual`, `procedural`, `cross_project`, `recent`, `deep`), and an optional `notes` field.
2. WHEN the evaluation script is run, THE script SHALL load curated query sets from `evaluations/query_sets/*.json`.
3. WHEN a curated query set is loaded, THE script SHALL validate that every `expected_memory_id` exists in the database and report any missing IDs as warnings (not failures).
4. WHEN the evaluation script processes a curated query, THE script SHALL generate an embedding via `src/embeddings.py`, run `hybrid_search()`, run `rerank()`, and record the rank position of the expected memory in the results.
5. WHEN the evaluation script completes, THE script SHALL compute MRR and Hit@1/3/5/10 broken down by `category`, in addition to aggregate metrics.
6. THE project SHALL include a seed curated query set (`evaluations/query_sets/seed.json`) with at least 5 entries per category, authored by examining actual memories in the database.

### Requirement 2: Cold vs Warm Retrieval Comparison

**User Story:** As a developer, I want to compare retrieval quality with V2 reranking signals enabled ("warm") vs disabled ("cold") so that I can quantify the value added by mem_class, depth_score, spacing_bonus, and project penalty.

#### Acceptance Criteria

1. WHEN the cold/warm comparison script is run, THE script SHALL execute each query from both golden queries and curated query sets twice: once with production rerank weights ("warm") and once with V2 signals zeroed out ("cold").
2. WHEN running in "cold" mode, THE script SHALL call `rerank()` with a wrapper that sets: `mem_class_boost = 0.0`, `depth_score` weight = 0.0, `spacing_bonus = 1.0` (constant, ignoring `last_accessed_at` — reinforcement still applies as `0.03 * log1p(access_count) * 1.0`), and `project_penalty = 0.0`, while keeping base weights (`rrf`, `overlap`, `title_overlap`, `recency`, `length`) and the reinforcement coefficient unchanged.
3. WHEN the comparison completes, THE script SHALL output a side-by-side summary showing: warm MRR vs cold MRR, warm Hit@k vs cold Hit@k for k=1,3,5,10, and the delta for each metric.
4. WHEN the comparison completes, THE script SHALL output per-query detail showing which queries improved, degraded, or stayed the same between warm and cold.
5. WHEN the comparison results are saved, THE output file SHALL be a timestamped JSON in `evaluations/results/cold_warm_YYYYMMDD_HHMMSS.json`.

### Requirement 3: Signal Ablation Testing

**User Story:** As a developer, I want to disable individual reranking signals one at a time and measure the impact on retrieval quality so that I can identify which signals contribute most and which may be noise.

#### Acceptance Criteria

1. WHEN the ablation script is run, THE script SHALL execute the full query set (golden + curated) once per ablation condition: baseline (all signals), minus-mem_class, minus-depth_score, minus-spacing_bonus (force `spacing_bonus = 1.0` constant while keeping reinforcement coefficient), minus-project_penalty, minus-type_boost, minus-reinforcement (zero the `0.03` coefficient entirely, killing the full reinforcement term).
2. WHEN an ablation condition disables a signal, THE script SHALL zero out only that signal's contribution in the rerank formula while keeping all other signals at production values.
3. WHEN all ablation conditions complete, THE script SHALL output a summary table showing: signal name, MRR with signal, MRR without signal, delta MRR, and rank of impact (largest delta = most impactful signal).
4. WHEN the ablation results are saved, THE output file SHALL be a timestamped JSON in `evaluations/results/ablation_YYYYMMDD_HHMMSS.json`.
5. WHEN the ablation script is run, THE script SHALL reuse embeddings across conditions (generate once, reuse for all ablation runs) to minimize Bedrock API calls.

### Requirement 4: Consolidation Quality Metrics

**User Story:** As a developer, I want to measure whether dream-cycle insights actually retrieve better than the raw episodic memories they were distilled from, so that I can validate the dream cycle is producing genuinely useful consolidations.

#### Acceptance Criteria

1. WHEN the consolidation quality script is run, THE script SHALL identify all accepted dream-cycle insights that have source memories recorded in `candidate_json.source_memories` on the `dream_cycle_candidates` table (the Thinker's declared source UUIDs, not the copy in the created memory's `metadata.source_memories`).
2. WHEN an insight and its source memories are identified, THE script SHALL run each golden query for that insight and record: (a) the rank position and rerank score of the insight, and (b) the rank position and rerank score of each source memory.
3. WHEN the consolidation comparison completes, THE script SHALL compute: fraction of queries where the insight outranks all its sources, average rank improvement (source rank minus insight rank), and average rerank score improvement.
4. WHEN the consolidation results are saved, THE output file SHALL be a timestamped JSON in `evaluations/results/consolidation_YYYYMMDD_HHMMSS.json`.
5. WHEN an insight has no source memories recorded, THE script SHALL skip it and log a warning.

### Requirement 5: Longitudinal Tracking

**User Story:** As a developer, I want evaluation results stored with timestamps so that I can track retrieval quality trends over time as the corpus grows and the dream cycle runs.

#### Acceptance Criteria

1. WHEN any evaluation script completes, THE script SHALL write results to `evaluations/results/` with a filename containing the evaluation type and ISO 8601 timestamp.
2. WHEN the trend analysis script is run, THE script SHALL read all result files of a given type from `evaluations/results/`, extract the aggregate metrics, and output a chronological summary showing metric values over time.
3. WHEN the trend analysis script outputs results, THE output SHALL include: date, total queries, MRR, Hit@1, Hit@3, Hit@5, Hit@10, and corpus size (total memory count at time of evaluation).
4. WHEN the trend analysis script is run, THE script SHALL also compute deltas between consecutive runs (e.g., MRR improved by +0.02 since last run).
5. WHEN any evaluation script writes results, THE results JSON SHALL include a `metadata` section with: timestamp, corpus_size (total memories), script_version, and git commit hash (if available).

### Requirement 6: Unified Evaluation Runner

**User Story:** As a developer, I want a single entry point that runs all evaluation tiers and produces a consolidated report so that monthly benchmarking is a one-command operation.

#### Acceptance Criteria

1. WHEN the unified runner is invoked with `python scripts/run_evaluation.py`, THE script SHALL execute in order: golden queries (existing Tier 3), curated query benchmark, cold/warm comparison, signal ablation, and consolidation quality.
2. WHEN the unified runner completes, THE script SHALL output a consolidated summary to stdout showing key metrics from each tier.
3. WHEN the unified runner is invoked with `--tier <name>`, THE script SHALL run only the specified tier (one of: `golden`, `curated`, `cold_warm`, `ablation`, `consolidation`, `trends`).
4. WHEN the unified runner is invoked with `--dry-run`, THE script SHALL report what would be executed (query counts, tiers) without running any evaluations or making any database queries.
5. WHEN the unified runner completes, THE script SHALL write a consolidated JSON report to `evaluations/results/full_eval_YYYYMMDD_HHMMSS.json` containing results from all executed tiers.
