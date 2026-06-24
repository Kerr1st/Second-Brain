# Implementation Plan

## Overview

A behavior-preserving refactor that introduces `src/rerank_weights.py` as the single source
of truth for the rerank weights, repoints the production scorer (`src/search.py`) and the
eval scorer (`scripts/eval/eval_common.py`) at it, and adds a DB-backed drift guard test.
The independent value oracle (`tests/test_rerank.py`) is left unchanged. Tasks are ordered so
the green baseline is captured first and the guard is proven to have teeth before completion.

## Task Dependency Graph

```
1 (baseline)
└─> 2 (canonical module)
    ├─> 3 (production scorer) ─┐
    └─> 4 (eval PRODUCTION_WEIGHTS) ─┤
                                     └─> 5 (verify behavior preserved)
                                         └─> 6 (drift guard) ─> 6.1 ─> 6.2
                                             └─> 7 (negative check)
                                                 └─> 8 (final verify + doc)
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "rationale": "Capture green baseline before any change." },
    { "wave": 2, "tasks": ["2"], "rationale": "Create the canonical module everything depends on." },
    { "wave": 3, "tasks": ["3", "4"], "rationale": "Repoint production and eval scorers at the module; independent, can run in parallel." },
    { "wave": 4, "tasks": ["5"], "rationale": "Verify behavior preserved once both scorers use the module." },
    { "wave": 5, "tasks": ["6.1", "6.2"], "rationale": "Build the DB-backed drift guard (seed fixtures, then assert agreement)." },
    { "wave": 6, "tasks": ["7"], "rationale": "Prove the guard fails on a perturbation." },
    { "wave": 7, "tasks": ["8"], "rationale": "Full-suite verification and doc pointer." }
  ]
}
```

## Tasks

- [x] 1. Capture the green test baseline
  - Run `.venv/bin/python -m pytest tests/test_rerank.py tests/test_eval_properties.py tests/test_search.py -q` and record that they pass before any change, so behavior preservation can be verified later.
  - _Requirements: 2.4_

- [x] 2. Create the canonical weights module
  - Add `src/rerank_weights.py` defining `COEFFICIENTS` (rrf, overlap, title_overlap, context_overlap, recency, length, depth), `REINFORCEMENT_COEFF`, `TYPE_BOOST`, `TYPE_BOOST_TYPES`, `MEM_CLASS_BOOST`, `PROJECT_PENALTY`, `SUPERSEDED_PENALTY`, `STALENESS_PENALTY_MAX` with the exact current values.
  - Add a module docstring stating it is the single source of truth and that `tests/test_rerank.py` deliberately does not import it.
  - Ensure the module imports nothing from the `src` package (no circular import).
  - _Requirements: 1.1, 1.4, 1.5_

- [x] 3. Point the production scorer at the canonical module
  - In `src/search.py`, import the canonical constants and replace the inline literals in `rerank()` for: the seven coefficient terms, `type_boost`, `mem_class_boost`, `reinforcement` coefficient, `project_penalty`, `superseded_penalty`, and the staleness magnitude.
  - Leave signal-shaping internals unchanged (recency `30/10/0.8`, length cap `80`, staleness day thresholds `90/180`).
  - Keep the formula structure and all stored underscore-prefixed signals identical.
  - _Requirements: 1.2, 2.1, 2.2, 2.3_

- [x] 4. Derive eval `PRODUCTION_WEIGHTS` from the canonical module
  - In `scripts/eval/eval_common.py`, import `COEFFICIENTS` and `REINFORCEMENT_COEFF` and build `PRODUCTION_WEIGHTS` from them, retaining the `1.0`-multiplier entries for type_boost, mem_class_boost, project_penalty, superseded_penalty, staleness_penalty.
  - Leave `rerank_with_overrides()`, `COLD_OVERRIDES`, and `ABLATION_CONDITIONS` unchanged.
  - Update the comment block to point to `src/rerank_weights.py` as the source.
  - _Requirements: 1.3, 1.5, 4.3_

- [x] 5. Verify behavior preservation
  - Re-run `tests/test_rerank.py` and `tests/test_eval_properties.py`; confirm they pass unchanged with no edits to those files.
  - Confirm `tests/test_rerank.py` still uses its own literals and does not import `src/rerank_weights.py`.
  - _Requirements: 2.1, 2.2, 2.4, 4.1, 4.2_

- [x] 6. Write the DB-backed drift guard test
- [x] 6.1 Seed branch-covering fixtures
  - In a new `tests/test_rerank_drift.py`, using the existing `memory_bank_test` fixture and mocked embeddings, seed memories that exercise every additive-magnitude branch: each `TYPE_BOOST_TYPES` value plus a non-boosted type, `mem_class` in {semantic, procedural, episodic/None}, a `status="superseded"` row, a stale row (`access_count=0`, `created_at` > 90 days), rows in two projects, and rows with/without `encoding_context`.
  - _Requirements: 3.4, 3.5_
- [x] 6.2 Assert production and eval scorers agree
  - Run `rerank(hybrid_search(query, emb), query, query_project=...)` to get production scores, deep-copy the results, re-score via `rerank_with_overrides(copy, PRODUCTION_WEIGHTS)`, and assert per-id `rerank_score` equality within `1e-9`.
  - Assert the seeded branches are actually present among scored results so coverage can't silently vanish.
  - _Requirements: 3.1, 3.2, 3.3, 3.6_

- [x] 7. Demonstrate the guard has teeth (transient negative check)
  - Temporarily perturb one value in `src/rerank_weights.py`, confirm both `tests/test_rerank_drift.py` and `tests/test_rerank.py` fail, then revert and confirm green.
  - _Requirements: 3.2, 4.1_

- [x] 8. Final verification and doc pointer
  - Run the full suite: `.venv/bin/python -m pytest`.
  - Add a one-line pointer in `docs/ARCHITECTURE.md` noting `src/rerank_weights.py` is the authoritative source for the rerank formula values (no value changes).
  - _Requirements: 2.4, 4.4_

## Notes

- This is a refactor: no weight value changes. Tasks 1 and 5 bracket the change to prove
  behavior preservation via the unchanged oracle test.
- The drift guard (task 6) is DB-backed and uses the existing `memory_bank_test` fixture with
  mocked embeddings; it seeds rows where realistic data would not naturally hit a branch.
- Out of scope (deferred): the "Bayesian" docstring and incomplete code-snippet template in
  `eval_optimize.py`; centralizing non-duplicated signal-shaping constants.
