"""Property-based and explicit tests for consensus tally correctness.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**
- WHEN a candidate receives ≥3 ACCEPT verdicts out of 4 → ACCEPTED.
- WHEN a candidate receives ≤2 ACCEPT verdicts out of 4 → REJECTED.
- THE Tally_Consensus function SHALL NOT return DEFERRED for any input.
- THE Tally_Consensus function SHALL accept exactly 4 EvaluatorVerdict objects.
- THE Tally_Consensus function SHALL raise ValueError if len != 4.
- THE Tally_Consensus function SHALL remain a pure function with no side effects.
"""

from unittest.mock import patch

import pytest
from hypothesis import given, settings, strategies as st

from src.models import EvaluatorVerdict


# ---------------------------------------------------------------------------
# Helper: create orchestrator without real AgentInvoker / MCP config
# ---------------------------------------------------------------------------

def _make_orchestrator():
    """Instantiate DreamCycleOrchestrator (all-Kiro laptop profile; construction is subprocess-free)."""
    from src.dream_cycle import DreamCycleOrchestrator
    return DreamCycleOrchestrator()


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

verdict_values = st.sampled_from(["ACCEPT", "REJECT"])

four_verdicts = st.lists(verdict_values, min_size=4, max_size=4)

roles = ["skeptic", "advocate", "epistemologist", "methodologist"]


def _build_evaluator_verdicts(verdict_list: list[str]) -> list[EvaluatorVerdict]:
    """Build 4 EvaluatorVerdict objects from a list of verdict strings."""
    return [
        EvaluatorVerdict(role=roles[i], verdict=verdict_list[i], reasoning=f"reason_{i}")
        for i in range(len(verdict_list))
    ]


# ---------------------------------------------------------------------------
# Property 1: Binary BFT Consensus Tally Correctness
# **Validates: Requirements 2.1, 2.2, 2.3**
# ---------------------------------------------------------------------------

class TestConsensusTallyProperty:
    """Property-based tests for tally_consensus — binary 4-verdict model."""

    @given(verdicts=four_verdicts)
    @settings(max_examples=100, deadline=None)
    def test_tally_returns_correct_binary_mapping(self, verdicts):
        """For any list of 4 verdicts (each ACCEPT or REJECT), tally_consensus
        returns exactly one of ACCEPTED/REJECTED with the correct mapping:
        accept_count ≥ 3 → ACCEPTED, accept_count ≤ 2 → REJECTED.
        DEFERRED is never returned.

        **Validates: Requirements 2.1, 2.2, 2.3**
        """
        from src.dream_cycle.consensus import tally_consensus
        evaluator_verdicts = _build_evaluator_verdicts(verdicts)

        result = tally_consensus(evaluator_verdicts)

        accept_count = sum(1 for v in verdicts if v == "ACCEPT")

        # Binary output only — no DEFERRED
        assert result in ("ACCEPTED", "REJECTED")
        assert result != "DEFERRED"

        if accept_count >= 3:
            assert result == "ACCEPTED"
        else:
            assert result == "REJECTED"


# ---------------------------------------------------------------------------
# Property 2: Tally Input Validation
# **Validates: Requirements 2.4, 2.5**
# ---------------------------------------------------------------------------

class TestTallyInputValidationProperty:
    """Property-based tests for tally_consensus input validation."""

    @given(n=st.integers(min_value=0, max_value=3))
    @settings(max_examples=100, deadline=None)
    def test_too_few_verdicts_raises_value_error(self, n):
        """Lists of length 0-3 raise ValueError.

        **Validates: Requirements 2.4, 2.5**
        """
        from src.dream_cycle.consensus import tally_consensus
        verdicts = [
            EvaluatorVerdict(role="skeptic", verdict="ACCEPT", reasoning="r")
            for _ in range(n)
        ]
        with pytest.raises(ValueError, match=f"Expected 4 verdicts, got {n}"):
            tally_consensus(verdicts)

    @given(n=st.integers(min_value=5, max_value=20))
    @settings(max_examples=100, deadline=None)
    def test_too_many_verdicts_raises_value_error(self, n):
        """Lists of length 5+ raise ValueError.

        **Validates: Requirements 2.4, 2.5**
        """
        from src.dream_cycle.consensus import tally_consensus
        verdicts = [
            EvaluatorVerdict(role="skeptic", verdict="ACCEPT", reasoning="r")
            for _ in range(n)
        ]
        with pytest.raises(ValueError, match=f"Expected 4 verdicts, got {n}"):
            tally_consensus(verdicts)


# ---------------------------------------------------------------------------
# Explicit tests for all 16 combinations of 4 binary verdicts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("votes,expected", [
    ("AAAA", "ACCEPTED"), ("AAAR", "ACCEPTED"), ("AARA", "ACCEPTED"),
    ("ARAA", "ACCEPTED"), ("RAAA", "ACCEPTED"),
    ("AARR", "REJECTED"), ("ARAR", "REJECTED"), ("ARRA", "REJECTED"),
    ("RAAR", "REJECTED"), ("RARA", "REJECTED"), ("RRAA", "REJECTED"),
    ("ARRR", "REJECTED"), ("RARR", "REJECTED"), ("RRAR", "REJECTED"),
    ("RRRA", "REJECTED"), ("RRRR", "REJECTED"),
])
def test_consensus_truth_table(votes, expected):
    """All sixteen binary vote permutations preserve the three-of-four quorum."""
    from src.dream_cycle.consensus import tally_consensus
    verdicts = _build_evaluator_verdicts([
        "ACCEPT" if vote == "A" else "REJECT" for vote in votes
    ])
    assert tally_consensus(verdicts) == expected


