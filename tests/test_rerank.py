"""Property tests for rerank formula components (Properties 3, 8, 9, 10).

Feature: retrieval-quality
"""

import uuid
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, assume, strategies as st

from src.search import compute_spacing_bonus, rerank


class TestSpacingBonusFormula:
    """Feature: retrieval-quality, Property 3: Spacing Bonus Formula

    For any non-negative days_since_last_access, the spacing bonus shall equal
    min(1.0, days_since_last_access / 7.0). When last_accessed_at is NULL, the
    spacing bonus shall be 1.0. This implies: 0 days → 0.0, 3.5 days → 0.5,
    7+ days → 1.0.

    **Validates: Requirements 3.2, 3.3, 3.4, 3.5**
    """

    @given(days=st.floats(min_value=0.0, max_value=365.0))
    @settings(max_examples=100)
    def test_spacing_bonus_matches_formula(self, days):
        """For any non-negative days, bonus == min(1.0, days / 7.0)."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        last_accessed = now - timedelta(days=days)
        result = compute_spacing_bonus(last_accessed, now=now)
        expected = min(1.0, days / 7.0)
        assert abs(result - expected) < 1e-9, (
            f"days={days}: got {result}, expected {expected}"
        )

    def test_null_last_accessed_returns_one(self):
        """NULL last_accessed_at → spacing_bonus = 1.0."""
        assert compute_spacing_bonus(None) == 1.0

    def test_zero_days_returns_zero(self):
        """0 days since last access → spacing_bonus = 0.0."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert compute_spacing_bonus(now, now=now) == 0.0

    def test_seven_plus_days_returns_one(self):
        """7+ days since last access → spacing_bonus = 1.0."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        last_accessed = now - timedelta(days=10)
        assert compute_spacing_bonus(last_accessed, now=now) == 1.0

    def test_half_week_returns_half(self):
        """3.5 days since last access → spacing_bonus = 0.5."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        last_accessed = now - timedelta(days=3.5)
        result = compute_spacing_bonus(last_accessed, now=now)
        assert abs(result - 0.5) < 1e-9

    @given(days=st.floats(min_value=0.0, max_value=365.0))
    @settings(max_examples=100)
    def test_spacing_bonus_in_unit_interval(self, days):
        """Spacing bonus is always in [0.0, 1.0]."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        last_accessed = now - timedelta(days=days)
        result = compute_spacing_bonus(last_accessed, now=now)
        assert 0.0 <= result <= 1.0, f"Out of range: {result}"


class TestSpacingBonusOrdering:
    """Feature: retrieval-quality, Property 9: Spacing Bonus Ordering

    For any two memories that are identical in all reranking components but
    differ only in last_accessed_at, the memory with the higher spacing bonus
    (older last access) shall receive a higher rerank score.

    **Validates: Requirements 3.7**
    """

    @given(
        recent_days=st.floats(min_value=0.0, max_value=6.0),
        older_days=st.floats(min_value=0.1, max_value=365.0),
        access_count=st.integers(min_value=1, max_value=1000),
        rrf_score=st.floats(min_value=0.0, max_value=1.0),
    )
    @settings(max_examples=100)
    def test_older_access_gets_higher_rerank_score(
        self, recent_days, older_days, access_count, rrf_score
    ):
        """Two memories identical except last_accessed_at — older access ranks higher."""
        assume(older_days > recent_days + 0.01)
        # Ensure spacing bonuses actually differ (both cap at 1.0 after 7 days)
        recent_bonus = min(1.0, recent_days / 7.0)
        older_bonus = min(1.0, older_days / 7.0)
        assume(older_bonus > recent_bonus + 1e-9)

        # Use actual current time so rerank()'s internal datetime.now() aligns
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(days=30)
        shared_content = "This is shared test content for spacing bonus ordering"
        shared_title = "Shared Title"

        base = {
            "title": shared_title,
            "content": shared_content,
            "type": "idea",
            "created_at": created_at,
            "access_count": access_count,
            "rrf_score": rrf_score,
            "metadata": {},
        }

        recent_memory = {
            **base,
            "id": str(uuid.uuid4()),
            "last_accessed_at": now - timedelta(days=recent_days),
        }
        older_memory = {
            **base,
            "id": str(uuid.uuid4()),
            "last_accessed_at": now - timedelta(days=older_days),
        }

        results = rerank([recent_memory, older_memory], "test query")

        older_score = older_memory["rerank_score"]
        recent_score = recent_memory["rerank_score"]
        assert older_score > recent_score, (
            f"Older access ({older_days}d) score {older_score} should be > "
            f"recent access ({recent_days}d) score {recent_score}"
        )


class TestClassificationOrdering:
    """Feature: retrieval-quality, Property 10: Classification Ordering

    For any two memories that are identical in all reranking components except
    mem_class, where one has mem_class="semantic" and the other has
    mem_class="episodic" (or None), the semantic memory shall receive a higher
    rerank score.

    **Validates: Requirements 4.10**
    """

    @given(
        rrf_score=st.floats(min_value=0.0, max_value=1.0),
        access_count=st.integers(min_value=0, max_value=1000),
        days_old=st.floats(min_value=0.0, max_value=365.0),
        mem_type=st.sampled_from(["idea", "synthesis", "source", "research", "insight"]),
        episodic_class=st.sampled_from(["episodic", None]),
    )
    @settings(max_examples=100)
    def test_semantic_ranks_above_episodic(
        self, rrf_score, access_count, days_old, mem_type, episodic_class
    ):
        """Two memories identical except mem_class — semantic ranks above episodic."""
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(days=days_old)
        shared_content = "Shared content for classification ordering test"
        shared_title = "Shared Title"

        base = {
            "title": shared_title,
            "content": shared_content,
            "type": mem_type,
            "created_at": created_at,
            "access_count": access_count,
            "rrf_score": rrf_score,
            "metadata": {},
            "last_accessed_at": None,
        }

        semantic_memory = {
            **base,
            "id": str(uuid.uuid4()),
            "mem_class": "semantic",
        }
        episodic_memory = {
            **base,
            "id": str(uuid.uuid4()),
            "mem_class": episodic_class,
        }

        results = rerank([semantic_memory, episodic_memory], "test query")

        semantic_score = semantic_memory["rerank_score"]
        episodic_score = episodic_memory["rerank_score"]
        assert semantic_score > episodic_score, (
            f"Semantic score {semantic_score} should be > "
            f"episodic score {episodic_score} "
            f"(mem_class: semantic vs {episodic_class})"
        )


class TestCompleteRerankFormula:
    """Feature: retrieval-quality, Property 8: Complete Rerank Formula

    For any memory with known component values, rerank_score equals the V2
    formula within float tolerance:

    rerank_score = 0.30 * rrf_score + 0.18 * token_overlap + 0.18 * title_overlap
                 + 0.12 * recency + 0.08 * length_score + 0.05 * depth_score
                 + type_boost + mem_class_boost
                 + 0.03 * log1p(access_count) * spacing_bonus
                 + project_penalty

    **Validates: Requirements 7.1–7.7, 3.6, 4.7–4.9, 5.7, 6.5–6.7**
    """

    @given(
        rrf_score=st.floats(min_value=0.0, max_value=1.0),
        depth_score=st.floats(min_value=0.0, max_value=1.0),
        access_count=st.integers(min_value=0, max_value=1000),
        days_since_access=st.one_of(
            st.none(),
            st.floats(min_value=0.0, max_value=365.0),
        ),
        days_old=st.floats(min_value=0.0, max_value=365.0),
        mem_type=st.sampled_from(["idea", "synthesis", "insight", "decision", "source", "research"]),
        mem_class=st.one_of(st.none(), st.sampled_from(["semantic", "episodic", "procedural"])),
        query_project=st.one_of(st.none(), st.just("project-a")),
        mem_project=st.one_of(st.none(), st.just("project-a"), st.just("project-b")),
    )
    @settings(max_examples=100)
    def test_rerank_score_matches_v2_formula(
        self,
        rrf_score,
        depth_score,
        access_count,
        days_since_access,
        days_old,
        mem_type,
        mem_class,
        query_project,
        mem_project,
    ):
        """For any memory with known component values, rerank_score matches V2 formula."""
        import math
        import re
        from unittest.mock import patch as mock_patch

        # Fixed reference time for both test computation and rerank()
        fixed_now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        created_at = fixed_now - timedelta(days=days_old)

        if days_since_access is not None:
            last_accessed_at = fixed_now - timedelta(days=days_since_access)
        else:
            last_accessed_at = None

        # Use deterministic content/title so we can compute overlap independently
        query_text = "alpha beta gamma"
        content = "alpha beta delta epsilon"
        title = "alpha gamma"

        memory = {
            "id": str(uuid.uuid4()),
            "rrf_score": rrf_score,
            "content": content,
            "title": title,
            "type": mem_type,
            "status": "active",
            "created_at": created_at,
            "access_count": access_count,
            "last_accessed_at": last_accessed_at,
            "mem_class": mem_class,
            "project": mem_project,
            "metadata": {"depth_score": depth_score},
            "encoding_context": None,
        }

        # Independently compute expected score using the same logic as rerank()
        query_tokens = set(re.sub(r"[^\w\s]", "", query_text).lower().split())
        content_tokens = set(re.sub(r"[^\w\s]", "", content[:2000]).lower().split())
        title_tokens = set(re.sub(r"[^\w\s]", "", title).lower().split())
        all_tokens = content_tokens | title_tokens

        expected_overlap = len(query_tokens & all_tokens) / max(1, len(query_tokens))
        expected_title_overlap = len(query_tokens & title_tokens) / max(1, len(query_tokens))

        # Context overlap (None encoding_context → 0.0)
        expected_context_overlap = 0.0

        # Recency — power law: R = (1 + t/S)^(-b)
        expected_days_old = max(0, (fixed_now - created_at).total_seconds() / 86400)
        stability = 30.0 + 10.0 * math.log1p(access_count)
        decay_b = 0.8
        expected_recency = (1.0 + expected_days_old / stability) ** (-decay_b)

        # Length score
        expected_length_score = min(1.0, len(content_tokens) / 80)

        # Type boost
        expected_type_boost = 0.06 if mem_type in ("idea", "synthesis", "insight", "decision") else 0.0

        # Mem class boost
        expected_mem_class_boost = {"semantic": 0.04, "procedural": 0.02}.get(mem_class, 0.0)

        # Spacing bonus
        expected_spacing = compute_spacing_bonus(last_accessed_at, now=fixed_now)

        # Reinforcement
        expected_reinforcement = 0.03 * math.log1p(access_count) * expected_spacing

        # Project penalty
        expected_project_penalty = -0.15 if (query_project and mem_project and mem_project != query_project) else 0.0

        # Superseded penalty (status is "active" → 0.0)
        expected_superseded_penalty = 0.0

        # Staleness penalty (active forgetting)
        if access_count == 0 and created_at:
            days_unretrieved = max(0, (fixed_now - created_at).total_seconds() / 86400)
            if days_unretrieved > 90:
                expected_staleness_penalty = -0.05 * min(1.0, (days_unretrieved - 90) / 180)
            else:
                expected_staleness_penalty = 0.0
        else:
            expected_staleness_penalty = 0.0

        expected_score = (
            0.30 * rrf_score
            + 0.18 * expected_overlap
            + 0.18 * expected_title_overlap
            + 0.10 * expected_context_overlap
            + 0.12 * expected_recency
            + 0.08 * expected_length_score
            + 0.05 * depth_score
            + expected_type_boost
            + expected_mem_class_boost
            + expected_reinforcement
            + expected_project_penalty
            + expected_superseded_penalty
            + expected_staleness_penalty
        )

        # Patch datetime.now in src.search to return fixed_now, keeping datetime
        # class itself functional for .replace() and timedelta operations
        original_datetime = datetime

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        with mock_patch("src.search.datetime", FrozenDatetime):
            rerank([memory], query_text, query_project=query_project)

        actual_score = memory["rerank_score"]
        assert abs(actual_score - expected_score) < 1e-9, (
            f"Score mismatch: got {actual_score}, expected {expected_score}\n"
            f"  rrf={rrf_score}, overlap={expected_overlap}, title_overlap={expected_title_overlap}\n"
            f"  context_overlap={expected_context_overlap}, recency={expected_recency}\n"
            f"  length={expected_length_score}, depth={depth_score}\n"
            f"  type_boost={expected_type_boost}, class_boost={expected_mem_class_boost}\n"
            f"  reinforcement={expected_reinforcement}, project_penalty={expected_project_penalty}\n"
            f"  superseded_penalty={expected_superseded_penalty}, staleness_penalty={expected_staleness_penalty}"
        )


# --- Edge-branch coverage: encoding_context overlap + active-forgetting staleness ---

def _mem(**over):
    base = {
        "id": str(uuid.uuid4()), "rrf_score": 0.5, "content": "some neutral body text",
        "title": "neutral title", "type": "idea", "status": "active",
        "created_at": datetime.now(timezone.utc), "access_count": 0,
        "last_accessed_at": None, "mem_class": "semantic", "project": None,
        "metadata": {}, "encoding_context": None,
    }
    base.update(over)
    return base


class TestRerankEncodingContextOverlap:
    """The encoding_context overlap branch (contextual reinstatement) is exercised."""

    def test_context_overlap_positive_when_encoding_context_matches_query(self):
        mem = _mem(encoding_context="quantum cryptography lattice scheme")
        rerank([mem], "quantum cryptography")
        assert mem["_context_overlap"] > 0

    def test_context_overlap_zero_without_encoding_context(self):
        mem = _mem(encoding_context=None)
        rerank([mem], "quantum cryptography")
        assert mem["_context_overlap"] == 0.0


class TestRerankStalenessPenalty:
    """The active-forgetting staleness penalty branch is exercised."""

    def test_penalty_applied_for_old_unaccessed_memory(self):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        mem = _mem(created_at=old, access_count=0, last_accessed_at=None)
        rerank([mem], "anything here")
        assert mem["_staleness_penalty"] < 0

    def test_no_penalty_for_recent_memory(self):
        recent = datetime.now(timezone.utc) - timedelta(days=10)
        mem = _mem(created_at=recent, access_count=0)
        rerank([mem], "anything here")
        assert mem["_staleness_penalty"] == 0.0

    def test_no_penalty_when_memory_has_been_accessed(self):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        mem = _mem(created_at=old, access_count=5)
        rerank([mem], "anything here")
        assert mem["_staleness_penalty"] == 0.0
