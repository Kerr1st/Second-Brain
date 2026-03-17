# Requirements Document

## Introduction

Decompose the monolithic `src/dream_cycle.py` (43KB, 13 methods) into a `src/dream_cycle/` package with focused, single-responsibility modules. The existing `src/dream_cycle_db.py` (27KB, 15 functions) stays as-is but receives 3 new query functions: two extracted from `generate_digest()` and one from `_get_previous_run_id()`. All 124 existing tests must pass after each extraction step. The refactor is purely structural — no behavioral changes.

## Glossary

- **Orchestrator**: The `DreamCycleOrchestrator` class that coordinates the four-agent pipeline (Explorer → Thinker → Consensus Panel → Storage). After decomposition, lives in `src/dream_cycle/orchestrator.py`.
- **Package**: The `src/dream_cycle/` directory with `__init__.py` that replaces the monolithic `src/dream_cycle.py` module.
- **Consensus_Module**: The `src/dream_cycle/consensus.py` module containing the pure `tally_consensus()` function.
- **Storage_Module**: The `src/dream_cycle/storage.py` module containing `store_accepted()` and `check_duplicate()`.
- **Digest_Module**: The `src/dream_cycle/digest.py` module containing `generate_digest()` for pure markdown rendering.
- **Feedback_Module**: The `src/dream_cycle/feedback.py` module containing `build_feedback_injection()` for formatting rejection history.
- **Patch_Target**: A `unittest.mock.patch("src.dream_cycle.X")` string in test files that must be updated when the symbol `X` moves to a submodule.
- **Dream_Cycle_DB**: The `src/dream_cycle_db.py` module providing database operations for dream cycle runs and candidates.
- **Public_API**: The set of symbols exported from `src/dream_cycle/__init__.py`, specifically `DreamCycleOrchestrator`.

## Requirements

### Requirement 1: Package Skeleton Creation

**User Story:** As a developer, I want `src/dream_cycle.py` converted into a `src/dream_cycle/` package so that the codebase supports modular extraction of responsibilities.

#### Acceptance Criteria

1. WHEN the Package is created, THE Package SHALL contain an `__init__.py` that exports `DreamCycleOrchestrator` from `src.dream_cycle.orchestrator`.
2. WHEN the Package is created, THE Package SHALL contain an `orchestrator.py` with the full contents of the original `src/dream_cycle.py` module.
3. WHEN the Package is created, THE Orchestrator SHALL retain all 13 original methods with identical signatures and behavior.
4. WHEN the Package is created, THE `scripts/dream_cycle_run.py` import `from src.dream_cycle import DreamCycleOrchestrator` SHALL resolve to the Orchestrator class without modification to the script.
5. WHEN the Package is created, THE test suite SHALL pass all 124 existing tests with updated Patch_Targets referencing `src.dream_cycle.orchestrator` instead of `src.dream_cycle`.
6. WHEN the Package is created, THE original `src/dream_cycle.py` file SHALL be removed.

### Requirement 2: Consensus Module Extraction

**User Story:** As a developer, I want `tally_consensus()` extracted into a pure module so that consensus logic is independently testable with zero external dependencies.

#### Acceptance Criteria

1. WHEN the Consensus_Module is created, THE Consensus_Module SHALL contain a standalone `tally_consensus()` function that accepts a `list[EvaluatorVerdict]` and returns a string.
2. THE Consensus_Module SHALL import only from `src.models` and the Python standard library.
3. WHEN the Consensus_Module is created, THE Orchestrator SHALL import `tally_consensus` from `src.dream_cycle.consensus` and delegate to the standalone function.
4. WHEN the Consensus_Module is created, THE test suite SHALL pass all 124 existing tests.
5. THE `tally_consensus` function SHALL map 3 ACCEPT verdicts to "ACCEPTED", 2 ACCEPT verdicts to "DEFERRED", and fewer than 2 ACCEPT verdicts to "REJECTED".

