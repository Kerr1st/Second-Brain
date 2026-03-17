# Implementation Plan: Dream Cycle Decomposition

## Overview

Decompose the monolithic `src/dream_cycle.py` into a `src/dream_cycle/` package following a strict 7-step extraction sequence. Each step is independently verifiable — all 124 existing tests must pass after every extraction. The sequence is ordered by risk and dependency: package skeleton → consensus → storage → digest DB queries → digest module → feedback → orchestrator cleanup.

## Tasks

- [x] 1. Package skeleton — convert `src/dream_cycle.py` to `src/dream_cycle/` package
  - [x] 1.1 Create `src/dream_cycle/` directory with `__init__.py` that re-exports `DreamCycleOrchestrator`
    - Create `src/dream_cycle/__init__.py` with `from src.dream_cycle.orchestrator import DreamCycleOrchestrator` and `__all__ = ["DreamCycleOrchestrator"]`
    - _Requirements: 1.1, 1.4_
  - [x] 1.2 Move `src/dream_cycle.py` → `src/dream_cycle/orchestrator.py`
    - Copy full contents of `src/dream_cycle.py` into `src/dream_cycle/orchestrator.py`
    - Delete the original `src/dream_cycle.py` file
    - Delete any stale `src/__pycache__/dream_cycle.cpython-*.pyc` files
    - _Requirements: 1.2, 1.3, 1.6_
  - [x] 1.3 Update all 80 `patch("src.dream_cycle.X")` targets to `patch("src.dream_cycle.orchestrator.X")` across test files
    - Update `tests/test_dream_cycle.py`: `AgentInvoker`, `os.path.expanduser`, `dream_cycle_db`, `generate_embedding`, `create_memory`, `create_relationship`, `search_similar`, `update_memory`, `get_memory`, `logger`
    - Update `tests/test_consensus.py`: `AgentInvoker`, `os.path.expanduser`
    - Update `tests/test_integration.py`: `AgentInvoker`, `os.path.expanduser`, `dream_cycle_db`, `generate_embedding`, `create_memory`, `create_relationship`, `search_similar`, `update_memory`, `get_memory`
    - Verify `scripts/dream_cycle_run.py` import `from src.dream_cycle import DreamCycleOrchestrator` works without modification
    - _Requirements: 1.4, 1.5, 8.1, 8.6_

- [x] 2. Checkpoint — run `pytest tests/ -v` and verify all 124 tests pass

- [x] 3. Consensus extraction — create `consensus.py` with pure `tally_consensus()`
  - [x] 3.1 Create `src/dream_cycle/consensus.py` with standalone `tally_consensus()` function
    - Function accepts `list[EvaluatorVerdict]` and returns `str` ("ACCEPTED"/"DEFERRED"/"REJECTED")
    - Import only from `src.models` and Python standard library
    - _Requirements: 2.1, 2.2, 2.3, 2.5_
  - [x] 3.2 Update orchestrator to import and delegate to standalone `tally_consensus`
    - Add `from src.dream_cycle.consensus import tally_consensus` to orchestrator imports
    - Replace `self.tally_consensus(evaluator_verdicts)` calls with `tally_consensus(evaluator_verdicts)` in `_run_pipeline()`
    - Remove the `tally_consensus` method from `DreamCycleOrchestrator` class
    - _Requirements: 2.3, 2.4_
  - [x] 3.3 Update `tests/test_consensus.py` to also test the standalone function directly
    - Add a test verifying `from src.dream_cycle.consensus import tally_consensus` works and produces correct results for all 3 verdict counts
    - Existing tests via `orch.tally_consensus()` should still pass (orchestrator delegates to standalone)
    - _Requirements: 2.4, 2.5_

- [x] 4. Checkpoint — run `pytest tests/ -v` and verify all 124 tests pass (plus new consensus test)

