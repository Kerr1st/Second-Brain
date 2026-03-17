# Implementation Plan: Dream Cycle — Multi-Agent Autonomous Learning

## Overview

Implement the Dream Cycle as a four-agent pipeline (Explorer → Thinker → Consensus Panel) that runs during downtime to discover hidden connections, surface contradictions, and store consensus-gated insights. This replaces/extends V2 Task 8 (Consolidation Pipeline) with a research-grounded multi-agent approach. Implementation follows the module structure from the design: orchestrator, DB layer, agent invoker, prompt templates, CLI entry point, and scheduling.

## Tasks

- [x] 1. Schema migration and data models
  - [x] 1.1 Create `migrations/003_dream_cycle.sql` with `dream_cycle_runs` table, `dream_cycle_candidates` table, and `ALTER TABLE memory_relationships ADD COLUMN expired_at TIMESTAMPTZ`
    - Include all columns from the design: id, run_type, started_at, completed_at, explorer_output, explorer_feedback_injected, candidates_generated/accepted/deferred/rejected, digest for runs
    - Include all columns for candidates: id, run_id, candidate_json, operation, target_memory_id, schema_operation, evaluator verdicts/reasoning (a/b/c), final_verdict, created_memory_id, user_rejected_at, user_rejection_reason, deferred_twice_rejected, created_at
    - Add referential integrity constraints (run_id → dream_cycle_runs, created_memory_id → memories)
    - Add indexes on run_type, final_verdict, created_at for query performance
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

  - [x] 1.2 Create `src/models.py` with data model dataclasses: `DreamCycleResult`, `MemorySlice`, `CandidateInsight`, `EvaluatorVerdict`
    - Follow the exact field definitions from the design document
    - Include type annotations and default values
    - _Requirements: 1.4, 5.1, 6.5_

- [x] 2. Dream Cycle database layer
  - [x] 2.1 Create `src/dream_cycle_db.py` with run lifecycle functions: `create_run`, `complete_run`, `store_candidate`
    - `create_run(run_type)` inserts a dream_cycle_runs record, returns UUID
    - `complete_run(run_id, stats, digest)` updates with completed_at, stats, digest
    - `store_candidate(run_id, candidate, verdicts, final_verdict, created_memory_id)` inserts a dream_cycle_candidates record
    - _Requirements: 1.1, 1.5, 1.6, 2.5_

  - [x] 2.2 Implement feedback and deferred candidate queries: `get_recent_rejections`, `get_deferred_candidates`
    - `get_recent_rejections(n_cycles=3)` queries last N cycles' rejected/deferred candidates with evaluator reasoning
    - `get_deferred_candidates(previous_run_id)` gets DEFERRED candidates from a specific run
    - _Requirements: 4.1, 10.1, 10.2_

  - [x] 2.3 Implement session and metrics functions: `should_run_briefing`, `get_last_briefing_time`, `get_memory_stats`, `mark_user_rejected`, `get_golden_queries`
    - `should_run_briefing()` checks 24h gap AND (new memories OR dream cycle ran)
    - `get_memory_stats()` returns total count, date range, type distribution, recent activity
    - `mark_user_rejected(candidate_id, reason)` records rejection without deleting
    - `get_golden_queries()` extracts "Questions this answers" from accepted insights
    - _Requirements: 11.5, 11.6, 14.5, 18.1, 18.2, 18.3_

  - [x] 2.4 Implement tier metrics functions: `get_tier1_metrics`, `get_tier2_metrics`
    - `get_tier1_metrics(n_cycles=10)` computes acceptance rate, acceptance rate trend, deferred-to-accepted conversion rate, Explorer strategy diversity, cost efficiency — all via SQL aggregates on dream_cycle_runs and dream_cycle_candidates
    - `get_tier2_metrics(n_cycles=10)` computes user rejection rate, rejection reason clustering, insight citation rate via access_count on dream-cycle-tagged memories
    - _Requirements: 18.1, 18.2_

  - [x] 2.5 Write unit tests for `src/dream_cycle_db.py` in `tests/test_dream_cycle_db.py`
    - Test create_run returns valid UUID and sets started_at
    - Test complete_run updates all stats fields
    - Test store_candidate with all verdict combinations
    - Test get_recent_rejections returns correct cycles with reasoning
    - Test get_deferred_candidates filters by run_id and DEFERRED status
    - Test should_run_briefing enforces 24h cap and new-content condition
    - Test mark_user_rejected preserves memory, records timestamp and reason
    - _Requirements: 1.1, 1.5, 1.6, 4.1, 10.1, 11.5, 11.6, 14.5_

