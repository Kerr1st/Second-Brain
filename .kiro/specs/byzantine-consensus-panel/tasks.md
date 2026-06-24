# Implementation Plan: Byzantine Consensus Panel

## Overview

Upgrade the Dream Cycle consensus panel from 3 evaluators to 4 (adding Methodologist), replace the three-state consensus model (ACCEPTED/DEFERRED/REJECTED) with binary BFT consensus (≥3/4 ACCEPTED, ≤2/4 REJECTED), remove all DEFERRED machinery, add accepted-dissent signal preservation via digest annotation and feedback injection, and extend the database schema with evaluator D columns.

## Tasks

- [x] 1. Update tally_consensus to binary 4-verdict model
  - [x] 1.1 Rewrite `tally_consensus()` in `src/dream_cycle/consensus.py` to accept exactly 4 verdicts, return ACCEPTED if ≥3 ACCEPT else REJECTED, raise ValueError if len != 4
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - [x] 1.2 Write property test: Binary BFT Consensus Tally Correctness
    - **Property 1: Binary BFT Consensus Tally Correctness**
    - Generate all combinations of 4 binary verdicts; assert accept_count ≥ 3 → ACCEPTED, accept_count ≤ 2 → REJECTED, DEFERRED never returned
    - **Validates: Requirements 2.1, 2.2, 2.3**
  - [x] 1.3 Write property test: Tally Input Validation
    - **Property 2: Tally Input Validation**
    - Generate lists of length 0-3 and 5+; assert ValueError raised
    - **Validates: Requirements 2.4, 2.5**
  - [x] 1.4 Update existing tests in `tests/test_consensus.py` — change 3-verdict helpers to 4-verdict, remove DEFERRED assertions, update `TestConsensusTallyProperty`, `TestConsensusTallyExplicit`, and `TestStandaloneTallyConsensus` to match new binary model
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 2. Add Methodologist prompt template
  - [x] 2.1 Add `_METHODOLOGIST_CRITERIA`, Methodologist entries in `_ROLE_DESCRIPTIONS` and `_ROLE_CRITERIA` dicts, and update `get_evaluator_prompt` valid-role error message in `src/prompts/panel.py`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 4.1, 4.2, 4.3_
  - [x] 2.2 Write property test: Methodologist Prompt Completeness
    - **Property 3: Methodologist Prompt Completeness**
    - For random candidate JSON and source content, assert prompt contains "internal consistency", "source independence", "reasoning structure", "reproducibility"
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 4.1, 4.2**

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Update orchestrator to 4-evaluator binary consensus and remove DEFERRED machinery
  - [x] 4.1 In `src/dream_cycle/orchestrator.py`: add 4th evaluator invocation (methodologist) in Step 7, build 8-key verdicts_dict, replace DEFERRED branch with REJECTED, remove `_is_second_deferral()` method, remove deferred candidate retrieval (Step 5), remove `deferred` parameter from `invoke_thinker()` call and method signature, remove `deferred_new` list, ensure `candidates_deferred` is always 0
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 6.1, 6.2, 6.3, 6.4, 6.5_
  - [x] 4.2 Write property test: Four-Evaluator Orchestration
    - **Property 4: Four-Evaluator Orchestration**
    - Mock invoker, assert exactly 4 evaluator invocations per candidate and 8-key verdicts dict
    - **Validates: Requirements 3.1, 3.3**
  - [x] 4.3 Write property test: Evaluator Independence
    - **Property 5: Evaluator Independence**
    - Capture all 4 evaluator prompts, assert no cross-contamination of verdicts/reasoning
    - **Validates: Requirements 3.4**
  - [x] 4.4 Write property test: No DEFERRED in Pipeline
    - **Property 6: No DEFERRED in Pipeline**
    - Run pipeline with mocked evaluators returning various verdict combos, assert no DEFERRED final verdicts and candidates_deferred == 0
    - **Validates: Requirements 2.3, 6.1, 6.5**

