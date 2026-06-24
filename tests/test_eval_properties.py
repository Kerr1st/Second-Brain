"""Property-based tests for evaluation utilities (eval_common.py).

Tests pure functions: compute_metrics and rerank_with_overrides.
No database or Bedrock calls.
"""

import math
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval.eval_common import compute_metrics, rerank_with_overrides, PRODUCTION_WEIGHTS


# --- Strategies ---

rank_position = st.one_of(st.none(), st.integers(min_value=1, max_value=100))

eval_result = st.fixed_dictionaries({
    "rank_position": rank_position,
    "category": st.sampled_from(["factual", "conceptual", "procedural", "golden"]),
})

# A result dict with intermediate rerank signals (as set by rerank())
reranked_result = st.fixed_dictionaries({
    "id": st.uuids().map(str),
    "rrf_score": st.floats(min_value=0, max_value=0.05),
    "_overlap": st.floats(min_value=0, max_value=1),
    "_title_overlap": st.floats(min_value=0, max_value=1),
    "_recency": st.floats(min_value=0, max_value=1),
    "_length_score": st.floats(min_value=0, max_value=1),
    "_depth_score": st.floats(min_value=0, max_value=1),
    "_type_boost": st.sampled_from([0.0, 0.06]),
    "_mem_class_boost": st.sampled_from([0.0, 0.02, 0.04]),
    "_spacing_bonus": st.floats(min_value=0, max_value=1),
    "_project_penalty": st.sampled_from([0.0, -0.15]),
    "_reinforcement": st.floats(min_value=0, max_value=0.1),
    "access_count": st.integers(min_value=0, max_value=100),
    "rerank_score": st.floats(min_value=-1, max_value=2),
})


# --- Property 1: MRR bounds ---

class TestComputeMetricsMRRBounds:
    @given(st.lists(eval_result, min_size=1, max_size=50))
    @settings(max_examples=200)
    def test_mrr_in_unit_interval(self, results):
        metrics = compute_metrics(results)
        assert 0.0 <= metrics["mrr"] <= 1.0


# --- Property 2: Hit@k monotonicity ---

class TestComputeMetricsHitMonotonicity:
    @given(st.lists(eval_result, min_size=1, max_size=50))
    @settings(max_examples=200)
    def test_hit_at_k_monotonically_nondecreasing(self, results):
        metrics = compute_metrics(results)
        assert metrics.get("hit_at_1", 0) <= metrics.get("hit_at_3", 0)
        assert metrics.get("hit_at_3", 0) <= metrics.get("hit_at_5", 0)
        assert metrics.get("hit_at_5", 0) <= metrics.get("hit_at_10", 0)


# --- Property 3: All-zero overrides produce lower-or-equal scores ---

class TestRerankOverridesAllZero:
    @given(st.lists(reranked_result, min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_zeroing_signals_produces_finite_scores(self, results):
        """Zeroing all additive V2 signals still produces finite rerank scores.

        Note: we can't assert zeroed <= baseline universally because project_penalty
        is negative — zeroing it *increases* scores for penalized results.
        """
        zeroed = [dict(r) for r in results]
        rerank_with_overrides(zeroed, {
            "depth": 0.0, "type_boost": 0.0, "mem_class_boost": 0.0,
            "reinforcement_coeff": 0.0, "project_penalty": 0.0,
        })

        for z in zeroed:
            assert math.isfinite(z["rerank_score"])


# --- Property 4: Identity overrides produce identical scores ---

class TestRerankOverridesIdentity:
    @given(st.lists(reranked_result, min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_empty_overrides_preserves_scores(self, results):
        # Run twice with empty overrides — scores should be identical
        run1 = [dict(r) for r in results]
        rerank_with_overrides(run1, {})

        run2 = [dict(r) for r in results]
        rerank_with_overrides(run2, {})

        scores1 = sorted((str(r["id"]), r["rerank_score"]) for r in run1)
        scores2 = sorted((str(r["id"]), r["rerank_score"]) for r in run2)
        for (id1, s1), (id2, s2) in zip(scores1, scores2):
            assert id1 == id2
            assert abs(s1 - s2) < 1e-10
