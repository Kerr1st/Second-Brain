"""End-to-end integration test for the Dream Cycle pipeline with mock agents.

Runs the orchestrator end-to-end with deterministic mock data, verifying:
- Run record created and completed
- Feedback injection built
- Explorer invoked with correct prompt
- Thinker invoked for each slice
- All 4 evaluators invoked for each candidate
- Consensus tallied correctly (binary BFT: ≥3/4 ACCEPTED, ≤2/4 REJECTED)
- Deduplication checked for accepted candidates
- Memory created for accepted candidates
- Candidates stored with correct verdicts
- Digest generated

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 3.1, 6.5, 7.1, 8.1, 12.1
"""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import MagicMock, patch, call, ANY

import pytest

from src.models import (
    CandidateInsight,
    DreamCycleResult,
    EvaluatorVerdict,
    MemorySlice,
)


# ---------------------------------------------------------------------------
# Deterministic mock data
# ---------------------------------------------------------------------------

MOCK_RUN_ID = "integration-run-001"

SLICE_1 = MemorySlice(
    name="Cross-project DB patterns",
    strategy="cross_project_collision",
    memory_ids=["mem-a1", "mem-a2", "mem-a3"],
    memory_titles=["DB Migration A", "DB Migration B", "DB Migration C"],
    hypothesis="Projects share implicit migration conventions",
)

SLICE_2 = MemorySlice(
    name="Stale synthesis check",
    strategy="stale_synthesis_check",
    memory_ids=["mem-b1", "mem-b2"],
    memory_titles=["Old Synthesis 1", "Old Synthesis 2"],
    hypothesis="Some syntheses may be outdated",
)

# Candidate 1: will get 4/4 ACCEPT → ACCEPTED
CANDIDATE_1 = CandidateInsight(
    title="Implicit DB Migration Convention",
    type="insight",
    operation="CREATE",
    schema_operation="assimilation",
    schema_note="Reinforces existing pattern",
    confidence="high",
    confidence_reasoning="Strong evidence across 3 projects",
    content="All three projects follow an implicit convention of...",
    source_memories=["mem-a1", "mem-a2", "mem-a3"],
    relationships=[
        {"target_id": "mem-a1", "relation_type": "supports", "note": "evidence"},
    ],
    strategy_that_found_it="cross_project_collision",
)

# Candidate 2: will get 2 ACCEPT, 2 REJECT → REJECTED (binary BFT: ≤2/4 = REJECTED)
CANDIDATE_2 = CandidateInsight(
    title="Outdated Caching Strategy",
    type="insight",
    operation="CREATE",
    schema_operation="accommodation",
    schema_note="Challenges existing assumption",
    confidence="medium",
    confidence_reasoning="Moderate evidence",
    content="The caching strategy documented in 2023 is now outdated...",
    source_memories=["mem-b1", "mem-b2"],
    relationships=[],
    strategy_that_found_it="stale_synthesis_check",
)

MOCK_MEMORY_STATS = {
    "total_count": 150,
    "recent_activity": 10,
    "date_range": {"min": None, "max": None},
    "type_distribution": {"insight": 80, "connection": 40, "question": 30},
}


# ---------------------------------------------------------------------------
# Evaluator verdict builders
# ---------------------------------------------------------------------------

def _verdict_accept(role: str, reasoning: str = "Approved") -> EvaluatorVerdict:
    return EvaluatorVerdict(role=role, verdict="ACCEPT", reasoning=reasoning)


