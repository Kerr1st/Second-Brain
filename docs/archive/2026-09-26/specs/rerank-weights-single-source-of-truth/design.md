# Design Document

## Overview

The rerank weights currently exist as two unsynchronized copies: inline literals in
`rerank()` (`src/search.py`, the production scorer) and the `PRODUCTION_WEIGHTS` dict in
`scripts/eval/eval_common.py` (which drives the eval scorer `rerank_with_overrides()`). A
third hand-maintained copy lives in `tests/test_rerank.py`.

This design introduces a single canonical module, `src/rerank_weights.py`, that defines
every rerank weight exactly once. The production scorer reads its weights from this module
instead of inline literals. The eval suite derives `PRODUCTION_WEIGHTS` from this module
instead of re-typing the numbers. A new DB-backed guard test asserts the two scorers compute
identical scores on realistic inputs, so any future divergence in formula wiring fails
loudly. `tests/test_rerank.py` is intentionally left as an independent value oracle (its own
literals, no import of the canonical module) so that a wrong *value* in the canonical source
is also caught.

The change is a behavior-preserving refactor: no weight value changes, and the production
`rerank_score` for any input is identical before and after.

## Architecture

### Before

```
src/search.py rerank()                    scripts/eval/eval_common.py
  0.30 * rrf_score   ◄── literals          PRODUCTION_WEIGHTS = {       ◄── duplicate literals
  0.18 * overlap                             "rrf": 0.30, ...
  type_boost = 0.06 ...                      "reinforcement_coeff": 0.03, ... }
            (no link between them — silent drift)

tests/test_rerank.py  ◄── third copy of literals (independent oracle)
```

### After

```
                 ┌───────────────────────────┐
                 │  src/rerank_weights.py     │   ← single source of truth
                 │  COEFFICIENTS, TYPE_BOOST, │
                 │  MEM_CLASS_BOOST, ...       │
                 └────────────┬───────────────┘
            imports           │            derives
        ┌───────────────────┐ │ ┌──────────────────────────────┐
        ▼                     ▼ ▼                                ▼
  src/search.py rerank()        scripts/eval/eval_common.py
  uses constants in formula     PRODUCTION_WEIGHTS built from constants
        │                                 │
        └──────────── drift guard ────────┘
        tests/test_rerank_drift.py asserts both produce equal scores (DB-backed)

  tests/test_rerank.py  ← UNCHANGED: independent oracle, own literals, no canonical import
```

### Import safety

`src/rerank_weights.py` imports nothing from the `src` package (only, if anything, stdlib).
`src/search.py` already imports `src.db`; adding an import of `src.rerank_weights` (which has
no `src` imports) introduces no cycle. `scripts/eval/eval_common.py` already imports
`src.search`; importing `src.rerank_weights` is likewise acyclic.

## Components and Interfaces

### 1. `src/rerank_weights.py` (new)

The canonical definition. Two conceptual groups, matching the requirements glossary:

```python
"""Canonical rerank weights — the single source of truth.

Both the production scorer (src/search.py rerank) and the evaluation scorer
(scripts/eval/eval_common.py rerank_with_overrides) read their weights from here.
Changing a weight here changes it everywhere. The independent value oracle in
tests/test_rerank.py deliberately does NOT import this module.
"""

# Coefficient signals — multiplied by normalized [0,1] signal values.
COEFFICIENTS = {
    "rrf": 0.30,
    "overlap": 0.18,
    "title_overlap": 0.18,
    "context_overlap": 0.10,
    "recency": 0.12,
    "length": 0.08,
    "depth": 0.05,
}

# Reinforcement coefficient — multiplies log1p(access_count) * spacing_bonus.
REINFORCEMENT_COEFF = 0.03

# Additive magnitudes — the raw value a signal contributes when its condition holds.
TYPE_BOOST = 0.06
TYPE_BOOST_TYPES = ("idea", "synthesis", "insight", "decision")
MEM_CLASS_BOOST = {"semantic": 0.04, "procedural": 0.02}
PROJECT_PENALTY = -0.15
SUPERSEDED_PENALTY = -0.20
STALENESS_PENALTY_MAX = -0.05
```

