# 12b — Tests: mock-shaped tests and near-duplicates (dream_cycle / orchestrator / pool)

Second-pass, narrow-scope review. HEAD 6b8c2d2. Read-only; pytest NOT run.

Scope: `tests/test_dream_cycle.py`, `tests/test_dream_cycle_db.py`,
`tests/test_orchestrator_properties.py`, `tests/test_pool_properties.py`.

## Findings


### (1) Metrics (AST-derived, `python3 -c ast` over the four files; no pytest)

Method: test = `def test_*`/`async def test_*`; "behavioural assert" = bare `assert` + `self.assert*(`; "mock assert" = `.assert_called*/assert_not_called/assert_any_call/assert_has_calls/assert_awaited*`; "mock use" = `patch(`/`patch.object(`/`patch.dict(`/`MagicMock(`/`Mock(`/`AsyncMock(` in the body **or** decorators.

| File | Lines | Tests | Behav. asserts | Mock asserts | Asserts/test | Mock uses | Mock uses/test | Tests using mocks |
|---|---|---|---|---|---|---|---|---|
| tests/test_dream_cycle.py | 2723 | 60 | 228 | 39 | 3.80 | 257 | 4.28 | 55/60 |
| tests/test_dream_cycle_db.py | 1063 | 42 | 106 (6 bare + 100 `self.assert*`) | 10 | 2.52 | 247 | 5.88 | 42/42 |
| tests/test_orchestrator_properties.py | 327 | 3 | 11 | 0 | 3.67 | 20 | 6.67 | 3/3 |
| tests/test_pool_properties.py | 306 | 6 | 10 | 0 | 1.67 | 1 | 0.17 | 1/6 |

Notes:
- `test_dream_cycle_db.py` is 100 % mock-driven: every one of the 42 tests patches `src.dream_cycle_db.get_connection` and hand-wires `conn.cursor().__enter__/__exit__`. The 6-line context-manager wiring block is repeated verbatim in **every** test (see §6).
- `test_dream_cycle.py`: 2 tests have **zero** behavioural asserts and only mock-call asserts (line 676 `test_session_start_proceeds_when_briefing_allowed`, line 704 `test_non_session_start_skips_briefing_check`).
- `test_orchestrator_properties.py` has the highest mock density (6.67/test) — each of its three tests patches 6–7 collaborators of `run_dream_cycle`.


### (2) Mock-shaped tests — running list (appended as found)

Definition used: a test is "mock-shaped" if every collaborator whose behaviour matters is replaced by a mock, and the assertions merely echo the shapes the mocks were told to return, so a stub implementation that calls the same collaborators in the same order would pass.

**M1 — S2 — tests/test_dream_cycle.py:161 `TestCircuitBreakerExplicit.test_circuit_breaker_completes_run_record`**
Patches `dream_cycle_db` (whole module), `_skim_arrivals`, `invoke_explorer→[]`, `build_feedback_injection→""`. Asserts `result.run_id == "run-123"` (the value handed to `create_run.return_value`), zero counts, and `complete_run.assert_called_once_with(...)`. Proves: that `run()` forwards `create_run`'s return value into the result and calls `complete_run` with a fixed dict when there are no slices. Does *not* prove any circuit-breaker logic beyond `if not slices`. Near-duplicate of the Hypothesis test at :117 which asserts the same thing with `run_type` varied (the run_type has no effect on the asserted path).

**M2 — S3 — tests/test_dream_cycle.py:355 `TestSupersedeConsistencyProperty.test_supersede_sets_old_status_and_creates_relationship`**
Patches all 7 collaborators of `store_accepted` (`generate_embedding`, `create_memory`, `update_memory`, `get_memory`, `create_relationship`, `classify_memory`, `compute_depth_score`). Assertions are entirely on `call_args_list` of the mocks. Proves only the *call choreography* (one `update_memory(..., status="superseded")`, one `create_relationship(old,new,"superseded_by")`, N+1 relationship calls). A stub that issued those calls without touching the DB passes. It never checks what happens if `update_memory` fails, nor that `create_memory` was given the candidate's content. Reasonable as a "wiring" test, mislabelled as a property of persisted state ("verify old memory status='superseded'" — nothing about status is ever read back).

**M3 — S2 — tests/test_dream_cycle.py:676 `TestSessionStartFrequencyCap.test_session_start_proceeds_when_briefing_allowed`**
Zero behavioural asserts. Only `create_run.assert_called_once_with("session_start", backend_provenance=ANY)` and `invoke_explorer.assert_called_once()`. The `result` variable is assigned and never inspected. Proves that `run()` reaches the explorer when `should_run_briefing` is mocked `True`; nothing about "proceeds normally through the pipeline" as the docstring claims (pipeline is short-circuited by `invoke_explorer→[]`).