def _verdict_reject(role: str, reasoning: str = "Rejected") -> EvaluatorVerdict:
    return EvaluatorVerdict(role=role, verdict="REJECT", reasoning=reasoning)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_orchestrator():
    """Instantiate DreamCycleOrchestrator (all-Kiro laptop profile; construction is subprocess-free)."""
    from src.dream_cycle import DreamCycleOrchestrator
    orch = DreamCycleOrchestrator()
    return orch


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestFullPipelineIntegration:
    """End-to-end integration test with deterministic mock agents.

    Mocks the orchestrator's agent-facing methods (invoke_explorer,
    invoke_thinker, invoke_evaluator) to return deterministic outputs,
    and mocks all DB operations. Then runs the orchestrator end-to-end
    and verifies every step of the pipeline.
    """

    def _run_pipeline(self):
        """Execute the full pipeline with all mocks wired up.

        Returns (result, mocks_dict) for assertions.
        """
        orch = _make_orchestrator()

        # Track evaluator invocations
        evaluator_calls = []

        def mock_invoke_evaluator(candidate, role):
            evaluator_calls.append({"candidate_title": candidate.title, "role": role})
            if candidate.title == "Implicit DB Migration Convention":
                return _verdict_accept(role, f"{role} approves: solid evidence")
            elif candidate.title == "Outdated Caching Strategy":
                if role in ("epistemologist", "methodologist"):
                    return _verdict_reject(role, "Insufficient novelty" if role == "epistemologist" else "Weak methodology")
                return _verdict_accept(role, f"{role} approves: relevant")
            return _verdict_reject(role, "unknown")

        # Track thinker invocations
        thinker_calls = []

        def mock_invoke_thinker(slice_obj):
            thinker_calls.append({"slice_name": slice_obj.name})
            if slice_obj.name == "Cross-project DB patterns":
                return [CANDIDATE_1]
            elif slice_obj.name == "Stale synthesis check":
                return [CANDIDATE_2]
            return []

        # Track explorer invocations
        explorer_calls = []

        def mock_invoke_explorer(feedback, run_type, scope=None, stats=None):
            explorer_calls.append({"feedback": feedback, "run_type": run_type, "scope": scope})
            return [SLICE_1, SLICE_2]

        # Wire mock methods
        with patch.object(orch, "invoke_explorer", side_effect=mock_invoke_explorer) as mock_explorer, \
             patch.object(orch, "invoke_thinker", side_effect=mock_invoke_thinker) as mock_thinker, \
             patch.object(orch, "invoke_evaluator", side_effect=mock_invoke_evaluator) as mock_evaluator:

            # Mock all DB operations
            with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_dc_db, \
                 patch("src.dream_cycle.digest.dream_cycle_db", mock_dc_db), \
                 patch("src.dream_cycle.feedback.dream_cycle_db", mock_dc_db), \
                 patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024) as mock_embed, \
                 patch("src.dream_cycle.storage.create_memory", return_value="new-memory-001") as mock_create_mem, \
                 patch("src.dream_cycle.storage.create_relationship") as mock_create_rel, \
                 patch("src.dream_cycle.storage.search_similar", return_value=[]) as mock_search, \
                 patch("src.dream_cycle.storage.update_memory") as mock_update_mem, \
                 patch("src.dream_cycle.storage.get_memory") as mock_get_mem:

                # Configure dream_cycle_db mocks
                mock_dc_db.create_run.return_value = MOCK_RUN_ID
                mock_dc_db.get_memory_stats.return_value = MOCK_MEMORY_STATS
                mock_dc_db.get_recent_rejections.return_value = []
                mock_dc_db.get_user_rejections.return_value = []
                mock_dc_db.get_accepted_dissents.return_value = []
                mock_dc_db.store_candidate.return_value = "candidate-uuid-001"
                mock_dc_db.should_run_briefing.return_value = True

                # Mock digest file writing
                with patch("pathlib.Path.mkdir"), \
                     patch("pathlib.Path.write_text"):
                    result = orch.run(run_type="scheduled")

                mocks = {
                    "dc_db": mock_dc_db,
                    "embed": mock_embed,
                    "create_mem": mock_create_mem,
                    "create_rel": mock_create_rel,
                    "search": mock_search,
                    "update_mem": mock_update_mem,
                    "get_mem": mock_get_mem,
                    "explorer": mock_explorer,
                    "thinker": mock_thinker,
                    "evaluator": mock_evaluator,
                    "explorer_calls": explorer_calls,
                    "thinker_calls": thinker_calls,
                    "evaluator_calls": evaluator_calls,
                }

                return result, mocks

    def test_result_counts(self):
        """Verify the final DreamCycleResult has correct candidate counts.

        **Validates: Requirements 1.4, 1.5**
        """
        result, _ = self._run_pipeline()

        assert result.candidates_generated == 2
        assert result.candidates_accepted == 1
        assert result.candidates_rejected == 1
        assert result.aborted_early is False

    def test_run_record_created_and_completed(self):
        """Verify run record is created at start and completed at end.

        **Validates: Requirements 1.1, 1.5**
        """
        result, mocks = self._run_pipeline()

        # Run record created
        mocks["dc_db"].create_run.assert_called_once_with("scheduled", backend_provenance=ANY)
        assert result.run_id == MOCK_RUN_ID

        # Run record completed with correct stats
        mocks["dc_db"].complete_run.assert_called_once()
        complete_kwargs = mocks["dc_db"].complete_run.call_args[1]
        stats = complete_kwargs["stats"]
        assert stats["candidates_generated"] == 2
        assert stats["candidates_accepted"] == 1
        assert stats["candidates_rejected"] == 1

    def test_feedback_injection_built(self):
        """Verify feedback injection was queried from DB.

        **Validates: Requirement 4.1**
        """
        _, mocks = self._run_pipeline()

        mocks["dc_db"].get_recent_rejections.assert_called_once_with(n_cycles=3)
        mocks["dc_db"].get_user_rejections.assert_called_once_with(n_cycles=3)

    def test_explorer_invoked_with_correct_prompt(self):
        """Verify Explorer was invoked with feedback and run_type.

        **Validates: Requirements 1.2, 3.1**
        """
        _, mocks = self._run_pipeline()

        assert len(mocks["explorer_calls"]) == 1
        explorer_call = mocks["explorer_calls"][0]
        assert explorer_call["run_type"] == "scheduled"
        # Feedback is empty string since no prior rejections
        assert explorer_call["feedback"] == ""

    def test_thinker_invoked_for_each_slice(self):
        """Verify Thinker was invoked once per slice (2 slices).

        **Validates: Requirement 1.2**
        """
        _, mocks = self._run_pipeline()

        assert len(mocks["thinker_calls"]) == 2
        assert mocks["thinker_calls"][0]["slice_name"] == "Cross-project DB patterns"
        assert mocks["thinker_calls"][1]["slice_name"] == "Stale synthesis check"

    def test_evaluators_invoked_for_each_candidate(self):
        """Verify all 4 evaluators were invoked for each of the 2 candidates (8 total).

        **Validates: Requirement 1.3, 3.1**
        """
        _, mocks = self._run_pipeline()

        assert len(mocks["evaluator_calls"]) == 8

        # Candidate 1 evaluated by all 4 roles
        c1_roles = {c["role"] for c in mocks["evaluator_calls"]
                     if c["candidate_title"] == "Implicit DB Migration Convention"}
        assert c1_roles == {"skeptic", "advocate", "epistemologist", "methodologist"}

        # Candidate 2 evaluated by all 4 roles
        c2_roles = {c["role"] for c in mocks["evaluator_calls"]
                     if c["candidate_title"] == "Outdated Caching Strategy"}
        assert c2_roles == {"skeptic", "advocate", "epistemologist", "methodologist"}

    def test_consensus_tallied_correctly(self):
        """Verify candidates stored with correct final verdicts.

        Candidate 1: 4/4 ACCEPT → ACCEPTED
        Candidate 2: 2/4 ACCEPT → REJECTED (binary BFT: ≤2/4 = REJECTED)

        **Validates: Requirements 1.4, 2.1, 2.2**
        """
        _, mocks = self._run_pipeline()

        store_calls = mocks["dc_db"].store_candidate.call_args_list
        assert len(store_calls) == 2

        # Extract final verdicts
        verdicts_stored = [c[0][3] for c in store_calls]
        assert "ACCEPTED" in verdicts_stored
        assert "REJECTED" in verdicts_stored

    def test_deduplication_checked_for_accepted(self):
        """Verify deduplication (embedding similarity search) was performed for the accepted candidate.

        **Validates: Requirement 7.1**
        """
        _, mocks = self._run_pipeline()

        # search_similar is called by check_duplicate for accepted candidates
        assert mocks["search"].call_count >= 1

    def test_memory_created_for_accepted(self):
        """Verify a memory was created for the accepted candidate.

        **Validates: Requirement 8.1**
        """
        _, mocks = self._run_pipeline()

        mocks["create_mem"].assert_called_once()
        call_kwargs = mocks["create_mem"].call_args[1]

        assert call_kwargs["title"] == "Implicit DB Migration Convention"
        assert "dream-cycle" in call_kwargs["tags"]
        assert call_kwargs["metadata"]["dream_cycle"] is True
        assert call_kwargs["metadata"]["strategy"] == "cross_project_collision"

    def test_relationships_created_for_accepted(self):
        """Verify proposed relationships were created for the accepted candidate.

        **Validates: Requirement 8.1**
        """
        _, mocks = self._run_pipeline()

        # Candidate 1 has 1 relationship
        assert mocks["create_rel"].call_count == 1
        rel_call = mocks["create_rel"].call_args
        assert rel_call[0][0] == "new-memory-001"  # source = new memory
        assert rel_call[0][1] == "mem-a1"  # target
        assert rel_call[0][2] == "supports"  # relation_type

    def test_candidates_stored_with_evaluator_verdicts(self):
        """Verify each candidate is stored with all 4 evaluator verdicts.

        **Validates: Requirements 1.6, 2.5, 3.1**
        """
        _, mocks = self._run_pipeline()

        store_calls = mocks["dc_db"].store_candidate.call_args_list
        assert len(store_calls) == 2

        for c in store_calls:
            verdicts_dict = c[0][2]  # third positional arg
            assert "evaluator_a_verdict" in verdicts_dict
            assert "evaluator_a_reasoning" in verdicts_dict
            assert "evaluator_b_verdict" in verdicts_dict
            assert "evaluator_b_reasoning" in verdicts_dict
            assert "evaluator_c_verdict" in verdicts_dict
            assert "evaluator_c_reasoning" in verdicts_dict
            assert "evaluator_d_verdict" in verdicts_dict
            assert "evaluator_d_reasoning" in verdicts_dict

    def test_accepted_candidate_stored_with_memory_id(self):
        """Verify the ACCEPTED candidate's store_candidate call includes the created memory ID.

        **Validates: Requirements 1.6, 8.1**
        """
        _, mocks = self._run_pipeline()

        store_calls = mocks["dc_db"].store_candidate.call_args_list
        accepted_call = [c for c in store_calls if c[0][3] == "ACCEPTED"]
        assert len(accepted_call) == 1
        assert accepted_call[0][0][4] == "new-memory-001"

    def test_rejected_candidate_stored_without_memory_id(self):
        """Verify the REJECTED candidate's store_candidate call has no memory ID.

        **Validates: Requirement 1.6, 6.5**
        """
        _, mocks = self._run_pipeline()

        store_calls = mocks["dc_db"].store_candidate.call_args_list
        rejected_calls = [c for c in store_calls if c[0][3] == "REJECTED"]
        assert len(rejected_calls) == 1
        # REJECTED candidates have 4 positional args (no memory_id)
        assert len(rejected_calls[0][0]) == 4

    def test_digest_generated(self):
        """Verify digest generation produced a file path in the run completion.

        **Validates: Requirement 12.1**
        """
        result, mocks = self._run_pipeline()

        complete_kwargs = mocks["dc_db"].complete_run.call_args[1]
        digest = complete_kwargs["digest"]
        assert digest is not None
        assert isinstance(digest, str)
        assert "dream-cycle-digest" in digest

    def test_run_type_propagated(self):
        """Verify the run_type is correctly propagated to the result.

        **Validates: Requirement 1.1**
        """
        result, _ = self._run_pipeline()
        assert result.run_type == "scheduled"

    def test_evaluator_verdicts_match_expected(self):
        """Verify the specific evaluator verdicts stored match our deterministic setup.

        Candidate 1: skeptic=ACCEPT, advocate=ACCEPT, epistemologist=ACCEPT, methodologist=ACCEPT
        Candidate 2: skeptic=ACCEPT, advocate=ACCEPT, epistemologist=REJECT, methodologist=REJECT

        **Validates: Requirements 1.4, 2.1, 2.2, 3.1**
        """
        _, mocks = self._run_pipeline()

        store_calls = mocks["dc_db"].store_candidate.call_args_list

        # ACCEPTED candidate: all 4 ACCEPT
        accepted_call = [c for c in store_calls if c[0][3] == "ACCEPTED"][0]
        av = accepted_call[0][2]
        assert av["evaluator_a_verdict"] == "ACCEPT"
        assert av["evaluator_b_verdict"] == "ACCEPT"
        assert av["evaluator_c_verdict"] == "ACCEPT"
        assert av["evaluator_d_verdict"] == "ACCEPT"

        # REJECTED candidate: 2 ACCEPT + 2 REJECT
        rejected_call = [c for c in store_calls if c[0][3] == "REJECTED"][0]
        rv = rejected_call[0][2]
        verdict_values = [
            rv["evaluator_a_verdict"], rv["evaluator_b_verdict"],
            rv["evaluator_c_verdict"], rv["evaluator_d_verdict"],
        ]
        assert verdict_values.count("ACCEPT") == 2
        assert verdict_values.count("REJECT") == 2