- [x] 3. Agent Invoker
  - [x] 3.1 Create `src/agent_invoker.py` with `AgentInvoker` class: `invoke` and `parse_json_output` methods
    - `invoke(system_prompt, user_message, mcp_config, timeout)` spawns `kiro --no-interactive` subprocess
    - Passes system prompt via `--system-prompt` flag, user message via stdin
    - Passes MCP config via `--mcp-config` flag when provided
    - Enforces timeout, captures stdout/stderr, raises on crash/timeout with stderr
    - `parse_json_output(raw)` extracts JSON from agent output, handles markdown code fences (`\`\`\`json ... \`\`\``)
    - Returns `AgentResponse` (or dict) with parsed output and raw text
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5_

  - [x] 3.2 Write property test for JSON output parsing in `tests/test_agent_invoker.py`
    - **Property 13: JSON Output Parsing**
    - Test that for any valid JSON wrapped in markdown fences, bare, or with surrounding text, `parse_json_output` extracts the correct JSON
    - **Validates: Requirement 13.3**

  - [x] 3.3 Write unit tests for `AgentInvoker` in `tests/test_agent_invoker.py`
    - Test invoke with mock subprocess returning valid JSON
    - Test invoke timeout raises exception with stderr
    - Test invoke crash raises exception
    - Test MCP config passed to Explorer/Thinker, not to evaluators
    - _Requirements: 13.1, 13.2, 13.4, 13.5_

- [x] 4. Prompt templates
  - [x] 4.1 Create `src/prompts/__init__.py`, `src/prompts/explorer.py`, `src/prompts/thinker.py`, `src/prompts/panel.py`
    - `explorer.py`: `get_explorer_prompt(memory_count, date_range, feedback_injection, run_type, scope)` — full Explorer prompt from design doc with variable interpolation
    - For session_start mode, restrict strategies to 6, 8, 10 only
    - For post_learn mode, scope exploration to new insights and neighbors
    - `thinker.py`: `get_thinker_prompt()` — static Thinker prompt from design doc
    - `panel.py`: `get_evaluator_prompt(role, candidate_json, source_memories_content)` — evaluator prompts for skeptic, advocate, epistemologist
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

  - [x] 4.2 Write property test for prompt template interpolation in `tests/test_prompts.py`
    - **Property 15: Prompt Template Interpolation**
    - Test that for any valid interpolation variables, Explorer prompt contains all interpolated values
    - Test that session_start mode restricts strategies to 6, 8, 10
    - **Validates: Requirements 17.2, 17.4, 17.5**