**M4 — S2 — tests/test_dream_cycle.py:704 `TestSessionStartFrequencyCap.test_non_session_start_skips_briefing_check`**
Zero behavioural asserts; only `should_run_briefing.assert_not_called()` inside a 3-iteration loop that re-patches everything each time. Proves a negative on a mock. Fine as intent, but combined with M3 both tests could be a single parametrised test with a real `result` assertion.

**M5 — S2 — tests/test_dream_cycle.py:753 / :802 / :846 `TestSessionStartCandidateLimiting.*`** (three tests)
Each patches 10 collaborators (`dream_cycle_db`, `_skim_arrivals`, `invoke_explorer`, `invoke_thinker`, `_ground_candidate`, `invoke_evaluator`, `build_feedback_injection`, `generate_digest`, `check_duplicate`, `store_accepted`). Assertions: `result.candidates_generated == 2/2/3` and `mock_eval.call_count == 8/12`. What is actually tested is a single `[:2]` slice in `run()`. The 3×50-line bodies differ in ~4 lines (see §6 D1). The `call_count == 8` assertion hard-codes "4 evaluators" — coupling the test to panel size, which is not the property under test.


**M6 — S3 — tests/test_dream_cycle.py:1121 `TestErrorHandling.test_explorer_failure_completes_run_with_zero_candidates`**
Identical scaffolding to M1/`:117` except `invoke_explorer` has `side_effect=TimeoutError`. Same four asserts + same `complete_run` inspection. Proves: an exception in the explorer is caught and produces the same result object as an empty slice list. That is a legitimate behaviour, but 90 % of the body is copy of `:117`; the "empty slices" and "explorer raises" cases should be a single parametrised test (`[[], TimeoutError]`).

**M7 — S2 — tests/test_dream_cycle.py:1391 `TestErrorHandling.test_supersede_target_missing_downgrades_to_create_with_warning`**
Same 7 storage patches as M2 plus `patch("src.dream_cycle.storage.logger")`. It is a strict superset of `:419 test_supersede_target_not_found_downgrades_to_create` (same candidate shape, same `get_memory→None`, same three asserts) — the only addition is `mock_logger.warning.assert_called_once()` and a substring check on the message. Two tests, ~95 lines, for one branch (`if target is None: downgrade`). The logger assertion couples the test to `logger.warning` being called exactly once — any second warning in `store_accepted` (e.g. embedding fallback) would break it for the wrong reason.

**M8 — S3 — tests/test_dream_cycle.py:457 `test_supersede_already_superseded_downgrades_to_create`**
Same 7 patches; the only variation is `get_memory→{"status":"superseded"}`. Asserts `result_id == "new-id"` (mock return) + `create_memory.assert_called_once()` + no `superseded_by` relationship. `mock_update` is captured but never asserted on — so the test does not actually verify the old memory was left alone, which is the whole point of the "already superseded" branch. Name promises "downgrades to CREATE"; the CREATE path itself (tags/metadata) is not checked either.

**M9 — S3 — tests/test_dream_cycle.py:930 `TestDigestCompletenessProperty.test_digest_contains_required_sections_and_grouping`**
Not mock-shaped in the bad sense — the digest is real and is asserted on as text. Recorded as a *good* counter-example. However, it patches `pathlib.Path.write_text` **globally** (`patch("pathlib.Path.write_text", ...)`), which will intercept any other `write_text` in the process during the test, including Hypothesis' own database writes. `.hypothesis/` uses `open()` not `write_text`, so it happens to be safe, but `tmp_path`/`monkeypatch` would be the idiomatic fix. The same global patch is repeated in `:986`, `:1019`, `:1054` (4 copies of `_capture_write`/`_patched_write_text`).

**M10 — S2 — tests/test_dream_cycle.py:1304 / :1349 `test_one_dissenting_vote_tolerated_quorum_accepts` / `test_two_dissenting_votes_rejected`**
Each patches 8–10 collaborators; the meaningful input is `mock_eval.side_effect = [4 verdicts]` and the assertions are `candidates_accepted/rejected` plus `store_candidate.call_args[0][3] == "ACCEPTED"/"REJECTED"`. What is proved is the tally rule `accepted >= 3` inside `run()`. A ~40-line body per test to exercise one comparison; the 3/4 and 2/4 cases should be one parametrised test with `(verdicts, expected)` — and the quorum function itself (if extracted) could be tested directly with zero mocks. Positional index `[0][3]` for the verdict argument is fragile: any signature change to `store_candidate` silently re-targets the assertion.


