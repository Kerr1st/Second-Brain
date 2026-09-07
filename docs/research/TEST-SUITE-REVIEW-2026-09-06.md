# Second Brain test-suite review — September 6, 2026

## Recommendation

Keep the broad protection, but simplify its implementation and strengthen what a passing result proves. There is no evidence that an arbitrary target such as 500 tests would improve the project. The suite is already fast. Its important weaknesses are copied logic, missing production boundaries, inconsistent database setup, and the gap between software checks and useful AI outcomes.

**First priority: reliable test isolation and tests that detect broken behavior. Second: remove proven duplication. Third: connect one real Codex/job-search lifecycle to an outcome evaluation.** No production code or existing tests were edited during this review.

## Scope and measured baseline

Reviewed commit `8c0d58ca45194a5ebc3160af8f6f59a521357dda`. Inventoried all 60 test modules, collected and ran the complete suite, measured statement/branch coverage, inspected critical paths and candidate duplication, and ran targeted process-local fault experiments. This is not an assertion-by-assertion certification of all 912 cases or an exhaustive mutation analysis.

| Measurement | Result | Interpretation |
|---|---:|---|
| Collected cases | 912 | Parameterized cases count separately; randomized examples do not |
| Test modules | 60 | 20,163 lines in `test_*.py` files |
| Functions using Hypothesis `given` | 127 | Randomized testing is substantial, but quality depends on the invariant |
| Complete instrumented run | 912 passed, 22.47 seconds | Runtime is not currently the main problem |
| Executable statements in `src` exercised | 3,797 / 4,600 — 82.5% | Executing a line does not prove its result was checked |
| Branches in `src` exercised | 1,033 / 1,456 — 70.9% | Important rejection/error paths remain unexercised |
| Warnings in instrumented run | 139 | Many unclosed SQLite connection warnings; not 139 independent defects |

Coverage's combined statement/branch figure is 79.8%; it must not be described as 80% line coverage. Coverage was measured for `src`, not all scripts or migration utilities. The full run used the disposable `second_brain_codex_test` database with both database environment variables explicitly set. Ordinary non-coverage fault runs emitted one existing Starlette deprecation warning.

The 102 migration-file cases collectively took roughly 0.06 seconds. The slowest individual test took 2.08 seconds. Large case counts do not identify the expensive or low-value areas.

Local detailed evidence: `evaluations/results/2026-09-06-test-review/` contains `results.xml`, `coverage.json`, `inventory.json`, baseline and fault logs, the fault plugin, and `probes.json`. These artifacts are ignored by Git. The findings and reproducible commands are recorded here so the conclusions do not depend on committing private execution output.

## Findings that matter most

### 1. Broken Explorer and Thinker invocation can leave the entire suite green

**Evidence:** Neither production body, `invoke_explorer` nor `invoke_thinker`, executed in the baseline: respectively 0/29 and 0/20 executable statements. See `src/dream_cycle/orchestrator.py:390` and `:487`.

In an isolated pytest process, both methods were replaced with functions that immediately raise an assertion error. **All 912 tests still passed in 23.00 seconds.** Existing tests replace these methods with their own mocks before using the orchestrator.

`tests/test_prompt_contracts.py:35` makes this particularly clear: its helper explicitly replicates the Thinker payload construction. Three tests verify this copy and JSON serialization, rather than the message the production method sends. A payload regression in the application would not necessarily affect the copy.

**Action:** Replace those three copied-payload tests with a small parameterized contract that calls the actual production invocation methods. Substitute the external agent invoker only. Assert the outgoing prompt, source fields, tool configuration, returned object parsing, and behavior for malformed/empty results. Do not call a paid model in these deterministic tests. Keep orchestrator unit tests, but do not let them stand in for this boundary.

**Acceptance:** Disabling either production method or dropping a required payload field must fail the relevant test.

### 2. The context tests do not protect against guidance applying outside its intended scope

`src/context_broker.py:84` checks integration, Semantic Project, repository, and topic applicability. The baseline did not execute its rejection returns. Replacing `_rule_applies` with an unconditional `True` left **all seven context-broker tests passing**.

