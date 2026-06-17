"""Canonical rerank weights — the single source of truth.

Both the production scorer (``src/search.py`` ``rerank``) and the evaluation
scorer (``scripts/eval/eval_common.py`` ``rerank_with_overrides``) read their
weights from here. Changing a weight here changes it everywhere.

The independent value oracle in ``tests/test_rerank.py`` deliberately does NOT
import this module: it re-declares the weight values with its own literals so
that a wrong value in this canonical source is detected rather than verified
against itself.

This module imports nothing from the ``src`` package (stdlib only, if anything)
so it can be imported by both ``src/search.py`` and ``scripts/eval/eval_common.py``
without introducing a circular import.
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