# ---------------------------------------------------------------------------
# Property 2: Deduplication Guarantee
# **Validates: Requirements 7.2, 7.3, 7.4**
# ---------------------------------------------------------------------------


class TestDeduplicationGuaranteeProperty:
    """Property-based tests for check_duplicate deduplication logic."""

    @given(
        similarity=st.floats(min_value=0.86, max_value=1.0),
        content=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    )
    @settings(max_examples=20)
    def test_above_threshold_returns_memory_id(self, similarity, content):
        """When similarity > 0.85 for an active non-chunk memory,
        check_duplicate returns that memory's ID.

        **Validates: Requirements 7.2**
        """
        from src.dream_cycle.storage import check_duplicate
        fake_embedding = [0.1] * 1024
        matched_id = "existing-memory-uuid"

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding), \
             patch("src.dream_cycle.storage.search_similar", return_value=[
                 {"id": matched_id, "similarity": similarity, "parent_id": None, "status": "active"},
             ]):

            result = check_duplicate(content, threshold=0.85)
            assert result == matched_id

    @given(
        similarity=st.floats(min_value=0.0, max_value=0.84),
        content=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    )
    @settings(max_examples=20)
    def test_below_threshold_returns_none(self, similarity, content):
        """When similarity <= 0.85 for all memories,
        check_duplicate returns None.

        **Validates: Requirements 7.3**
        """
        from src.dream_cycle.storage import check_duplicate
        fake_embedding = [0.1] * 1024

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding), \
             patch("src.dream_cycle.storage.search_similar", return_value=[
                 {"id": "some-id", "similarity": similarity, "parent_id": None, "status": "active"},
             ]):

            result = check_duplicate(content, threshold=0.85)
            assert result is None

    @given(
        similarity=st.floats(min_value=0.90, max_value=1.0),
        content=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    )
    @settings(max_examples=20)
    def test_chunk_memories_are_skipped(self, similarity, content):
        """Chunk memories (parent_id not null) are skipped even when
        similarity exceeds threshold.

        **Validates: Requirements 7.4**
        """
        from src.dream_cycle.storage import check_duplicate
        fake_embedding = [0.1] * 1024

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding), \
             patch("src.dream_cycle.storage.search_similar", return_value=[
                 # This is a chunk memory (parent_id is set) — should be skipped
                 {"id": "chunk-id", "similarity": similarity, "parent_id": "parent-uuid", "status": "active"},
             ]):

            result = check_duplicate(content, threshold=0.85)
            assert result is None


class TestDeduplicationExplicit:
    """Explicit tests for deduplication edge cases."""

    def test_no_results_returns_none(self):
        """When search_similar returns empty list, check_duplicate returns None.

        **Validates: Requirements 7.3**
        """
        from src.dream_cycle.storage import check_duplicate

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.search_similar", return_value=[]):

            result = check_duplicate("some content")
            assert result is None

    def test_mixed_results_skips_chunks_finds_active(self):
        """When results contain both chunks and active memories,
        only active non-chunk memories are considered.

        **Validates: Requirements 7.2, 7.4**
        """
        from src.dream_cycle.storage import check_duplicate

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.search_similar", return_value=[
                 # Chunk with high similarity — should be skipped
                 {"id": "chunk-1", "similarity": 0.99, "parent_id": "parent-1", "status": "active"},
                 # Active non-chunk below threshold — should not match
                 {"id": "active-1", "similarity": 0.80, "parent_id": None, "status": "active"},
                 # Active non-chunk above threshold — should match
                 {"id": "active-2", "similarity": 0.90, "parent_id": None, "status": "active"},
             ]):

            result = check_duplicate("test content")
            assert result == "active-2"

    def test_exact_threshold_not_matched(self):
        """Similarity exactly at 0.85 does NOT trigger deduplication
        (threshold is strictly greater than).

        **Validates: Requirements 7.2**
        """
        from src.dream_cycle.storage import check_duplicate

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.search_similar", return_value=[
                 {"id": "border-id", "similarity": 0.85, "parent_id": None, "status": "active"},
             ]):

            result = check_duplicate("borderline content")
            assert result is None


# ---------------------------------------------------------------------------
# Standalone tally_consensus function tests (no orchestrator needed)
# **Validates: Requirements 2.1, 2.2, 2.4, 2.5**
# ---------------------------------------------------------------------------

class TestStandaloneTallyConsensus:
    """Test tally_consensus imported directly from src.dream_cycle.consensus."""

    def test_import_works(self):
        """Verify standalone import path resolves correctly."""
        from src.dream_cycle.consensus import tally_consensus as fn
        assert callable(fn)


    def test_wrong_length_raises_value_error(self):
        """Non-4 length → ValueError. **Validates: Requirements 2.4, 2.5**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts_3 = _build_evaluator_verdicts(["ACCEPT", "ACCEPT", "ACCEPT"])
        with pytest.raises(ValueError, match="Expected 4 verdicts, got 3"):
            tally_consensus(verdicts_3)