- [x] 5. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Orchestrator core pipeline
  - [x] 6.1 Create `src/dream_cycle.py` with `DreamCycleOrchestrator` class skeleton and `run` method + `invoke_explorer`
    - `run(run_type, scope)` implements the main orchestration algorithm skeleton: create run record, build feedback, invoke Explorer, circuit breaker check, then delegate to Thinker/Panel (wired in 6.3)
    - Handles circuit breaker (empty slices → abort early, complete run with 0 candidates)
    - `invoke_explorer(feedback, run_type, scope)` builds Explorer prompt via template, invokes via AgentInvoker, parses MemorySlice list
    - _Requirements: 1.1, 1.2, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 9.1_

  - [x] 6.2 Implement `build_feedback_injection` method
    - Queries last 3 cycles' rejections via `get_recent_rejections`, formats as "Lessons from recent cycles" text block with actual evaluator reasoning (not just counts)
    - Returns empty string if no previous cycles exist
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 6.3 Implement `invoke_thinker` method
    - Builds Thinker input with memory slice content + any deferred candidates with dissenting objections
    - Invokes via AgentInvoker with MCP config, parses CandidateInsight list
    - _Requirements: 1.2, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [x] 6.4 Implement `invoke_evaluator` method and wire the pipeline loop in `run`
    - `invoke_evaluator(candidate, role)` builds evaluator prompt via template, invokes via AgentInvoker (no MCP config), parses EvaluatorVerdict
    - Wire the full pipeline loop in `run`: for each slice → invoke_thinker → for each candidate → invoke 3 evaluators → tally consensus → store/defer/reject
    - _Requirements: 1.3, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 6.5 Implement consensus tallying: `tally_consensus` method
    - 3/3 ACCEPT → ACCEPTED, 2/3 ACCEPT → DEFERRED, else → REJECTED
    - Pure function, no side effects
    - _Requirements: 1.4, 2.1, 2.2, 2.3, 2.4_

  - [x] 6.6 Write property test for consensus tally in `tests/test_consensus.py`
    - **Property 1: Consensus Tally Correctness**
    - For any list of 3 verdicts (each ACCEPT or REJECT), verify tally returns exactly one of ACCEPTED/DEFERRED/REJECTED with correct mapping
    - **Validates: Requirements 1.4, 2.1, 2.2, 2.3**

  - [x] 6.7 Write property test for circuit breaker in `tests/test_dream_cycle.py`
    - **Property 5: Circuit Breaker**
    - For any run where Explorer returns 0 slices, verify candidates_generated = 0, aborted_early = TRUE, and no Thinker or Panel invocations occur
    - **Validates: Requirements 9.1, 9.2, 9.3**

  - [x] 6.8 Write property test for evaluator independence in `tests/test_dream_cycle.py`
    - **Property 6: Evaluator Independence**
    - For any candidate evaluation, verify no evaluator's prompt contains another evaluator's verdict or reasoning, and evaluator agents receive no MCP config
    - **Validates: Requirements 2.4, 13.4**

  - [x] 6.9 Implement deduplication: `check_duplicate` method
    - Generate embedding for candidate content
    - Search active non-chunk memories (parent_id IS NULL, status='active') for similarity > 0.85
    - Return existing memory ID if found, None otherwise
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [x] 6.10 Write property test for deduplication in `tests/test_consensus.py`
    - **Property 2: Deduplication Guarantee**
    - For any content string, check_duplicate returns an ID only when similarity > 0.85 against active non-chunk memories
    - **Validates: Requirements 7.2, 7.3, 7.4**

  - [x] 6.11 Implement memory storage operations: `store_accepted` method
    - CREATE: new memory with embedding, tags ['dream-cycle', schema_operation], metadata with strategy/source_memories/confidence
    - UPDATE: update target memory content, re-embed
    - SUPERSEDE: create new memory, set old status='superseded', create 'superseded_by' relationship, preserve old relationships
    - Downgrade SUPERSEDE to CREATE if target doesn't exist or is already superseded
    - Create all proposed relationships for each accepted insight
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 15.1, 15.2, 15.5_

  - [x] 6.12 Write property test for SUPERSEDE consistency in `tests/test_dream_cycle.py`
    - **Property 7: SUPERSEDE Consistency**
    - For any accepted SUPERSEDE, verify old memory status='superseded', superseded_by relationship exists, old relationships preserved
    - **Validates: Requirements 8.3, 8.4, 15.2, 15.5**

  - [x] 6.13 Write property test for CREATE storage correctness in `tests/test_dream_cycle.py`
    - **Property 11: CREATE Storage Correctness**
    - For any accepted CREATE candidate, verify memory has embedding, correct tags, correct metadata fields, and all relationships created
    - **Validates: Requirements 8.1, 8.5**

- [x] 7. Deferred insight re-evaluation and two-strike rule
  - [x] 7.1 Implement deferred insight handling in the orchestrator
    - Query deferred candidates from previous run
    - Pass deferred candidates to Thinker with dissenting objection as context
    - Track second-deferral: if candidate was already deferred once, mark as REJECTED with `deferred_twice_rejected = TRUE`
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [x] 7.2 Write property test for two-strike expiration in `tests/test_dream_cycle.py`
    - **Property 4: Two-Strike Expiration**
    - For any candidate deferred in cycle N that fails consensus in cycle N+1, verify it is marked REJECTED with deferred_twice_rejected=TRUE
    - **Validates: Requirements 10.4, 18.4**

- [x] 8. Execution mode scoping
  - [x] 8.1 Implement execution mode logic in the orchestrator's `run` method
    - scheduled: full pipeline, all 11 strategies
    - post_learn: scope Explorer to new insights + neighbors
    - session_start: limit Explorer to strategies 6, 8, 10; limit Thinker to 1-2 candidates; enforce 24h frequency cap via `should_run_briefing()`
    - user_triggered: scope Explorer to user-specified topic, all strategies
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

  - [x] 8.2 Write property test for session-start frequency cap in `tests/test_dream_cycle.py`
    - **Property 8: Session-Start Frequency Cap**
    - For any session_start attempt, verify execution only when 24h gap AND (new memories OR dream cycle ran)
    - **Validates: Requirements 11.5, 11.6**

- [x] 9. Digest generation
  - [x] 9.1 Implement `generate_digest` method on the orchestrator
    - Write static markdown to `logs/dream-cycle-digest-{date}.md`
    - Group accepted insights by strategy type (not confidence)
    - Include 1-line summary, full content, source memory links, evaluator reasoning per accepted insight
    - Show diff with link to original for UPDATE/SUPERSEDE operations
    - Include run statistics (generated/accepted/deferred/rejected)
    - Include Explorer strategies used and feedback loop summary
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

  - [x] 9.2 Write property test for digest completeness in `tests/test_dream_cycle.py`
    - **Property 12: Digest Completeness**
    - For any set of accepted/deferred/rejected candidates, verify digest groups by strategy, includes required sections, shows diffs for UPDATE/SUPERSEDE
    - **Validates: Requirements 12.2, 12.3, 12.4, 12.5, 12.6**