This proves a coverage gap, not a live cross-project leak. The existing exact-topic/status/project expansion test remains valuable, but it tests a different filter from approved guidance applicability.

**Action:** Add a compact applicability decision table: matching scope; wrong integration; wrong Semantic Project; wrong/missing repository; wrong topic; malformed scope data; unrestricted scope. At least one rejection should be asserted through `build_context` with real database rows so a correct helper cannot mask a broken caller. Specify the intended malformed-input behavior before asserting it.

Also cover invalid context requests, conflicting selected memories, and outcome receipts that reference an unknown receipt or a memory that was not delivered. Several related guards are presently unexercised.

**Acceptance:** The unconditional-allow fault must fail. Valid matching guidance must still arrive first. Workspace location must not substitute for Semantic Project.

### 3. Three local database fixtures bypass the shared isolation guard

`tests/test_ingest_doc_chunks.py:157`, `tests/test_ingest_eventlog.py:157`, and `tests/test_enrich_qd_tags.py:225` override the shared `test_db` fixture.

Unlike `tests/conftest.py`, they neither allowlist the database name nor redirect the application's `src.db.DB_CONFIG`. Their connection follows `TEST_DB_NAME`; application helpers follow `DB_NAME`, which defaults to `memory_bank`. Running one of these files independently can therefore query/clean one database while the application writes another. They also skip on a database connection error, whereas a required integration check should fail clearly when its provisioned database is unavailable.

A probe with `psycopg2.connect` replaced by a recording mock confirmed that all three accepted an unapproved database name and left application configuration unchanged. **No real connection was made during this probe.** The complete review run explicitly aligned both variables, so it did not test this hazard against live data.

**Action:** Use one guarded setup fixture and a separate connection fixture. Adapt callers because the shared fixture yields configuration while these local fixtures yield connections. Preserve explicit cleanup and pool restoration. Establish database isolation before adding parallel workers; the current suite recreates a shared database and is not safely parallelizable as-is.

**Acceptance:** Any individual integration file runs against the same disposable database as the full suite. Unapproved database names are rejected before connecting or deleting, and missing required infrastructure is an error rather than a silent skip.

### 4. Evaluation completion can look successful without a successful evaluation

`scripts/eval/run_evaluation.py` catches tier exceptions, records an error, prints completion, and returns zero. Replacing the curated tier with a function that raises confirmed **exit status 0 after an evaluation failure**.

The committed `evaluations/query_sets/seed.json` has 31 entries with placeholder IDs, including its instruction row. The curated evaluator skips them all; directly evaluating this file returned `{"total": 0}`. This does not establish that every evaluation source is empty: other tiers can load golden queries from the database.

**Action:** Define distinct success, failed, and insufficient-evidence outcomes. Return a nonzero process status for tier exceptions and for required benchmarks with no usable cases. Make case counts and exclusions prominent. Add deterministic tests for these runner outcomes; the current `--cov=src` scope does not measure the runner.

Populate a small, versioned benchmark with stable fixtures or a documented mapping to a frozen private corpus. Preserve original evidence, expected current state, allowed uncertainty, and dated outcomes. Do not make an empty dataset pass a quality gate.

### 5. “End-to-end” currently means different things across the suite

`tests/test_integration.py:174` mocks Explorer, Thinker, Evaluator, database operations, storage, retrieval, embeddings, and digest writing. Its 16 cases are useful orchestration/component tests, but they cannot establish that those pieces work together in the running application.

There are stronger partial integrations already: `tests/test_codex_capture.py:717` covers native source capture, persisted segments/memories/provenance, and retrieval; the correction test at `:771` also uses the MCP search function. Keep these. Extend an existing real boundary rather than rebuilding the same setup under another “E2E” label.

**Action:** Establish one deterministic Codex lifecycle test from a realistic native source fixture through capture, semantic persistence, real database retrieval, context delivery, and a recorded outcome. Fake only external model/embedding responses. Verify exact provenance and the later correction, not merely a nonempty result. Keep live model quality evaluation separate: deterministic response fixtures cannot prove that a model will identify the right memory or help a person.

## What to remove, consolidate, or retain