**M11 — S2 — tests/test_dream_cycle.py:1726 `TestRunRecordCompletenessProperty.test_completed_run_has_correct_counts_and_all_verdicts`**
~135 lines, 11 patches, Hypothesis-driven. Property (2) `generated == accepted + rejected` is genuinely useful. But property (3)/(4) — "every store_candidate call has all 4 evaluator verdicts" — is checked by `verdicts_dict.get("evaluator_x_verdict") is not None` on a mock's `call_args_list`; it never checks the verdicts *match* the ones fed in via `mock_eval.side_effect`, so a `run()` that stored the same verdict for every seat would pass. The test also re-implements the session_start truncation rule (`effective_entries = candidate_entries[:2]`) in the test body — the oracle is a copy of the implementation, so if the constant changes both move together and the test cannot fail. Class docstring still says "generated = accepted + deferred + rejected" (deferred verdict was removed — the header comment at :634 says so) — stale.

**M12 — S2 — tests/test_dream_cycle.py:1864 / :1931 `TestRunRecordCompletenessExplicit.test_all_accepted_run_counts_match` / `test_mixed_verdicts_run_counts_match`**
Two ~65-line explicit tests that are strict instances of the Hypothesis property at :1726 (all-ACCEPT is one draw; 4/4,2/4,1/4 is another draw). Same 10 patches, same `mock_db.*.return_value` block, same assertions. They add nothing the property does not cover; they exist because the spec template asks for "Property + Explicit" pairs. ~130 lines deletable outright.

**Also noted (not counted in the 12):**
- tests/test_dream_cycle.py:2039 `test_import_store_accepted_and_check_duplicate` — asserts two imported names are `callable`. An import error would already fail collection; the test is a tautology. Delete.
- tests/test_dream_cycle.py:2489 / :2504 `TestPublicAPIPreservation` — asserts `pkg.X is module.X` and `"X" in __all__`. Cheap, but is decomposition-era scaffolding whose failure mode (someone re-defines the class in `__init__`) is implausible.
- tests/test_dream_cycle.py:2668 `test_spend_cap_breach_aborts_run_without_retry` — does **not** patch `_ground_candidate`, unlike every other `run()` test, so the first `expensive.invoke` may be the grounding audit rather than an evaluator; the assertion `call_count == 1` is still meaningful but the docstring/comment ("One Astra evaluator call") may be describing the wrong call. Could not verify without running.

### (3) Tests asserting only on mock call counts / `assert_called_with`

| file:line | test | what is asserted | what is missing |
|---|---|---|---|
| test_dream_cycle.py:676 | `test_session_start_proceeds_when_briefing_allowed` | `create_run.assert_called_once_with(...)`, `invoke_explorer.assert_called_once()` | `result` never inspected |
| test_dream_cycle.py:704 | `test_non_session_start_skips_briefing_check` | `should_run_briefing.assert_not_called()` ×3 | return value never inspected |
| test_dream_cycle.py:565 | `test_create_with_no_relationships` | `result_id == "mem-1"` (mock return) + `create_memory.assert_called_once()` + `create_relationship.assert_not_called()` | no check of what `create_memory` received |
| test_dream_cycle.py:753 / :846 | candidate-limiting tests | `candidates_generated` + `mock_eval.call_count == 8/12` | call-count hard-codes panel size |
| test_dream_cycle.py:1161 | `test_thinker_failure_for_one_slice_continues_others` | counts + `mock_eval.call_count == 4` | does not assert that slice A's failure was *recorded* anywhere (stats/log) |
| test_dream_cycle_db.py:51 | `test_create_run_executes_correct_sql` | `execute.assert_called_once()` + substring checks on the SQL text | SQL is asserted as *text* (`"INSERT INTO dream_cycle_runs" in sql`), so any syntactically broken SQL that still contains those substrings passes |
| test_dream_cycle_db.py:102 | `test_complete_run_updates_stats` | `execute.assert_called_once()`, substrings, positional `params` tuple | see §(2b) below — the whole file is this pattern |
| test_dream_cycle.py:1604 / :1658 | `TestUserRejectionPreservesMemoryProperty.*` | `execute.call_count == 2/1`, SQL substrings, `commit.assert_called_once()` | "single transaction" is inferred from one `commit()` call on a MagicMock — it cannot detect autocommit mode, a missing `BEGIN`, or a rollback path |

**(2b) The `test_dream_cycle_db.py` pattern as a whole — S2.** All 42 tests follow: patch `get_connection` → wire `MagicMock` cursor context manager → set `fetchone/fetchall.return_value` → call → assert on `execute.call_args[0][0]` (SQL substring) and `[0][1]` (params) and/or on the function's return value which is a pure reshaping of `fetchall.return_value`. These are "SQL string snapshot" tests: they prove the function *formats a query containing certain words* and *reshapes rows into dicts*, not that the query is valid or returns the intended rows against the real schema (migrations 001–014). A single real-Postgres (or `pg8000`-against-`testcontainers`) round-trip per function would give more confidence than the 42 mock tests combined. `conftest.py` should be checked for an existing DB fixture (see §7).


