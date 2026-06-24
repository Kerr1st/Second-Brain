# Requirements Document

## Introduction

The reranking weights that drive retrieval quality currently live in two independent
copies: the inline literals inside `rerank()` in `src/search.py` (production), and the
`PRODUCTION_WEIGHTS` dict in `scripts/eval/eval_common.py` (the evaluation suite). The
same values are also re-hardcoded in tests (`tests/test_rerank.py`,
`tests/test_eval_properties.py`).

Because nothing connects these copies, a weight change in one place does not propagate to
the others and produces no error. The failure mode is silent: the evaluation suite quietly
scores a different formula than production runs, so MRR/Hit@k numbers stop reflecting
reality, and weight-tuning decisions may be made against a stale formula.

This feature establishes a single canonical definition of the rerank weights that both the
production scorer and the evaluation scorer consume, plus an automated guard that fails
loudly if the two scoring paths ever diverge. The change is a behavior-preserving refactor:
the production rerank score for any given input must be identical before and after.

## Glossary

- **Coefficient signal**: a weight multiplied by a normalized `[0,1]` signal
  (`rrf`, `overlap`, `title_overlap`, `context_overlap`, `recency`, `length`, `depth`).
- **Additive magnitude**: the raw additive value a signal contributes when its condition
  holds (`type_boost` 0.06, `mem_class_boost` 0.04/0.02, `project_penalty` -0.15,
  `superseded_penalty` -0.20, `staleness_penalty` cap -0.05) plus `reinforcement_coeff` 0.03.
- **Production scorer**: `rerank()` in `src/search.py`.
- **Eval scorer**: `rerank_with_overrides()` in `scripts/eval/eval_common.py`, driven by
  `PRODUCTION_WEIGHTS`.
- **Drift**: the state where the production scorer and eval scorer compute different scores
  for the same input because their weight definitions disagree.

## Requirements

### Requirement 1: Single canonical definition of rerank weights

**User Story:** As a developer tuning retrieval, I want one authoritative place that defines
every rerank weight, so that I change a weight in exactly one location.

#### Acceptance Criteria

1. THE system SHALL define every rerank weight (all coefficient signals, all additive
   magnitudes, and `reinforcement_coeff`) in a single canonical location.
2. WHERE a weight value is needed by the production scorer, THE production scorer SHALL
   read it from the canonical location rather than an inline literal.
3. WHERE a weight value is needed by the eval scorer, THE eval scorer SHALL derive it from
   the canonical location rather than a re-typed literal.
4. THE canonical location SHALL be importable by both `src/search.py` and
   `scripts/eval/eval_common.py` without introducing a circular import.
5. THE canonical definition SHALL preserve the existing distinction between coefficient
   signals and additive magnitudes so the eval suite's override/ablation mechanism
   continues to function unchanged.

### Requirement 2: Behavior-preserving refactor

**User Story:** As a maintainer, I want the refactor to change structure only, so that
retrieval ranking behavior is provably identical before and after.

#### Acceptance Criteria

1. WHEN the production scorer ranks any given result set, THE resulting `rerank_score` for
   each item SHALL be identical (within floating-point tolerance) to the score produced by
   the pre-refactor implementation.
2. THE refactor SHALL NOT change any weight value.
3. THE refactor SHALL NOT change the set of underscore-prefixed intermediate signals that
   `rerank()` stores on each result.
4. WHEN the existing test suite is run after the refactor, all previously passing tests
   SHALL still pass.

### Requirement 3: Automated drift guard

**User Story:** As a developer, I want a test that fails when the production scorer and eval
scorer disagree, so that drift cannot be merged silently.

#### Acceptance Criteria

1. THE system SHALL include an automated test that runs the same input through the
   production scorer and the eval scorer (using the canonical weights) and asserts the
   resulting scores are equal within floating-point tolerance.
2. IF a weight is changed in the production scorer but not reflected in the eval scorer
   (or vice versa), THEN the drift guard test SHALL fail.
3. THE drift guard SHALL cover the additive-magnitude path (type_boost, mem_class_boost,
   penalties, reinforcement) in addition to the coefficient signals.
4. THE drift guard SHALL exercise the production scorer against realistic inputs sourced
   from a live local PostgreSQL database (via `hybrid_search` + `rerank`), consistent with
   how the evaluation suite runs.
5. WHERE realistic inputs do not naturally exercise an additive-magnitude branch
   (e.g. superseded status, staleness, cross-project), THE drift guard SHALL seed
   purpose-built rows into the isolated test database so that branch is covered.
6. THE drift guard SHALL run as part of the standard test suite using the existing
   PostgreSQL test fixture, without requiring setup beyond what existing DB-backed tests
   require.

### Requirement 4: Consistency of dependent tooling and docs

**User Story:** As a developer reading the code and docs, I want dependent references to the
weights to stay consistent, so that no third copy silently drifts.

#### Acceptance Criteria

1. THE system SHALL retain exactly one independent value oracle test
   (`tests/test_rerank.py`) that re-declares the weight values with its own literals and
   asserts the production scorer matches, so that an unintended change to a canonical weight
   value causes a test failure and weight changes remain deliberate.
2. THE independent value oracle SHALL NOT import the canonical weight values, so it can
   detect a wrong value in the canonical source rather than verifying it against itself.
3. THE optimizer (`scripts/eval/eval_optimize.py`) baseline starting point SHALL continue to
   originate from the canonical weights.
4. THE architecture documentation's stated rerank formula SHALL remain consistent with the
   canonical weights, OR a follow-up note SHALL identify it as out of scope for this change.

## Out of Scope

- Changing any weight value or the shape of the rerank formula.
- Centralizing signal-shaping internals that are not duplicated across the production and
  eval paths (recency stability constants `30`/`10`/`0.8`, the length cap `80`, the
  staleness day thresholds `90`/`180`). These live only in the production scorer and are not
  a drift source; they may be revisited later.
- Fixing the inaccurate "Bayesian" docstring and the incomplete code-snippet template in
  `eval_optimize.py` (tracked separately as minor cleanups).
- Any change to the dormant knowledge graph or inert schema-type feature.

## Assumptions

- The eval suite intentionally represents additive magnitudes as multipliers (1.0 =
  passthrough) so that ablation can zero a signal; this representation is retained.
- A live local PostgreSQL is available and fast enough to back the drift guard; the guard
  uses the existing isolated `memory_bank_test` fixture and may seed rows to cover specific
  scoring branches.
