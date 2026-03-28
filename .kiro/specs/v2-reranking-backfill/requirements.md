# Requirements Document

## Introduction

The V2 reranking formula in `src/search.py` uses three signals — `mem_class`, `depth_score` (in metadata), and `last_accessed_at` — that are currently NULL for all 74K+ existing memories. This means the reranking formula returns 0 contribution from these signals for every legacy memory. This feature delivers idempotent, batch-oriented backfill scripts that populate these three columns across the entire corpus, plus a before/after benchmark using `golden_queries.py` to measure retrieval quality improvement.

## Glossary

- **Backfill_Script**: A Python script in the `scripts/` directory that reads existing memories from the PostgreSQL database and populates a previously-NULL column or metadata field in batches.
- **Memory**: A row in the `memories` table representing a single knowledge unit (idea, source, synthesis, etc.) in the second-brain database.
- **mem_class**: A column on the `memories` table holding the classification of a memory as `"semantic"`, `"episodic"`, or `"procedural"`.
- **depth_score**: A float in [0.0, 1.0] stored in the `metadata` JSONB column under the key `depth_score`, measuring the explanatory depth of a memory's content.
- **last_accessed_at**: A timestamp column on the `memories` table recording when the memory was last retrieved; used by the spacing bonus in the reranking formula.
- **classify_memory**: The pure function in `src/classify.py` that deterministically classifies a memory given its type, source_type, and content.
- **compute_depth_score**: The pure function in `src/depth.py` that computes a numeric depth score from content text.
- **Golden_Queries**: The benchmark script `scripts/golden_queries.py` that measures retrieval quality by checking whether dream-cycle insights appear in top search results for their declared questions.
- **Batch**: A group of memory rows processed in a single database transaction during backfill, controlled by a configurable batch size.
- **Idempotent**: A property of the backfill scripts ensuring that re-running the script produces the same result and does not corrupt or duplicate data.

## Requirements

### Requirement 1: Backfill Memory Classification

**User Story:** As a system operator, I want to classify all existing memories with a `mem_class` value, so that the reranking formula's classification boost signal is active for the entire corpus.

#### Acceptance Criteria

1. WHEN `backfill_mem_class.py` is executed, THE Backfill_Script SHALL read all memories where `mem_class` IS NULL from the database and compute a classification using `classify_memory(type, source_type, content)` for each memory.
2. WHEN a classification is computed, THE Backfill_Script SHALL update the memory's `mem_class` column with the returned value (`"semantic"`, `"episodic"`, or `"procedural"`).
3. THE Backfill_Script SHALL process memories in configurable batches (default 500) and commit each batch in a separate transaction.
4. THE Backfill_Script SHALL log progress after each batch, reporting the number of memories processed and the total remaining.
5. WHILE a memory already has a non-NULL `mem_class` value, THE Backfill_Script SHALL skip that memory (idempotent behavior).
6. IF a single memory fails classification, THEN THE Backfill_Script SHALL log the error with the memory ID and continue processing the remaining memories in the batch.
7. WHEN the backfill completes, THE Backfill_Script SHALL print a summary showing total processed, total skipped, total errors, and a breakdown of classification counts (semantic, episodic, procedural).

### Requirement 2: Backfill Depth Score

**User Story:** As a system operator, I want to compute and store a `depth_score` for all existing memories, so that the reranking formula's depth signal is active for the entire corpus.

#### Acceptance Criteria

1. WHEN `backfill_depth_score.py` is executed, THE Backfill_Script SHALL read all memories where `metadata->>'depth_score'` IS NULL from the database and compute a depth score using `compute_depth_score(content)` for each memory.
2. WHEN a depth score is computed, THE Backfill_Script SHALL merge the `depth_score` float value into the memory's existing `metadata` JSONB column without overwriting other metadata keys.
3. THE Backfill_Script SHALL process memories in configurable batches (default 500) and commit each batch in a separate transaction.
4. THE Backfill_Script SHALL log progress after each batch, reporting the number of memories processed and the total remaining.
5. WHILE a memory already has a non-NULL `metadata->>'depth_score'` value, THE Backfill_Script SHALL skip that memory (idempotent behavior).
6. IF a single memory fails depth score computation, THEN THE Backfill_Script SHALL log the error with the memory ID and continue processing the remaining memories in the batch.
7. WHEN the backfill completes, THE Backfill_Script SHALL print a summary showing total processed, total skipped, total errors, and basic statistics of computed scores (min, max, mean).

### Requirement 3: Backfill Last Accessed Timestamp

**User Story:** As a system operator, I want to set `last_accessed_at` to `updated_at` for all existing memories that have a NULL `last_accessed_at`, so that the spacing bonus in the reranking formula produces meaningful values instead of defaulting to 1.0 for all legacy memories.

#### Acceptance Criteria

1. THE backfill SHALL be a single SQL statement: `UPDATE memories SET last_accessed_at = updated_at WHERE last_accessed_at IS NULL`.
2. THE SQL statement SHALL be idempotent — re-running it affects zero rows when all memories already have a non-NULL `last_accessed_at`.
3. THE SQL statement SHALL be delivered as a standalone script in `scripts/backfill_last_accessed.sql` (not a numbered migration, since it is a one-time data fix rather than a schema change).
4. THE script SHALL include a preceding `SELECT count(*)` query to report how many rows will be affected before executing the UPDATE.

### Requirement 4: Dry-Run Mode

**User Story:** As a system operator, I want to preview what each backfill script would do without modifying the database, so that I can verify correctness before committing changes.

#### Acceptance Criteria

1. WHEN the `--dry-run` flag is passed, THE Backfill_Script SHALL execute all read and compute operations but SHALL NOT commit any database writes.
2. WHEN running in dry-run mode, THE Backfill_Script SHALL log the same progress and summary output as a real run, prefixed with `[DRY RUN]`.
3. THE Backfill_Script SHALL default to live mode when `--dry-run` is not specified.

### Requirement 5: Retrieval Quality Benchmark

**User Story:** As a system operator, I want to run `golden_queries.py` before and after the backfill to measure the impact on retrieval quality, so that I can verify the backfill improved search results.

#### Acceptance Criteria

1. WHEN `golden_queries.py` is run before the backfill, THE Golden_Queries script SHALL produce baseline metrics (MRR, hit rates at 1/3/5/10).
2. WHEN `golden_queries.py` is run after all three backfill scripts complete, THE Golden_Queries script SHALL produce post-backfill metrics using the same queries.
3. THE Golden_Queries script SHALL already support this workflow without modification (existing functionality).

### Requirement 6: Script CLI Interface

**User Story:** As a system operator, I want consistent CLI arguments across all backfill scripts, so that I can control batch size and behavior uniformly.

#### Acceptance Criteria

1. THE Backfill_Script SHALL accept a `--batch-size` argument to override the default batch size.
2. THE Backfill_Script SHALL accept a `--dry-run` flag to enable dry-run mode.
3. THE Backfill_Script SHALL accept a `--limit` argument to cap the total number of memories processed (useful for testing on a subset).
4. WHEN no arguments are provided, THE Backfill_Script SHALL use default values (batch-size=500, no limit, live mode).
5. IF an invalid argument is provided, THEN THE Backfill_Script SHALL print a usage message and exit with a non-zero exit code.