### (4) Tests whose name promises one thing and asserts another

| # | file:line | test name | promise | what is actually asserted | Sev |
|---|---|---|---|---|---|
| N1 | test_dream_cycle_db.py:221 | `test_store_deferred_candidate` | that a DEFERRED verdict is stored | `params[-3] == "DEFERRED"`. DEFERRED no longer exists in the domain: `src/dream_cycle_db.py:87` docstring says `final_verdict: ACCEPTED or REJECTED`, `test_dream_cycle.py:634` says "DEFERRED handling removed", and `grep DEFERRED src/` returns nothing. The test proves `store_candidate` is a pass-through for *any* string — i.e. it documents the absence of validation. | S2 |
| N2 | test_dream_cycle_db.py:193/221/242 | `TestStoreCandidate.*` via `_make_verdicts(a,b,c)` | "all verdict combinations" (class docstring) | helper builds only evaluators a/b/c; the fourth seat (`evaluator_d_*`, present in the INSERT at `src/dream_cycle_db.py:117`) is never supplied and never asserted in these three tests. Only `TestStoreCandidatePersistsEvaluatorD:870` covers seat d. | S3 |
| N3 | test_dream_cycle.py:1604 | `test_rejection_updates_candidate_and_memory_in_single_transaction` | atomicity | `commit.assert_called_once()` on a MagicMock. One `commit()` call is not a transaction proof: autocommit connections, or a `BEGIN`-less driver, would also call `commit()` once. Last comment line "# Connection always closed" has no assertion beneath it. | S3 |
| N4 | test_dream_cycle.py:676 | `test_session_start_proceeds_when_briefing_allowed` | "proceeds normally through the pipeline" | explorer stubbed to `[]`, so the pipeline is short-circuited after step 1; only `create_run`/`invoke_explorer` call assertions. | S3 |
| N5 | test_dream_cycle.py:355 | `test_supersede_sets_old_status_and_creates_relationship` | "verify old memory status='superseded' … old relationships are preserved" | no status is read back (mock); "old relationships are preserved" is never asserted at all (the assertion counts new relationship calls only). | S3 |
| N6 | test_dream_cycle.py:457 | `test_supersede_already_superseded_downgrades_to_create` | downgrade to CREATE | asserts only that no `superseded_by` relationship is created; `mock_update` is captured but unused, so an implementation that *did* mark the already-superseded memory again would pass. | S3 |
| N7 | test_dream_cycle.py:1726 (class docstring) | `TestRunRecordCompletenessProperty` | "generated = accepted + deferred + rejected" | code asserts `generated == accepted + rejected`; docstring stale since DEFERRED removal. | S3 |
| N8 | test_dream_cycle.py:209 | `test_evaluator_prompts_contain_no_other_verdicts` | independence: no evaluator sees another's verdict | asserts the literal substrings `"<role>'s verdict"` / `"<role> verdict"` are absent from the *system prompt*. Independence would be violated by passing verdicts in `user_message` or in any other phrasing; the test only checks a phrasing that no implementation would plausibly use. Also `other_verdicts`/`other_reasoning_fragments` are built (lines 236–237) and never used — dead code. | S2 |
| N9 | test_dream_cycle.py:309 | `test_each_evaluator_gets_own_role_criteria` | each prompt contains its role-specific criteria | asserts `len(set(prompts)) == 4` — distinctness, not content. Four prompts differing by a single character would pass. | S3 |
| N10 | test_dream_cycle_db.py:1015 | `test_distinguishes_unanimous_from_non_unanimous` | 3/4 vs 4/4 distinction | the numbers 3 and 1 are *fed in* via `fetchone.side_effect = [{"total_accepted": 3, "non_unanimous": 1}]` and the test asserts `1/3`. The SQL that distinguishes unanimity is never exercised; the test proves `1/3 == 1/3`. | S2 |
| N11 | test_dream_cycle.py:2039 | `test_import_store_accepted_and_check_duplicate` | "behave correctly" (class docstring) | `callable(x)` ×2. | S3 |


### (4b) Vacuous / broken-oracle tests found while tracing the orchestrator