Scope boundary (per requirements "Out of Scope"): signal-*shaping* internals that are not
duplicated across the two scorers stay in `search.py` — the recency stability constants
(`30.0`, `10.0`, `0.8`), the length cap (`80`), and the staleness day thresholds
(`90`, `180`). Only the staleness *magnitude* (`-0.05`) moves, because that is the weight.
`STALENESS_PENALTY_MAX` is the only staleness value the eval path conceptually mirrors.

### 2. `src/search.py` `rerank()` (modified)

Replace inline literals with references to the canonical constants. The formula structure,
the intermediate underscore-prefixed signals, and every value are unchanged.

```python
from src.rerank_weights import (
    COEFFICIENTS, REINFORCEMENT_COEFF, TYPE_BOOST, TYPE_BOOST_TYPES,
    MEM_CLASS_BOOST, PROJECT_PENALTY, SUPERSEDED_PENALTY, STALENESS_PENALTY_MAX,
)

# inside the per-result loop:
type_boost = TYPE_BOOST if mem_type in TYPE_BOOST_TYPES else 0.0
mem_class_boost = MEM_CLASS_BOOST.get(mem_class, 0.0)
reinforcement = REINFORCEMENT_COEFF * math.log1p(access) * spacing_bonus
project_penalty = PROJECT_PENALTY if (query_project and mem_project and mem_project != query_project) else 0.0
superseded_penalty = SUPERSEDED_PENALTY if mem_status == "superseded" else 0.0
# staleness keeps its 90/180-day shaping; only the magnitude is canonical:
staleness_penalty = STALENESS_PENALTY_MAX * min(1.0, (days_unretrieved - 90) / 180)  # when applicable

r["rerank_score"] = (
    COEFFICIENTS["rrf"] * r.get("rrf_score", 0)
    + COEFFICIENTS["overlap"] * overlap
    + COEFFICIENTS["title_overlap"] * title_overlap
    + COEFFICIENTS["context_overlap"] * context_overlap
    + COEFFICIENTS["recency"] * recency
    + COEFFICIENTS["length"] * length_score
    + COEFFICIENTS["depth"] * depth_score
    + type_boost + mem_class_boost + reinforcement
    + project_penalty + superseded_penalty + staleness_penalty
)
```

### 3. `scripts/eval/eval_common.py` `PRODUCTION_WEIGHTS` (modified)

Derive the dict from the canonical constants. The eval-specific representation — additive
signals as `1.0` multipliers so ablation can zero them — is retained exactly.

```python
from src.rerank_weights import COEFFICIENTS, REINFORCEMENT_COEFF

PRODUCTION_WEIGHTS = {
    **COEFFICIENTS,                 # rrf, overlap, title_overlap, context_overlap, recency, length, depth
    "type_boost": 1.0,             # multiplier on raw _type_boost
    "mem_class_boost": 1.0,        # multiplier on raw _mem_class_boost
    "reinforcement_coeff": REINFORCEMENT_COEFF,
    "project_penalty": 1.0,        # multiplier on raw _project_penalty
    "superseded_penalty": 1.0,     # multiplier on raw _superseded_penalty
    "staleness_penalty": 1.0,      # multiplier on raw _staleness_penalty
}
```

`rerank_with_overrides()`, `COLD_OVERRIDES`, and `ABLATION_CONDITIONS` are unchanged — they
key off the same names. `eval_optimize.py` continues to seed its optimizer from
`PRODUCTION_WEIGHTS`, so its baseline still originates from the canonical weights
(Requirement 4.3) with no edit required.

### 4. `tests/test_rerank_drift.py` (new — the drift guard)

DB-backed. Uses the existing `memory_bank_test` fixture and mocked embeddings (same harness
as other DB tests). Procedure:

1. Seed memories that exercise every additive-magnitude branch (Requirement 3.3/3.5):
   - one of each `TYPE_BOOST_TYPES` and one non-boosted type;
   - `mem_class` ∈ {semantic, procedural, episodic/None};
   - a `status="superseded"` row;
   - a stale row (`access_count=0`, `created_at` > 90 days ago) for the staleness branch;
   - rows in two projects to drive the cross-project penalty;
   - rows with and without `encoding_context`.
2. Run the production path: `results = rerank(hybrid_search(query, emb), query, query_project=...)`.
   Record `{id: rerank_score}` as `production_scores`.