### Requirement 3: Storage Module Extraction

**User Story:** As a developer, I want `store_accepted()` and `check_duplicate()` extracted into a storage module so that memory persistence and deduplication logic are isolated from pipeline orchestration.

#### Acceptance Criteria

1. WHEN the Storage_Module is created, THE Storage_Module SHALL contain `store_accepted()` and `check_duplicate()` as standalone functions.
2. WHEN the Storage_Module is created, THE Storage_Module SHALL import `generate_embedding`, `create_memory`, `update_memory`, `get_memory`, `create_relationship`, and `search_similar` from `src.db` and `src.embeddings`.
3. WHEN the Storage_Module is created, THE Orchestrator SHALL import `store_accepted` and `check_duplicate` from `src.dream_cycle.storage` and delegate to the standalone functions.
4. WHEN the Storage_Module is created, THE Patch_Targets in test files that mock `generate_embedding`, `create_memory`, `update_memory`, `get_memory`, `create_relationship`, and `search_similar` SHALL reference `src.dream_cycle.storage` instead of `src.dream_cycle`.
5. WHEN the Storage_Module is created, THE test suite SHALL pass all 124 existing tests.
6. THE `store_accepted` function SHALL handle CREATE, UPDATE, and SUPERSEDE operations identically to the original method.
7. THE `check_duplicate` function SHALL skip chunk memories (those with non-null `parent_id`) and use strict greater-than comparison against the similarity threshold.

### Requirement 4: Digest Database Query Extraction

**User Story:** As a developer, I want the inline database queries in `generate_digest()` extracted into `dream_cycle_db.py` so that the digest module has no direct database access.

#### Acceptance Criteria

1. WHEN the queries are extracted, THE Dream_Cycle_DB SHALL contain a `get_evaluator_verdicts_for_run(run_id: str) -> dict[str, dict]` function that returns evaluator verdicts keyed by candidate title, where each value is a dict with keys `skeptic`, `advocate`, `epistemologist`, each containing `{"verdict": str, "reasoning": str}`.
2. WHEN the queries are extracted, THE Dream_Cycle_DB SHALL contain a `was_feedback_injected(run_id)` function that returns a boolean indicating whether feedback was injected for a given run.
3. WHEN the queries are extracted, THE `generate_digest()` function SHALL call `get_evaluator_verdicts_for_run()` and `was_feedback_injected()` instead of executing inline SQL.
4. WHEN the queries are extracted, THE test suite SHALL pass all 124 existing tests.

### Requirement 5: Digest Module Extraction

**User Story:** As a developer, I want `generate_digest()` extracted into a digest module so that markdown rendering is isolated from orchestration and database access.

#### Acceptance Criteria

1. WHEN the Digest_Module is created, THE Digest_Module SHALL contain a standalone `generate_digest()` function.
2. THE Digest_Module SHALL import only from `src.dream_cycle_db`, `src.models`, `pathlib`, and the Python standard library.
3. THE Digest_Module SHALL perform no direct database access (no `get_connection()` calls).
4. WHEN the Digest_Module is created, THE Orchestrator SHALL import `generate_digest` from `src.dream_cycle.digest` and delegate to the standalone function.
5. WHEN the Digest_Module is created, THE test suite SHALL pass all 124 existing tests.

### Requirement 6: Feedback Module Extraction

**User Story:** As a developer, I want `build_feedback_injection()` extracted into a feedback module so that rejection history formatting is isolated from orchestration.

#### Acceptance Criteria

1. WHEN the Feedback_Module is created, THE Feedback_Module SHALL contain a standalone `build_feedback_injection()` function.
2. THE Feedback_Module SHALL import `src.dream_cycle_db` for querying rejection history.
3. WHEN the Feedback_Module is created, THE Orchestrator SHALL import `build_feedback_injection` from `src.dream_cycle.feedback` and delegate to the standalone function.
4. WHEN Patch_Targets in test files mock `dream_cycle_db` for feedback tests, THE Patch_Targets SHALL reference `src.dream_cycle.feedback.dream_cycle_db` instead of `src.dream_cycle.dream_cycle_db`.
5. WHEN the Feedback_Module is created, THE test suite SHALL pass all 124 existing tests.