**V1 — S1 — tests/test_orchestrator_properties.py:227 `TestNoDeferredInPipeline.test_no_deferred_verdicts_and_zero_deferred_count` passes vacuously; its oracle is broken.**
Evidence chain (all read, not run):
- The test patches `invoke_evaluator` with `side_effect=eval_side_effects`, a list of exactly `4 × N` verdicts (lines 266–275), and does **not** patch `_ground_candidate` (contrast every `run()` test in `test_dream_cycle.py`, e.g. :768, which does).
- `src/dream_cycle/orchestrator.py:1116-1130` `_ground_candidate` calls `self.invoke_evaluator(candidate, GROUNDING_AUDIT_ROLE, stage="grounding_audit")` for every candidate (spend is 0 at start, so the skip branch at :1126 is not taken). The audit verdict is an `EvaluatorVerdict` with empty `blocking_issues`/`advisory_issues` (dataclass defaults, `src/models.py`), so the candidate is returned unchanged — but **one list element has been consumed**.
- Panel then needs 4 more per candidate → total demand `5 × N` against a supply of `4 × N`. On the `(4N+1)`-th call `MagicMock` raises `StopIteration`. `_invoke_evaluator_safe` (:632) only catches `(TimeoutError, RuntimeError, ValueError)`, so `StopIteration` escapes, is caught by `run()`'s blanket `except Exception` (:337), `complete_run(_ZERO_STATS)` is called and an aborted result is returned.
- Therefore the **last candidate is never stored** in any example, and for `N=1` (a third of the examples, since `min_size=1,max_size=3`) `stored_verdicts == []`. The only assertions are `"DEFERRED" not in stored_verdicts` and `all v in ("ACCEPTED","REJECTED")` — both vacuously true on `[]`. `result` is assigned and never asserted (no `aborted_early is False`, no count check).
- The name also promises "zero deferred count" — there is no such field on `DreamCycleResult` and no assertion about a count.
Fix: patch `_ground_candidate` (or supply `5N` verdicts), assert `result.aborted_early is False` and `len(stored_verdicts) == N`. Better: delete — `tally_consensus` (`src/dream_cycle/consensus.py:9`) is a 3-line pure function already returning only `"ACCEPTED"|"REJECTED"`; a direct property test on it (there is a `tests/test_consensus.py`) covers Property 6 without 60 lines of orchestrator mocks.

**V2 — S3 — tests/test_dream_cycle.py:2668 `test_spend_cap_breach_aborts_run_without_retry`** — same omission (no `_ground_candidate` patch), but here it is harmless: `_invoker_for` returns the same `expensive` mock for every seat, so the single breaching call is the grounding audit (skeptic seat), not "One Astra evaluator call" as the inline comment says (:2681). Assertion `invoke.call_count == 1` still holds. Comment is inaccurate, not the test.

**V3 — S3 — tests/test_orchestrator_properties.py:181 `test_no_cross_contamination_in_evaluator_prompts`** — trailing comment "Verify no evaluator receives tool access (already tested by the mock — tools defaults to False for evaluators)" has no assertion; the mock default proves nothing about the orchestrator. Also duplicates `test_dream_cycle.py:209` + `:309` (same substring oracle, same distinctness check) — see §6.

**V4 — S2 — tests/test_pool_properties.py:196-262 `TestSQLEquivalence.test_sql_equivalence`** — compares `_old_where()` with `_new_where()`, **both defined in the test file** (:200, :211). Nothing from `src/` is imported into the comparison; the test verifies that two helper functions the test author wrote agree with each other. It cannot detect any regression in `src/db.py`/`src/search.py`. Delete, or replace with a test that calls `search_similar`/`hybrid_search` against `test_db` with each filter combination.


### (5) The "signature registry" pattern (`tests/test_pool_properties.py:127-188`, and its twin in `test_dream_cycle.py:2399`)

**What it is.** `_EXPECTED_SIGNATURES` is a hand-maintained dict of 27 `module.function → [param names]`; `test_all_signatures_preserved` asserts `inspect.signature(fn)` parameter names equal the list exactly (order-sensitive, `*args/**kwargs` excluded, defaults and annotations *not* checked despite the docstring "parameter names, defaults, annotations"). `TestFinalOrchestratorMethodSet.test_method_set_is_exactly_coordination_methods` (`test_dream_cycle.py:2399`) is the same pattern for the orchestrator's method names.

**What it protects.** It was written for the one-off `db-layer-hardening` migration (commit `e07e294`, 2026-03-28: "For each migrated function, verify ... matches the pre-migration signature"). During *that* refactor it was a valid oracle: the target was "same public surface, new internals". Verified today with `inspect` against HEAD: 27/27 entries match, 0 mismatches.

**What it costs.**
- It is a change-detector, not a behaviour test. Any legitimate signature evolution fails it and requires editing the test to match the code — the test can never catch a *wrong* change, only *a* change. `git log -p` shows the registry has been edited in 4 of its 5 commits (2026-03-31 `list_memories` +`created_after`; 2026-06-04 `hybrid_search` +`source_type`,`created_after`; 2026-06-16 `create_run` +`backend_provenance`; 2026-09-17). Each was a mechanical "make the test agree with the code" edit.
- Coverage is frozen at migration-time: 22 public functions added since (db: 9 incl. `get_memories_by_ids`, `get_arrivals_summary`, `get_schema_with_constituents`, `find_schemas_for_memory(s)`, `get_memory_provenance`; search: 3 incl. `rerank`; dream_cycle_db: 10 incl. `record_slices`, `get_slices_for_run`, `expire_stale_relationships`) are not in the registry, so it does not even give uniform protection.
- `test_method_set_is_exactly_coordination_methods` has grown from 8 expected names to 18 (`git log -p` shows 17 added/removed method-name lines); each private helper added to the orchestrator (`_role_has_tools`, `_record_spend`, `_allocate_attention`, `_ground_candidate`, `_revise_candidate`, `_skim_arrivals`, `_load_skim_records`, `_budget_remaining`) required touching this test. The docstring still lists the original 8 names — stale. It asserts on *private* method names (`_`-prefixed), which is the opposite of what an API-preservation test should pin.
- Import-time side effects: `from src.db import ... _pool` (`:18`) snapshots the module-level `None` and is never used; `MagicMock` imported unused.