3. Re-score with the eval path on a deep copy:
   `rerank_with_overrides([dict(r) for r in results], PRODUCTION_WEIGHTS)`.
   Record `{id: rerank_score}` as `eval_scores`.
4. Assert `production_scores[id] == eval_scores[id]` within `1e-9` for every id, and assert
   the seeded branches were actually present among the scored results.

Because `rerank_with_overrides()` reads the stored `_recency`, `_staleness_penalty`, etc.
and only *recomputes* reinforcement from stable stored fields (`access_count`,
`_spacing_bonus`), the comparison is clock-independent — no time freezing needed.

### 5. `tests/test_rerank.py` (unchanged — independent oracle)

Left exactly as-is per Requirements 4.1–4.2: its own hardcoded literals, no import of
`src/rerank_weights.py`. After the refactor it must still pass (values unchanged), confirming
behavior preservation. It is the only place that validates the canonical *values* are what we
intend; a typo in `rerank_weights.py` makes this test fail.

## Data Models

No database schema changes. The only new "data" is the module-level constant set in
`src/rerank_weights.py` described above. The shape of result dicts and the underscore-prefixed
intermediate signals are unchanged.

## Correctness Properties

These are the executable properties the implementation must satisfy (validated via the test
suite, including property-based tests where applicable):

### Property 1: Behavior preservation

For any result set, `rerank()` after the refactor produces the same `rerank_score` per item
as before (within `1e-9`). *Validated by:* the unchanged property-based oracle in
`tests/test_rerank.py`.

**Validates: Requirements 2.1, 2.2**

### Property 2: Scorer agreement (no drift)

For any result set produced by `hybrid_search()+rerank()`, the eval scorer with
`PRODUCTION_WEIGHTS` yields the same score per item (within `1e-9`). *Validated by:*
`tests/test_rerank_drift.py`.

**Validates: Requirements 3.1, 3.2, 3.3**

### Property 3: Single definition / wiring fault detection

If `rerank()`'s formula and the eval formula diverge (a term added/removed/reweighted in one
but not the other), Property 2 fails. *Validated by:* `tests/test_rerank_drift.py`.

**Validates: Requirements 3.2**

### Property 4: Oracle independence

`tests/test_rerank.py` does not import `src/rerank_weights.py`, so a wrong canonical value
fails it. *Validated by:* code review plus an import-absence assertion if desired.

**Validates: Requirements 4.1, 4.2**

### Property 5: Intermediate-signal stability

The set of underscore-prefixed keys stored on each result is unchanged. *Validated by:*
existing tests that read those keys plus the drift guard.

**Validates: Requirements 2.3**

## Error Handling

This is a refactor with no new runtime failure modes. Considerations:

- **Import errors:** `src/rerank_weights.py` is dependency-free; the only failure mode is a
  typo in the import list, caught immediately by the test suite at import time.
- **Missing key access:** the production scorer references `COEFFICIENTS["rrf"]` etc. with
  known static keys; a missing key raises `KeyError` at first call and is caught by any rerank
  test. No defensive `.get()` is added, since silent fallback would mask exactly the kind of
  drift this feature prevents.
- **Float comparison:** all equality assertions use an absolute tolerance of `1e-9`,
  consistent with the existing oracle test.

## Testing Strategy

1. **Run the existing suite first** to capture the green baseline (especially
   `tests/test_rerank.py`, `tests/test_eval_properties.py`, `tests/test_search.py`).
2. **Implement** the canonical module, then update `search.py` and `eval_common.py`.
3. **Re-run** `tests/test_rerank.py` and `tests/test_eval_properties.py` — they must pass
   unchanged (proves P1, behavior preservation).
4. **Add** `tests/test_rerank_drift.py` (P2/P3) and run it against local Postgres.
5. **Negative check (manual, transient):** temporarily perturb one canonical value and
   confirm the drift guard and the oracle fail; revert. This demonstrates the guard has teeth.
6. **Full suite** green before completion.

Command: `.venv/bin/python -m pytest tests/test_rerank.py tests/test_rerank_drift.py tests/test_eval_properties.py -q`, then the full `.venv/bin/python -m pytest`.

## Documentation Note

`docs/ARCHITECTURE.md` states the rerank formula with literal values. Since values are
unchanged, it remains accurate. Optionally add a one-line pointer noting that
`src/rerank_weights.py` is the authoritative source (Requirement 4.4). No values to update.