### Requirement 7: Orchestrator Cleanup

**User Story:** As a developer, I want the orchestrator cleaned up after all extractions so that it contains only pipeline coordination logic with no dead code.

#### Acceptance Criteria

1. WHEN all extractions are complete, THE Orchestrator SHALL contain only `run()`, `_run_pipeline()`, `invoke_explorer()`, `invoke_thinker()`, `invoke_evaluator()`, `_invoke_evaluator_safe()`, and `_is_second_deferral()`. The `_get_previous_run_id()` method SHALL be removed in favor of calling `dream_cycle_db.get_previous_run_id()` directly.
2. WHEN all extractions are complete, THE Orchestrator SHALL have no unused imports.
3. WHEN the three copy-paste evaluator try/except blocks in `_run_pipeline()` are refactored, THE Orchestrator SHALL contain a `_invoke_evaluator_safe()` helper that wraps `invoke_evaluator()` with crash/timeout handling and returns a default REJECT verdict on failure.
4. WHEN the evaluator loop is DRY'd, THE `_run_pipeline()` method SHALL invoke `_invoke_evaluator_safe()` for each of the three evaluator roles instead of repeating the try/except pattern.
5. WHEN all extractions are complete, THE test suite SHALL pass all 124 existing tests.

### Requirement 8: Import Path Correctness

**User Story:** As a developer, I want all import paths and mock patch targets to be correct after decomposition so that the test suite validates the actual module boundaries.

#### Acceptance Criteria

1. WHEN a function is extracted to a submodule, THE Patch_Targets in test files SHALL reference the submodule where the symbol is imported (used), not where the symbol is originally defined.
2. WHEN `storage.py` imports `generate_embedding` from `src.embeddings`, THE test Patch_Targets SHALL use `src.dream_cycle.storage.generate_embedding`.
3. WHEN `feedback.py` imports `src.dream_cycle_db`, THE test Patch_Targets SHALL use `src.dream_cycle.feedback.dream_cycle_db`.
4. THE `_get_previous_run_id` method's inline database query SHALL be extracted to `dream_cycle_db.py` as `get_previous_run_id(run_type: str) -> str | None`, and the Orchestrator SHALL delegate to this function. This ensures the Orchestrator has zero direct database access after decomposition.
5. THE `patch.object` calls in tests that target the Orchestrator instance SHALL remain unchanged across all extraction steps, except for `_get_previous_run_id` which is removed from the class per AC 4 and whose 14 `patch.object` targets must be replaced with `dream_cycle_db.get_previous_run_id` mocking on the existing `dream_cycle_db` mock.
6. WHEN the Package `__init__.py` exports `DreamCycleOrchestrator`, THE `from src.dream_cycle import DreamCycleOrchestrator` import in `scripts/dream_cycle_run.py` SHALL continue to work without modification.

### Requirement 9: Incremental Safety

**User Story:** As a developer, I want each extraction step to be independently safe so that I can verify correctness at every stage and revert a single step if needed.

#### Acceptance Criteria

1. WHEN each extraction step is completed, THE test suite SHALL pass all 124 existing tests before proceeding to the next step.
2. WHEN each extraction step is completed, THE extraction SHALL be a single atomic commit that can be reverted independently.
3. IF a test failure occurs after an extraction step, THEN THE developer SHALL be able to identify the failure cause from the Patch_Target update or function signature change in that step alone.
4. THE extraction sequence SHALL follow the order: package skeleton → consensus → storage → digest DB queries (Req 4) → digest module (Req 5) → feedback → orchestrator cleanup. Reqs 4 and 5 are separate commits within the digest extraction phase.