- [x] 5. Database migration and dream_cycle_db updates
  - [x] 5.1 Create `migrations/004_evaluator_d.sql` with `ALTER TABLE dream_cycle_candidates ADD COLUMN IF NOT EXISTS evaluator_d_verdict TEXT, ADD COLUMN IF NOT EXISTS evaluator_d_reasoning TEXT` plus column comments
    - _Requirements: 5.1, 5.5_
  - [x] 5.2 Update `store_candidate()` in `src/dream_cycle_db.py` — add evaluator_d_verdict and evaluator_d_reasoning to INSERT (15 columns)
    - _Requirements: 5.2_
  - [x] 5.3 Update `get_recent_rejections()` — add evaluator_d columns to SELECT
    - _Requirements: 5.3_
  - [x] 5.4 Update `get_evaluator_verdicts_for_run()` — add evaluator_d columns to SELECT and return "methodologist" key in result dict
    - _Requirements: 5.4_
  - [x] 5.5 Update `get_tier1_metrics()` — change evaluator_calls factor from 3 to 4
    - _Requirements: 9.1_
  - [x] 5.6 Add `get_accepted_dissents(n_cycles=3)` function to `src/dream_cycle_db.py` — query accepted candidates with at least one REJECT verdict from recent cycles
    - _Requirements: 8.1_
  - [x] 5.7 Deprecate `get_deferred_candidates()` and `mark_deferred_twice_rejected()` — add deprecation docstring notes, no longer called by orchestrator
    - _Requirements: 6.2, 6.4_
  - [x] 5.8 Write property test: store_candidate Persists Evaluator D
    - **Property 9: store_candidate Persists Evaluator D**
    - Mock DB cursor, assert INSERT includes evaluator_d_verdict and evaluator_d_reasoning columns
    - **Validates: Requirements 5.2**

- [x] 6. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Update feedback injection with Methodologist and accepted dissents
  - [x] 7.1 In `src/dream_cycle/feedback.py`: add `"evaluator_d": "Methodologist"` to evaluator_roles dict, call `get_accepted_dissents()`, add "Dissenting concerns on accepted insights" section
    - _Requirements: 8.1, 8.2, 8.3_
  - [x] 7.2 Write property test: Feedback Injection Includes Methodologist and Accepted Dissents
    - **Property 7: Feedback Injection Includes Methodologist and Accepted Dissents**
    - Mock DB queries with evaluator_d rejections and non-unanimous accepts, assert "Methodologist" appears and "Dissenting concerns" section present
    - **Validates: Requirements 8.1, 8.2, 8.3**

- [x] 8. Update digest with Methodologist and dissent annotations
  - [x] 8.1 In `src/dream_cycle/digest.py`: add "methodologist" to evaluator role loop, add 3/4 vs 4/4 acceptance annotation with dissenter info
    - _Requirements: 7.1, 7.2, 7.3_
  - [x] 8.2 Write property test: Digest Annotates Non-Unanimous Accepts
    - **Property 8: Digest Annotates Non-Unanimous Accepts**
    - Mock verdicts with 3/4 and 4/4 cases, assert "Accepted (3/4)" with dissenter info and "Accepted (4/4 — unanimous)" respectively
    - **Validates: Requirements 7.1, 7.2, 7.3**

- [x] 9. Update models and remaining files
  - [x] 9.1 Update `EvaluatorVerdict` docstring in `src/models.py` to list all four valid roles: skeptic, advocate, epistemologist, methodologist
    - _Requirements: 1.6_
  - [x] 9.2 Update `tests/test_dream_cycle.py` — add 4th evaluator invocation to evaluator independence tests, remove deferred handling assertions, update `TestIsSecondDeferral` and `TestTwoStrikeRuleInRun` (remove or convert to verify removal), update evaluator role lists from 3 to 4
    - _Requirements: 3.1, 3.4, 6.1, 6.2_
  - [x] 9.3 Update `tests/test_integration.py` — update mock evaluator to invoke 4 roles, update threshold assertions (e.g., 1 REJECT + 3 ACCEPT = ACCEPTED not DEFERRED), update evaluator call count from 6 to 8, update verdicts_dict assertions to include evaluator_d, remove deferred count assertions
    - _Requirements: 2.1, 2.2, 3.1, 6.5_

- [x] 10. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- The existing 204 tests must continue to pass (updated where needed to reflect the new 4-evaluator binary model)
- `docs/DREAM-CYCLE-DESIGN.md` was already updated during the design phase (Requirement 10) — no implementation task needed