- [x] 5. Storage extraction — create `storage.py` with `store_accepted()` and `check_duplicate()`
  - [x] 5.1 Create `src/dream_cycle/storage.py` with standalone `store_accepted()` and `check_duplicate()` functions
    - Import `generate_embedding`, `create_memory`, `update_memory`, `get_memory`, `create_relationship`, `search_similar` from `src.db` and `src.embeddings`
    - Import `CandidateInsight` from `src.models`
    - Extract method bodies from orchestrator, converting `self.X()` calls to module-level function calls
    - `store_accepted` handles CREATE, UPDATE, and SUPERSEDE operations identically to original
    - `check_duplicate` skips chunk memories (non-null `parent_id`) and uses strict greater-than threshold comparison
    - _Requirements: 3.1, 3.2, 3.6, 3.7_
  - [x] 5.2 Update orchestrator to import and delegate to standalone storage functions
    - Add `from src.dream_cycle.storage import store_accepted, check_duplicate` to orchestrator imports
    - Replace `self.store_accepted(candidate)` with `store_accepted(candidate)` in `_run_pipeline()`
    - Replace `self.check_duplicate(...)` with `check_duplicate(...)` in `_run_pipeline()`
    - Remove `store_accepted` and `check_duplicate` methods from `DreamCycleOrchestrator` class
    - Remove now-unused imports from orchestrator: `generate_embedding`, `create_memory`, `update_memory`, `get_memory`, `create_relationship`, `search_similar`
    - _Requirements: 3.3_
  - [x] 5.3 Update 47 patch targets in test files from `src.dream_cycle.orchestrator.{symbol}` to `src.dream_cycle.storage.{symbol}`
    - Update 14 `generate_embedding` patches → `src.dream_cycle.storage.generate_embedding`
    - Update 8 `create_memory` patches → `src.dream_cycle.storage.create_memory`
    - Update 8 `create_relationship` patches → `src.dream_cycle.storage.create_relationship`
    - Update 7 `search_similar` patches → `src.dream_cycle.storage.search_similar`
    - Update 5 `update_memory` patches → `src.dream_cycle.storage.update_memory`
    - Update 5 `get_memory` patches → `src.dream_cycle.storage.get_memory`
    - Applies to `tests/test_dream_cycle.py`, `tests/test_consensus.py`, and `tests/test_integration.py`
    - _Requirements: 3.4, 3.5, 8.2_
  - [x] 5.4 Add import verification tests for standalone `store_accepted` and `check_duplicate`
    - Verify `from src.dream_cycle.storage import store_accepted, check_duplicate` works
    - **Property 2: store_accepted behavioral equivalence across operations**
    - **Property 3: check_duplicate skips chunks and uses strict threshold**
    - **Validates: Requirements 3.6, 3.7**

- [x] 6. Checkpoint — run `pytest tests/ -v` and verify all 124 tests pass (plus new tests)

- [x] 7. Digest DB query extraction — add 3 new functions to `src/dream_cycle_db.py`
  - [x] 7.1 Add `get_evaluator_verdicts_for_run(run_id: str) -> dict[str, dict]` to `src/dream_cycle_db.py`
    - Query `dream_cycle_candidates` for accepted candidates in the given run
    - Return dict keyed by candidate title, each value a dict with keys `skeptic`, `advocate`, `epistemologist`, each containing `{"verdict": str, "reasoning": str}`
    - _Requirements: 4.1_
  - [x] 7.2 Add `was_feedback_injected(run_id: str) -> bool` to `src/dream_cycle_db.py`
    - Query `dream_cycle_runs` for `explorer_feedback_injected` column
    - Return True when non-empty, False otherwise
    - _Requirements: 4.2_
  - [x] 7.3 Add `get_previous_run_id(run_type: str) -> str | None` to `src/dream_cycle_db.py`
    - Query most recent completed run of the same type
    - Return run UUID as string, or None if no previous completed run exists
    - Extracted from `DreamCycleOrchestrator._get_previous_run_id()` inline SQL
    - _Requirements: 8.4_
  - [x] 7.4 Add unit tests for the 3 new `dream_cycle_db` functions
    - Test `get_evaluator_verdicts_for_run` returns correct dict structure with mocked cursor
    - Test `was_feedback_injected` returns True/False correctly with mocked cursor
    - Test `get_previous_run_id` returns UUID string or None with mocked cursor
    - **Property 6: New dream_cycle_db query functions return correct results**
    - **Validates: Requirements 4.1, 4.2, 8.4**