**Recommendation.** Delete both registry tests (≈ 95 lines). If a stable public surface matters, pin it via `__all__` + a docs page, or a `typing.Protocol`, not a list of private names. For the orchestrator, the useful invariant ("storage/consensus/digest logic is not on the class") is already asserted by `test_extracted_methods_not_on_class` (`:2454`) — keep that one.


### (6) Near-duplicate test bodies — quantified

Method: AST-extract each `test_*` body (docstring and comments stripped), compare with `difflib.SequenceMatcher(autojunk=False).ratio()` over stripped lines. Two passes: **raw** (lines must be byte-identical) and **normalised** (string literals → `"S"`, numbers → `N`, so tests that differ only in `"run-123"` vs `"run-id"` count as duplicates).

| Measure | Pairs > 80 % | Cross-file pairs | Distinct tests involved |
|---|---|---|---|
| Raw | 3 | 0 | 6 / 111 |
| Normalised | **26** | 0 | **31 / 111** (test_dream_cycle.py 17, test_dream_cycle_db.py 14) |

Top normalised pairs (ratio, file:line):
- 0.98 `test_dream_cycle.py:753` ↔ `:846` (truncate vs. scheduled-no-truncate) — D1
- 0.97 `test_dream_cycle.py:1304` ↔ `:1349` (one vs. two dissents) — D2
- 0.97 / 0.95 `:753` ↔ `:802`, `:802` ↔ `:846` (the whole `TestSessionStartCandidateLimiting` class is one test ×3) — D1
- 0.93 `test_dream_cycle_db.py:221` ↔ `:242` (DEFERRED vs REJECTED store) — D3
- 0.91 `test_dream_cycle_db.py:471` ↔ `:490` (`fetchone→(None,)` vs `fetchone→None`) — D4
- 0.91 / 0.82 `test_dream_cycle_db.py:373` ↔ `:392` ↔ `:411` (`should_run_briefing` truth table, 3 tests for 3 rows) — D5
- 0.89 ×4 `test_dream_cycle_db.py:665/679/693/707` (`was_feedback_injected` ×4: present/""/None/no-row) — D6; note `:821` already covers all four as a Hypothesis property, so the four explicit tests are strictly redundant
- 0.89 ×3 `test_dream_cycle_db.py:331/541/996` "returns empty list when fetchall is []" for three different functions — D7 (identical body, different callee)
- 0.89–0.81 `test_dream_cycle.py:1492/1525/1545/1564` (`build_feedback_injection` user-rejection variants) — D8: a 4-row `@pytest.mark.parametrize((candidate_json, reason, expected_substr))`
- 0.83 `test_dream_cycle.py:117` ↔ `:1121` (explorer `[]` vs explorer raises) — D9
- 0.83 `test_dream_cycle.py:986` ↔ `:1019` (digest explicit tests) — D10
- 0.81 `test_dream_cycle.py:419` ↔ `:457` (supersede downgrade ×2; plus `:1391` is a superset of `:419`) — D11
- 0.81 `test_dream_cycle.py:1864` ↔ `:1931` (run-record explicit ×2; both are draws of the property at `:1726`) — D12

Cross-file duplication is *conceptual* rather than line-level (so the pairwise metric misses it):
- `_make_orchestrator()` defined identically in `test_dream_cycle.py:30` and `test_orchestrator_properties.py:27`.
- Strategies `run_types`, `confidence_levels`, `schema_operations`, `insight_types`, `create_candidates` copy-pasted between the two files; the copies have **drifted**: `test_orchestrator_properties.py:58-60` adds `.filter(lambda s: s.strip())` to `content` (needed because the grounding path rejects whitespace-only content) while `test_dream_cycle.py:60` does not. Harmless today (the un-filtered copy never feeds `run()`), but it is exactly the kind of divergence that produces a flaky test the first time someone reuses the strategy.
- Evaluator-independence oracle (substring `"<role>'s verdict"`) + "4 prompts are distinct" appears in `test_dream_cycle.py:209`, `:309` and `test_orchestrator_properties.py:181` — three tests, one oracle.
- `mark_user_rejected` two-SQL-statement test appears in `test_dream_cycle.py:1604/:1658` (Hypothesis) and `test_dream_cycle_db.py:433/:471/:490` (explicit) — five tests for one 15-line function.