| Area | Concrete evidence | Recommendation |
|---|---|---|
| Five consensus duplicates | `tests/test_consensus.py:378–406` repeat the same inputs/assertions as earlier explicit 4-, 3-, 2-, 1-, and 0-accept tests | Remove the five repeated cases after confirming the retained cases still detect threshold faults |
| Document-chunk “nonempty” property | `tests/test_ingest_doc_chunks.py:145` only asserts `len(text.strip()) >= 0`; it never calls the application | Remove this vacuous test; add real whitespace/filter behavior coverage if required |
| Three Thinker payload tests | Assert a helper that copies production payload construction | Replace with tests calling the actual production method |
| Consensus truth table | Sixteen separately written binary vote permutations | Parameterize all 16 rows in one readable table; preserve every permutation and invalid-length behavior |
| Markdown header checks | Nine title/source combinations assert the same separator; other cases check individual constant fields | Consolidate around complete header and round-trip behavior, preserving empty/long/special-character boundaries |
| Reranking drift tests | The retrieval setup and most branch-presence checks are repeated in two functions | Combine into one production-vs-evaluation contract, retaining the first test's additional signal/type coverage assertions |
| Evaluation “identity” property | `tests/test_eval_properties.py:95` compares two runs of the same function | It tests determinism, not preservation of production scores; rename or replace using the existing real scorer agreement contract |
| Refactor-history checks | `tests/test_dream_cycle.py:2369` enforces an exact set of private methods; search tests inspect import strings | Retire private implementation-shape checks unless an architectural constraint still needs them; retain meaningful public API compatibility |
| Backend adapters | Codex/Claude tests repeat timeout, process error, cleanup, and usage contracts | Consider shared parameterized adapter contracts; retain backend-specific command flags, envelopes, tool probes, and failure cases |
| Approval atomicity/concurrency | Eight real database cases exercise distinct write failures, embedding failure, and competing approvals | **Keep all eight.** Similar structure protects different failure boundaries |
| Capture ownership/provenance | Native source fixtures, delegated/unknown exclusions, refresh, cursor retry, correction links | **Keep.** These prevent contamination and preserve source attribution |
| Migration runner and retrieval properties | Fresh schema, concurrent migration, ordering/deduplication/filter contracts | **Keep.** They protect actual behavior, even when several tests execute the same lines |

The firm immediate deletion shortlist is **six cases: five duplicates and one vacuous assertion**. Larger reductions need assertion-by-assertion consolidation, not blanket removal by filename or coverage overlap. Parameterization reduces maintenance code without necessarily reducing the collected case count. I would not promise a reduction of hundreds based on this review.

The two invalid-length consensus property tests have similar bodies but different domains: too few versus too many votes. They are not exact redundant cases. Likewise, multiple approval failure cases should not be collapsed into a single write failure just because their setup is similar.

As a positive control, changing the consensus acceptance threshold from three votes to two produced **8 failures and 24 passes** in its 32-case module. The existing suite does detect that important defect. Keep the truth-table protection while removing repeated implementations. This tests quorum arithmetic; it does not establish independence or correctness of AI evaluators.

## Additional missing coverage, prioritized by use

| Production area | Measured gap | Next useful test |
|---|---|---|
| Dream-cycle database | `get_memory_stats`, `get_golden_queries`, `extract_golden_queries`, and `expire_stale_relationships` bodies unexecuted | Real database contract for data consumed by the Explorer/evaluation runner; age boundary and active/expired relationship behavior |
| Context broker | 66/100 branches exercised | Scope denial, conflicts, invalid receipts, absence of usable evidence |
| Dream-cycle orchestration | 21/44 branches exercised | Real invocation contracts first; avoid adding more mocks of already-mocked methods |
| Crawlee parser | 0/100 executable statements; 0/50 branches | Temporary-file contract for index/header/fallback parsing and already-processed filtering, if this supported scheduled ingestion path is retained |
| YouTube capture | 44.2% statement coverage | Failure/empty-transcript handling if used; lower priority than Codex reference integration |
| SQLite resource handling | ResourceWarnings in instrumented run | Trace allocation sites, close connections explicitly, then enforce targeted resource-warning cleanliness |