- [x] 8. Checkpoint — run `pytest tests/ -v` and verify all 124 tests pass (plus new DB function tests)

- [x] 9. Digest module extraction — create `digest.py` with standalone `generate_digest()`
  - [x] 9.1 Create `src/dream_cycle/digest.py` with standalone `generate_digest()` function
    - Import `src.dream_cycle_db` for `get_evaluator_verdicts_for_run()` and `was_feedback_injected()`
    - Import `CandidateInsight` from `src.models`, `pathlib.Path`, and standard library only
    - Replace inline SQL with `dream_cycle_db.get_evaluator_verdicts_for_run(run_id)` and `dream_cycle_db.was_feedback_injected(run_id)` calls
    - No direct `get_connection()` calls — the module has zero direct database access
    - _Requirements: 5.1, 5.2, 5.3_
  - [x] 9.2 Update orchestrator to import and delegate to standalone `generate_digest`
    - Add `from src.dream_cycle.digest import generate_digest` to orchestrator imports
    - Replace `self.generate_digest(...)` with `generate_digest(...)` in `_run_pipeline()`
    - Remove `generate_digest` method from `DreamCycleOrchestrator` class
    - Remove now-dead `from src.db import get_connection` import from orchestrator (if present)
    - _Requirements: 5.4_
  - [x] 9.3 Update digest test patches and remove dead code
    - Remove the `_mock_db_for_digest()` helper function from `tests/test_dream_cycle.py` (dead code)
    - Replace 4 `patch("src.db.get_connection", mock_get_conn)` calls in digest tests with `patch("src.dream_cycle.digest.dream_cycle_db")` mocking `get_evaluator_verdicts_for_run` and `was_feedback_injected` directly
    - _Requirements: 5.5, 8.1_

- [x] 10. Checkpoint — run `pytest tests/ -v` and verify all 124 tests pass

- [x] 11. Feedback extraction — create `feedback.py` with standalone `build_feedback_injection()`
  - [x] 11.1 Create `src/dream_cycle/feedback.py` with standalone `build_feedback_injection()` function
    - Import `src.dream_cycle_db` for `get_recent_rejections()` and `get_user_rejections()`
    - Extract method body from orchestrator, converting `self.X()` calls to module-level calls
    - _Requirements: 6.1, 6.2_
  - [x] 11.2 Update orchestrator to import and delegate to standalone `build_feedback_injection`
    - Add `from src.dream_cycle.feedback import build_feedback_injection` to orchestrator imports
    - Replace `self.build_feedback_injection()` with `build_feedback_injection()` in `_run_pipeline()`
    - Remove `build_feedback_injection` method from `DreamCycleOrchestrator` class
    - _Requirements: 6.3_
  - [x] 11.3 Update feedback test patches to `src.dream_cycle.feedback.dream_cycle_db`
    - Update feedback-specific test patches from `src.dream_cycle.orchestrator.dream_cycle_db` to `src.dream_cycle.feedback.dream_cycle_db`
    - _Requirements: 6.4, 8.3_
  - [x] 11.4 Update integration test with shared mock pattern for 3 `dream_cycle_db` imports
    - After digest and feedback extraction, `dream_cycle_db` is imported in 3 modules: orchestrator, digest, feedback
    - Update integration test to use shared mock pattern:
      ```python
      with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_dc_db, \
           patch("src.dream_cycle.digest.dream_cycle_db", mock_dc_db), \
           patch("src.dream_cycle.feedback.dream_cycle_db", mock_dc_db):
      ```
    - All three patches reference the same mock object so `mock_dc_db.create_run.return_value` etc. work uniformly
    - _Requirements: 6.5, 8.1_