Boilerplate repetition (S2, refactor seam): counts by `grep`:
- Cursor context-manager wiring (`mock_conn = MagicMock(); mock_cur = MagicMock(); …__enter__ = lambda s: mock_cur; …__exit__ = MagicMock(return_value=False); …`) — **40 copies** in `test_dream_cycle_db.py` + 2 in `test_dream_cycle.py`; ≈ 240 of `test_dream_cycle_db.py`'s 1,063 lines (23 %) are this block. One `_mock_conn(fetchone=…, fetchall=…)` helper or a `@pytest.fixture` would remove ~200 lines.
- `patch("src.dream_cycle.orchestrator.dream_cycle_db")` + `_skim_arrivals` patch + `get_memory_stats.return_value = {…4-key dict…}` — **19 + 2** copies. A `pipeline_mocks` fixture (or a `contextlib.ExitStack` helper returning the `mock_db`) would remove ~150 lines.
- The 5–7-deep `patch("src.dream_cycle.storage.*")` stack — **10 copies**.
- `_capture_write`/`_patched_write_text` closures over `pathlib.Path.write_text` — **4 copies**; `tmp_path` + patching the digest's `LOGS_DIR` would be shorter and not global.
- `_make_slice`/`_make_candidate` defined twice (`:733/:742` and `:1101/:1110`) with identical bodies; `MemorySlice(...)` literal inlined 8 more times.


### (7) Hand-rolled fakes in these four files vs. what conftest / sibling tests already provide

No `class Fake*`/`Stub*` is defined in the four in-scope files; the fakes are *inline closures and MagicMock graphs* instead, which is why they are duplicated so often:

| Hand-rolled fake (in scope) | Occurrences | Existing equivalent it duplicates |
|---|---|---|
| `mock_invoke(system_prompt, user_message, tools=False, **kw)` closure + `orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)` | 9 in `test_dream_cycle.py` (:224, :285, :316, :2560, :2588, :2614, :2638, :2660) + 1 in `test_orchestrator_properties.py:198` | `FakeInvoker` in `tests/test_skim_orchestrator.py:43`, `tests/test_skim_runner.py:45`, `tests/test_weekly_digest_backend.py:23`, `_FakeInvoker` in `tests/test_skim_probe.py:42` — four copies of a recording invoker already exist elsewhere in `tests/`; none is in `conftest.py`, so the in-scope files re-invent a fifth (as a closure) each time |
| `MagicMock()` connection/cursor graph with `__enter__/__exit__` wiring | 40 + 2 | `conftest.test_db` (session, real Postgres, migrations applied) + `clean_tables`. `tests/test_ledger.py:95-140` already exercises `dream_cycle_db.create_run/complete_run/record_slices/get_slices_for_run` against `test_db`, proving the real-DB path works for this module |
| `mock_db.get_memory_stats.return_value = {"total_count":…, "recent_activity":…, "date_range":…, "type_distribution":{}}` | 17 + 2 | none — candidate for a `MEMORY_STATS_STUB` constant or fixture |
| `_capture_write(self_path, text, encoding=None)` monkeypatching `pathlib.Path.write_text` | 4 | pytest `tmp_path` |
| `_make_slice()` / `_make_candidate()` / inline `MemorySlice(...)` | 2 + 2 defs, 8 inline literals | none — candidate for module-level factories or `st.builds` reuse |
| `_bedrock_resolver()` building a `Resolver` of six `RoleBackend`s | 1 (`:2530`) | `_FakeResolver` in `tests/test_panel_replay.py:24`, `StubResolver` in `tests/test_weekly_digest_backend.py:54` |
| `_deterministic_embedding` analogue: `return_value=[0.1] * 1024` / `[0.42] * 128` / `[0.5] * 1024` | 12 | `conftest.mock_embedding` (deterministic, L2-normalised, 1024-dim). Note the in-scope tests use **three different dimensionalities** (128, 1024) for the same `generate_embedding` patch — harmless because `create_memory` is also mocked, but it means no in-scope test would catch a dimension mismatch against the `vector(1024)` column |

**Refactor seam (S2):** promote one `FakeInvoker` (the `test_skim_orchestrator.py:43` version already records calls, supports scripted outputs incl. exceptions, and returns `usage`) and one `mock_conn()` helper into `tests/conftest.py`; the nine closures and forty wiring blocks collapse onto them.

### (8) Concrete recommendation