A low percentage alone does not justify a new test. Choose an active user-visible failure mode first. Retiring an unused integration is preferable to manufacturing tests for unused code, but this review has not established that Crawlee or YouTube is unused.

## A simpler overall approach

Use three clearly named layers. Register markers and document which dependencies each layer may access; do not infer the layer from whether a filename contains “integration.” Pytest supports registered markers and selection with `-m` ([official documentation](https://docs.pytest.org/en/stable/how-to/mark.html)).

1. **Behavior tests:** pure logic and actual adapter/prompt boundaries with fake external services. Deterministic, no network or live models. Keep useful properties for arbitrary text, order invariance, deduplication, and boundary conditions; use explicit examples where random input cannot change the asserted behavior.
2. **Integration tests:** disposable PostgreSQL/pgvector, temporary source files, real storage/retrieval/context/receipt paths. Include a small number of full deterministic lifecycles. Run both layers on every PR; the current full run is already only about 23 seconds.
3. **AI outcome evaluations:** a small frozen real-work set plus unseen holdouts. Run deliberately after changes to capture, distillation, ranking, prompts, or delivery. Record model/configuration, source corpus date, retrieved evidence, answer, decision, and outcome. A passing unit suite cannot substitute for this layer.

There is no committed `.github` test workflow in this checkout. A green external code-review check is not evidence that pytest ran. After fixture isolation is corrected, add a reproducible PR job with PostgreSQL/pgvector and no live-service credentials. Prefer a reliable complete run over selective test machinery or parallel workers at this size.

For the first real-work evaluation, continue with Job Search. Extend the existing correction/current-state cases; include an untouched second scenario. Score whether the system recovered the latest supported state, respected scope and provenance, proposed the right next action, admitted missing evidence, and reduced human rework. Record “not used” or “unknown” when appropriate. A retrieval hit alone is insufficient, and an agent-written “followed” receipt is not independent proof of utility.

The September 6 [current-state recall report](CURRENT-STATE-RECALL-2026-09-06.md) records both a repaired example and a remaining ownership/coverage limitation. Preserve that distinction. Freeze expected outcomes before altering implementation, retain the original failed results, and keep a holdout case that is not used to tune the repair.

This balanced use of small behavior tests, integration checks, and a few complete journeys is consistent with [Google's testing guidance](https://testing.googleblog.com/2015/04/just-say-no-to-more-end-to-end-tests.html). Coverage contexts identify which test executed a line; they do not prove assertion strength or redundancy ([coverage.py documentation](https://coverage.readthedocs.io/en/latest/contexts.html)).

## Incremental implementation order and completion criteria

**Change 1 — Make a passing check trustworthy.** Unify guarded database fixtures; replace copied Thinker checks and add real Explorer invocation checks; cover scope rejection; make evaluation errors/empty required datasets visible. Keep these changes reviewable, splitting the fixture and behavior fixes if needed. Completion: each relevant deliberate fault is caught; an individual integration module is safely reproducible; evaluation failure cannot be mistaken for success.

**Change 2 — Remove confirmed repetition.** Delete the six cases above, parameterize consensus, consolidate repeated contracts with all unique assertions retained, and retire obsolete private-shape checks. Completion: materially less repeated setup/assertion code; preserved behavioral coverage and fault detection; no production changes made merely to keep old tests passing.

**Change 3 — Prove one lifecycle and automate it.** Extend Codex capture integration through context/outcome, add a usable frozen job-search benchmark and holdout, document the three layers, then add the PR test job. Completion: real components connect under deterministic tests, and separate outcome evidence says where the AI helps or fails.

Success should be judged by defect detection, safe isolation, understandable failures, low duplicated maintenance, reproducibility, and measured real-work benefit. Test count and coverage percentages remain diagnostics, not goals.

## Reproduction and limitations

Baseline command (requires provisioned disposable PostgreSQL/pgvector):

```sh
DB_NAME=second_brain_codex_test TEST_DB_NAME=second_brain_codex_test \
  .venv/bin/python -m pytest --cov=src --cov-branch --cov-context=test \
  --cov-report=json:evaluations/results/2026-09-06-test-review/coverage.json \
  --durations=25 \
  --junitxml=evaluations/results/2026-09-06-test-review/results.xml
```

The fault plugin replaces methods only in an isolated pytest process, before collection. Invocation mode raises immediately in both `DreamCycleOrchestrator.invoke_explorer` and `.invoke_thinker`; applicability mode returns `True` from `_rule_applies`; quorum mode changes the acceptance threshold from 3 to 2 while preserving invalid-length rejection. These changes were never written into `src`. Run invocation mode against the full suite, applicability against `tests/test_context_broker.py`, and quorum against `tests/test_consensus.py`. Their logs record the results above.

No live AI-provider quality run, live approval mutation, production database fault injection, or exhaustive test-removal experiment was performed. The fault experiments establish weaknesses in the tests, not an observed incident in normal operation. The review does not certify every redundant-looking case for deletion. The appendix is an inventory, not a claim that every assertion was manually reviewed.

## Appendix: collected cases by module

| Module | Collected cases | File lines |
|---|---:|---:|
| `test_migration` | 102 | 855 |
| `test_dream_cycle` | 53 | 2464 |
| `test_codex` | 52 | 783 |
| `test_claude_code` | 48 | 698 |
| `test_capture_api` | 45 | 411 |
| `test_dream_cycle_db` | 41 | 1039 |
| `test_project` | 41 | 503 |
| `test_codex_capture` | 34 | 1162 |
| `test_express` | 33 | 481 |
| `test_consensus` | 32 | 413 |
| `test_agent_invoker` | 25 | 546 |
| `test_resolver` | 24 | 230 |
| `test_ide_chat` | 21 | 184 |
| `test_mcp_probe` | 19 | 234 |
| `test_project_properties` | 19 | 834 |
| `test_mcp_server` | 17 | 464 |
| `test_prompt_contracts` | 17 | 303 |
| `test_search_properties` | 17 | 278 |
| `test_enrich_qd_tags` | 16 | 324 |
| `test_integration` | 16 | 454 |
| `test_slack_graph` | 15 | 260 |
| `test_rerank` | 14 | 407 |
| `test_search` | 14 | 429 |
| `test_qd_migration` | 12 | 164 |
| `test_question_search` | 12 | 755 |
| `test_backends` | 11 | 85 |
| `test_claude_code_stream_json_probe_preservation` | 11 | 331 |
| `test_distill` | 11 | 140 |
| `test_ingest_doc_chunks` | 11 | 222 |
| `test_ingest_eventlog` | 11 | 222 |
| `test_prompts` | 10 | 272 |
| `test_ingest_interactions` | 9 | 199 |
| `test_steering_approval` | 8 | 196 |
| `test_context_broker` | 7 | 198 |
| `test_dream_cycle_run` | 6 | 57 |
| `test_embeddings` | 6 | 77 |
| `test_panel_prompts` | 6 | 109 |
| `test_pool_properties` | 6 | 307 |
| `test_cli_chat` | 5 | 73 |
| `test_db` | 5 | 147 |
| `test_agent_task_schema` | 4 | 94 |
| `test_eval_properties` | 4 | 108 |
| `test_capture_youtube` | 3 | 57 |
| `test_codex_canary_job` | 3 | 88 |
| `test_codex_capture_job` | 3 | 91 |
| `test_depth` | 3 | 139 |
| `test_digest_properties` | 3 | 209 |
| `test_feedback_properties` | 3 | 192 |
| `test_ingest_v2` | 3 | 300 |
| `test_orchestrator_properties` | 3 | 320 |
| `test_schema_migration_runner` | 3 | 237 |
| `test_steering` | 3 | 171 |
| `test_capture_codex_cli` | 2 | 63 |
| `test_claude_code_stream_json_probe` | 2 | 187 |
| `test_reembed_memories` | 2 | 81 |
| `test_rerank_drift` | 2 | 317 |
| `test_classify` | 1 | 82 |
| `test_reembed_job` | 1 | 42 |
| `test_steering_publisher` | 1 | 50 |
| `test_test_database_safety` | 1 | 25 |