- [x] 12. Checkpoint — run `pytest tests/ -v` and verify all 124 tests pass

- [x] 13. Orchestrator cleanup — DRY evaluator loop, remove dead code, add new tests
  - [x] 13.1 Add `_invoke_evaluator_safe()` helper method to orchestrator
    - Wraps `invoke_evaluator()` with try/except for `TimeoutError` and `RuntimeError`
    - Returns default `EvaluatorVerdict(role=role, verdict="REJECT", reasoning="Evaluator crashed or timed out")` on failure
    - Replace the three copy-paste try/except blocks in `_run_pipeline()` with calls to `_invoke_evaluator_safe()`
    - _Requirements: 7.3, 7.4_
  - [x] 13.2 Remove `_get_previous_run_id` method from orchestrator
    - Replace `self._get_previous_run_id(run_type)` call in `_run_pipeline()` with `dream_cycle_db.get_previous_run_id(run_type)`
    - Remove the `_get_previous_run_id` method from `DreamCycleOrchestrator` class (the inline `from src.db import get_connection` import inside it is removed along with the method)
    - _Requirements: 7.1, 8.4_
  - [x] 13.3 Update 14 `patch.object(orch, "_get_previous_run_id", ...)` calls in test files
    - Replace all 14 `patch.object(orch, "_get_previous_run_id", return_value=X)` with `mock_db.get_previous_run_id.return_value = X` on the existing `dream_cycle_db` mock
    - 13 occurrences in `tests/test_dream_cycle.py`, 1 in `tests/test_integration.py`
    - _Requirements: 8.5_
  - [x] 13.4 Remove unused imports from orchestrator
    - Audit and remove any imports that are no longer used after all extractions
    - Verify the orchestrator imports only: `json`, `logging`, `os`, `datetime`, `psycopg2`, `AgentInvoker`, models, prompts, `dream_cycle_db`, and the 4 extracted module functions
    - _Requirements: 7.2_
  - [x] 13.5 Add property test for `_invoke_evaluator_safe` returns REJECT on failure
    - **Property 4: _invoke_evaluator_safe returns REJECT on failure**
    - For any CandidateInsight and evaluator role, when `invoke_evaluator()` raises `TimeoutError` or `RuntimeError`, `_invoke_evaluator_safe()` returns `EvaluatorVerdict` with `verdict="REJECT"`
    - **Validates: Requirements 7.3**
  - [x] 13.6 Add property test for final orchestrator method set
    - **Property 5: Final orchestrator method set contains only coordination methods**
    - Verify the set of non-dunder methods is exactly: `run`, `_run_pipeline`, `invoke_explorer`, `invoke_thinker`, `invoke_evaluator`, `_invoke_evaluator_safe`, `_is_second_deferral`
    - **Validates: Requirements 7.1**
  - [x] 13.7 Add `__init__.py` re-export identity test
    - **Property 7: Public API preservation through __init__.py**
    - Verify `from src.dream_cycle import DreamCycleOrchestrator` resolves to the same object as `from src.dream_cycle.orchestrator import DreamCycleOrchestrator`
    - **Validates: Requirements 1.1, 1.4, 8.6**

- [x] 14. Final checkpoint — run `pytest tests/ -v` and verify all 124 tests pass (plus all new tests)
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each checkpoint verifies all 124 existing tests pass before proceeding to the next extraction step
- The extraction sequence is strictly ordered: package skeleton → consensus → storage → digest DB queries → digest module → feedback → orchestrator cleanup
- Property tests validate universal correctness properties from the design document
- All patch target counts (80 total, 47 for storage) are derived from the actual test files