- [x] 10. Error handling
  - [x] 10.1 Implement error handling in the orchestrator for all failure scenarios
    - Explorer crash/timeout: log error, complete run with 0 candidates, exit cleanly
    - Thinker failure (single slice): skip slice, log error, continue remaining slices
    - Evaluator crash/timeout: treat as REJECT, continue with remaining evaluators
    - Database unreachable: log error, exit with code 2
    - SUPERSEDE target not found: downgrade to CREATE, log warning
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5_

  - [x] 10.2 Write unit tests for error handling in `tests/test_dream_cycle.py`
    - Test Explorer failure completes run with 0 candidates
    - Test Thinker failure for one slice continues processing others
    - Test evaluator timeout treated as REJECT
    - Test SUPERSEDE target missing downgrades to CREATE
    - _Requirements: 16.1, 16.2, 16.3, 16.5_

- [x] 11. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. User rejection and memory lifecycle
  - [x] 12.1 Implement user rejection flow: `mark_user_rejected` integration in orchestrator
    - Set memory status to 'user_rejected', store reason
    - Record rejection timestamp in dream_cycle_candidates
    - Ensure rejected insight feeds back into next cycle's feedback injection
    - _Requirements: 14.5, 15.1, 15.4_

  - [x] 12.2 Write property test for user rejection preservation in `tests/test_dream_cycle.py`
    - **Property 14: User Rejection Preserves Memory**
    - For any user rejection, verify memory exists with status='user_rejected', rejection timestamp and reason recorded, memory not deleted
    - **Validates: Requirements 14.5, 15.4**

- [x] 13. CLI entry point and scheduling
  - [x] 13.1 Create `scripts/dream_cycle_run.py` CLI entry point
    - Accept `--run-type` argument (scheduled, post_learn, session_start, user_triggered)
    - Accept optional `--topic` argument for user_triggered mode
    - Accept optional `--memory-ids` argument for post_learn mode
    - Use `scripts/job_wrapper.sh` for macOS notification on failure
    - Exit codes: 0 success, 1 partial failure, 2 total failure
    - _Requirements: 19.2, 19.3, 19.4, 19.5_

  - [x] 13.2 Create `scheduling/com.second-brain.dream-cycle.plist` launchd job
    - Weekly Sunday 4AM execution
    - Run `scripts/dream_cycle_run.py --run-type scheduled`
    - Use job_wrapper.sh for failure notifications
    - _Requirements: 19.1_

  - [x] 13.3 Create `scripts/golden_queries.py` for Tier 3 metrics
    - Extract "Questions this answers" from accepted dream cycle insights
    - Run hybrid_search + rerank for each golden query
    - Log rank position of the dream-cycle insight and co-results
    - _Requirements: 18.3_

- [x] 14. Integration wiring and run record completeness
  - [x] 14.1 Wire all components together in `src/dream_cycle.py`
    - Ensure orchestrator imports and uses dream_cycle_db, agent_invoker, prompts, models
    - Ensure run record is always completed (even on error paths)
    - Ensure candidate counts are accurate: generated = accepted + deferred + rejected
    - Ensure every candidate has a dream_cycle_candidates record with all 3 evaluator verdicts
    - _Requirements: 1.5, 1.6, 2.5_

  - [x] 14.2 Write property test for run record completeness in `tests/test_dream_cycle.py`
    - **Property 10: Run Record Completeness**
    - For any completed run, verify completed_at is non-null, candidate counts sum correctly, every candidate has all 3 verdicts stored
    - **Validates: Requirements 1.5, 1.6, 2.5**

  - [x] 14.3 Write integration test for full pipeline with mock agents in `tests/test_integration.py`
    - Mock AgentInvoker to return deterministic Explorer/Thinker/Panel outputs
    - Run orchestrator end-to-end, verify all DB records created correctly
    - Verify feedback injection, deduplication, consensus, storage, digest
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 7.1, 8.1, 12.1_

- [x] 15. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- The design uses Python throughout — all implementation in Python
- Existing `src/db.py` functions (create_memory, update_memory, search_similar, create_relationship) are reused by the orchestrator's storage operations
- Agent invocation uses `kiro --no-interactive` subprocess pattern — no direct LLM API calls
- The dream cycle depends on V2 Tasks 3-6 for deterministic enrichment but can run without them