**Delete outright (≈ 640 lines, no coverage lost):**
1. `test_dream_cycle_db.py` — all 42 mock tests except keep as *regression seeds* the three that pin non-obvious param layouts (`:73 backend_provenance JSON`, `:146 extra stats → JSONB`, `:870 evaluator_d positions`). The rest are SQL-substring snapshots (§2b) and are covered better by the replacement in step 3. −≈ 900 lines.
2. `test_dream_cycle.py:161` (dup of :117), `:1121` (fold into :117 via parametrize), `:1864`, `:1931` (draws of :1726), `:457` and `:1391` (fold into :419 via parametrize), `:2039` (tautology), `:2399` (method-set registry), `:665-707`-style explicit truth-table tests already covered by Hypothesis siblings. −≈ 330 lines.
3. `test_orchestrator_properties.py:227` (vacuous, V1) −60 lines; `:181` (dup of `test_dream_cycle.py:209`) −50 lines.
4. `test_pool_properties.py:127-188` signature registry and `:196-262` self-comparing SQL test. −≈ 95 lines.

**Convert to a handful of behavioural tests (≈ +250 lines):**
5. `dream_cycle_db` round-trips on `test_db` (pattern already in `tests/test_ledger.py:95`): one test per function that (a) `create_run` → `store_candidate` ×3 with mixed verdicts incl. seat d → `complete_run` with extra stats → assert `get_evaluator_verdicts_for_run`, `get_accepted_dissents`, `get_tier1_metrics["non_unanimous_acceptance_rate"]`, `was_feedback_injected`, `get_previous_run_id` all read back the *persisted* values; (b) `mark_user_rejected` then `SELECT status FROM memories` — proves the memory is preserved, which no mock test can. ~8 tests, ~150 lines. This is the only way `test_distinguishes_unanimous_from_non_unanimous` (N10) stops being `1/3 == 1/3`.
6. Orchestrator pipeline: one `pipeline` fixture (patches `dream_cycle_db`, `_skim_arrivals`, `_ground_candidate`, `build_feedback_injection`, `generate_digest`, `check_duplicate`, `store_accepted`; returns `(orch, mock_db)`), then **one** parametrised test for `(explorer_result, run_type, thinker_candidates, verdict_script) → (generated, accepted, rejected, aborted_early, store_candidate verdicts)` covering: empty slices, explorer raises, session_start cap 2, scheduled cap `MAX_CANDIDATES_PER_SLICE`, 3/4 accept, 2/4 reject, thinker fails one slice, evaluator unrecoverable after N, partial-stats-on-abort. ~10 rows, ~100 lines, replacing ≈ 700 lines of `run()` tests. Keep the two Hypothesis properties (`:117`, `:1726`) but fix M11's oracle to compare stored verdicts to the fed verdicts.
7. Quorum: move dissent tests onto `tally_consensus` directly (`tests/test_consensus.py` exists).
8. `build_feedback_injection` user-rejection variants → one 4-row parametrize (D8).

**Expected net effect:** the four files go from **4,419 lines / 111 tests** to roughly **2,000 lines / ~55 tests** (−55 %), with *more* behaviour covered (real SQL against the migrated schema; the memory-preservation invariant; verdict mapping) and fewer change-detectors. The S1 vacuous test and the two stale-domain tests (DEFERRED) disappear as a side effect.

Priority order if time is short: fix/delete V1 (S1) → real-DB round-trips for `dream_cycle_db` (§8.5) → `pipeline` fixture (§8.6) → registry deletions (§5).

## Could not verify

- I did not run pytest (forbidden), so every "passes vacuously" / "would pass with a stub" claim is from static tracing of the call graph (`orchestrator.py:272-530, 606-650, 1116-1150`, `consensus.py:9-27`, `models.py EvaluatorVerdict`), not observed execution. In particular V1 depends on `MagicMock` raising `StopIteration` when a list `side_effect` is exhausted (documented `unittest.mock` behaviour) and on `_ground_candidate`'s spend-skip branch not firing at run start (`SpendTracker.from_env()` defaults not inspected).
- Whether the whole-suite run currently touches the live `memory_bank` DB from these files: none of the four in-scope files request `test_db` except `test_pool_properties.py`; `test_dream_cycle*.py` patch `dream_cycle_db`/`get_connection` throughout, so I found no unpatched DB path in scope — but I did not audit the transitive imports (`_make_orchestrator()` → `DreamCycleOrchestrator()` constructor) for connections at construction time (12a covers isolation).
- Line-count reduction figures in §8 are estimates from the per-test line spans measured by AST; the "+250" for replacements is a judgement.
- Hypothesis `HealthCheck.function_scoped_fixture`: `test_pool_properties.py` mixes `@given` with the `test_db` fixture; `test_db` is session-scoped so the health check should not fire, but I did not confirm there is no `pytest.ini`/`conftest` `settings.register_profile` that changes this (`pytest.ini` contains only `testpaths`).
