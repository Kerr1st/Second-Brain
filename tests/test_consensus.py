"""Property-based and explicit tests for consensus tally correctness.

**Validates: Requirements 1.4, 2.1, 2.2, 2.3**
- WHEN all evaluators return verdicts for a candidate, THE Orchestrator SHALL
  tally consensus: 3/3 ACCEPT = ACCEPTED, 2/3 ACCEPT = DEFERRED, otherwise REJECTED.
- WHEN a candidate receives 3 ACCEPT verdicts → ACCEPTED.
- WHEN a candidate receives exactly 2 ACCEPT verdicts → DEFERRED.
- WHEN a candidate receives fewer than 2 ACCEPT verdicts → REJECTED.
"""

from unittest.mock import patch

import pytest
from hypothesis import given, settings, strategies as st

from src.models import EvaluatorVerdict


# ---------------------------------------------------------------------------
# Helper: create orchestrator without real AgentInvoker / MCP config
# ---------------------------------------------------------------------------

def _make_orchestrator():
    """Instantiate DreamCycleOrchestrator with mocked __init__ dependencies."""
    with patch("src.dream_cycle.orchestrator.AgentInvoker"), \
         patch("src.dream_cycle.orchestrator.os.path.expanduser", return_value="/fake/mcp.json"):
        from src.dream_cycle import DreamCycleOrchestrator
        return DreamCycleOrchestrator()


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

verdict_values = st.sampled_from(["ACCEPT", "REJECT"])

three_verdicts = st.lists(verdict_values, min_size=3, max_size=3)

roles = ["skeptic", "advocate", "epistemologist"]


def _build_evaluator_verdicts(verdict_list: list[str]) -> list[EvaluatorVerdict]:
    """Build 3 EvaluatorVerdict objects from a list of verdict strings."""
    return [
        EvaluatorVerdict(role=roles[i], verdict=verdict_list[i], reasoning=f"reason_{i}")
        for i in range(3)
    ]


# ---------------------------------------------------------------------------
# Property 1: Consensus Tally Correctness
# **Validates: Requirements 1.4, 2.1, 2.2, 2.3**
# ---------------------------------------------------------------------------

class TestConsensusTallyProperty:
    """Property-based tests for tally_consensus."""

    @given(verdicts=three_verdicts)
    @settings(deadline=None)
    def test_tally_returns_correct_mapping(self, verdicts):
        """For any list of 3 verdicts (each ACCEPT or REJECT), tally_consensus
        returns exactly one of ACCEPTED/DEFERRED/REJECTED with the correct mapping:
        3 ACCEPT → ACCEPTED, 2 ACCEPT → DEFERRED, 0-1 ACCEPT → REJECTED.

        **Validates: Requirements 1.4, 2.1, 2.2, 2.3**
        """
        from src.dream_cycle.consensus import tally_consensus
        evaluator_verdicts = _build_evaluator_verdicts(verdicts)

        result = tally_consensus(evaluator_verdicts)

        accept_count = sum(1 for v in verdicts if v == "ACCEPT")

        assert result in ("ACCEPTED", "DEFERRED", "REJECTED")

        if accept_count == 3:
            assert result == "ACCEPTED"
        elif accept_count == 2:
            assert result == "DEFERRED"
        else:
            assert result == "REJECTED"


# ---------------------------------------------------------------------------
# Explicit tests for all 8 combinations of 3 binary verdicts
# ---------------------------------------------------------------------------

class TestConsensusTallyExplicit:
    """Explicit tests covering all 8 combinations of 3 binary verdicts."""

    def test_3_accept(self):
        """AAA → ACCEPTED. **Validates: Requirements 2.1**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "ACCEPT", "ACCEPT"])
        assert tally_consensus(verdicts) == "ACCEPTED"

    def test_2_accept_reject_last(self):
        """AAR → DEFERRED. **Validates: Requirements 2.2**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "ACCEPT", "REJECT"])
        assert tally_consensus(verdicts) == "DEFERRED"

    def test_2_accept_reject_middle(self):
        """ARA → DEFERRED. **Validates: Requirements 2.2**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "REJECT", "ACCEPT"])
        assert tally_consensus(verdicts) == "DEFERRED"

    def test_2_accept_reject_first(self):
        """RAA → DEFERRED. **Validates: Requirements 2.2**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["REJECT", "ACCEPT", "ACCEPT"])
        assert tally_consensus(verdicts) == "DEFERRED"

    def test_1_accept_first(self):
        """ARR → REJECTED. **Validates: Requirements 2.3**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "REJECT", "REJECT"])
        assert tally_consensus(verdicts) == "REJECTED"

    def test_1_accept_middle(self):
        """RAR → REJECTED. **Validates: Requirements 2.3**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["REJECT", "ACCEPT", "REJECT"])
        assert tally_consensus(verdicts) == "REJECTED"

    def test_1_accept_last(self):
        """RRA → REJECTED. **Validates: Requirements 2.3**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["REJECT", "REJECT", "ACCEPT"])
        assert tally_consensus(verdicts) == "REJECTED"

    def test_0_accept(self):
        """RRR → REJECTED. **Validates: Requirements 2.3**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["REJECT", "REJECT", "REJECT"])
        assert tally_consensus(verdicts) == "REJECTED"


# ---------------------------------------------------------------------------
# Helper: create orchestrator (reuse from above)
# ---------------------------------------------------------------------------

# _make_orchestrator is already defined above


# ---------------------------------------------------------------------------
# Property 2: Deduplication Guarantee
# **Validates: Requirements 7.2, 7.3, 7.4**
# ---------------------------------------------------------------------------

from hypothesis import given, settings, strategies as st


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
# **Validates: Requirements 2.4, 2.5**
# **Feature: dream-cycle-decomposition, Property 1: Consensus tally mapping is preserved after extraction**
# ---------------------------------------------------------------------------

class TestStandaloneTallyConsensus:
    """Test tally_consensus imported directly from src.dream_cycle.consensus."""

    def test_import_works(self):
        """Verify standalone import path resolves correctly."""
        from src.dream_cycle.consensus import tally_consensus as fn
        assert callable(fn)

    def test_3_accept_returns_accepted(self):
        """3 ACCEPT → ACCEPTED. **Validates: Requirements 2.5**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "ACCEPT", "ACCEPT"])
        assert tally_consensus(verdicts) == "ACCEPTED"

    def test_2_accept_returns_deferred(self):
        """2 ACCEPT → DEFERRED. **Validates: Requirements 2.5**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "ACCEPT", "REJECT"])
        assert tally_consensus(verdicts) == "DEFERRED"

    def test_1_accept_returns_rejected(self):
        """1 ACCEPT → REJECTED. **Validates: Requirements 2.5**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["ACCEPT", "REJECT", "REJECT"])
        assert tally_consensus(verdicts) == "REJECTED"

    def test_0_accept_returns_rejected(self):
        """0 ACCEPT → REJECTED. **Validates: Requirements 2.5**"""
        from src.dream_cycle.consensus import tally_consensus
        verdicts = _build_evaluator_verdicts(["REJECT", "REJECT", "REJECT"])
        assert tally_consensus(verdicts) == "REJECTED"
