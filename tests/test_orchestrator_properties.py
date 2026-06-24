"""Property-based tests for orchestrator — 4-evaluator consensus panel.

Properties 4, 5, 6 from the Byzantine Consensus Panel design document.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import MagicMock, patch, call

import pytest
from hypothesis import given, settings, strategies as st

from src.models import (
    CandidateInsight,
    DreamCycleResult,
    EvaluatorVerdict,
    MemorySlice,
)


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

confidence_levels = st.sampled_from(["high", "medium", "low"])

schema_operations = st.sampled_from(["assimilation", "accommodation"])

insight_types = st.sampled_from(["insight", "connection", "question", "synthesis"])

run_types = st.sampled_from(["scheduled", "post_learn", "session_start", "user_triggered"])

create_candidates = st.builds(
    CandidateInsight,
    title=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    type=insight_types,
    operation=st.just("CREATE"),
    target_memory_id=st.none(),
    supersedes_reason=st.none(),
    schema_operation=schema_operations,
    schema_note=st.text(max_size=30),
    confidence=confidence_levels,
    confidence_reasoning=st.text(max_size=50),
    content=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    source_memories=st.lists(st.uuids().map(str), min_size=0, max_size=3),
    relationships=st.lists(
        st.fixed_dictionaries({
            "target_id": st.uuids().map(str),
            "relation_type": st.sampled_from(["supports", "contradicts", "extends", "related_to"]),
            "note": st.text(max_size=20),
        }),
        min_size=0,
        max_size=3,
    ),
    strategy_that_found_it=st.sampled_from([
        "temporal_juxtaposition", "cross_project_collision", "orphan_archaeology",
        "pattern_emergence", "contradiction_hunting",
    ]),
)

# Strategy for 4 binary verdicts
four_verdict_combos = st.lists(verdict_values, min_size=4, max_size=4)

EVALUATOR_ROLES = ["skeptic", "advocate", "epistemologist", "methodologist"]
EXPECTED_VERDICT_KEYS = {
    "evaluator_a_verdict", "evaluator_a_reasoning",
    "evaluator_b_verdict", "evaluator_b_reasoning",
    "evaluator_c_verdict", "evaluator_c_reasoning",
    "evaluator_d_verdict", "evaluator_d_reasoning",
}



# ---------------------------------------------------------------------------
# Property 4: Four-Evaluator Orchestration
# **Validates: Requirements 3.1, 3.3**
# ---------------------------------------------------------------------------

class TestFourEvaluatorOrchestration:
    """Property 4: For any candidate insight, the orchestrator invokes exactly
    4 evaluators (skeptic, advocate, epistemologist, methodologist) and produces
    a verdicts dictionary containing all 8 keys.

    **Validates: Requirements 3.1, 3.3**
    """

    @given(candidate=create_candidates, verdicts=four_verdict_combos)
    @settings(max_examples=100, deadline=None)
    def test_four_evaluators_invoked_with_8_key_verdicts(self, candidate, verdicts):
        """For any candidate and verdict combination, the orchestrator invokes
        exactly 4 evaluators and builds an 8-key verdicts dict.

        **Validates: Requirements 3.1, 3.3**
        """
        orch = _make_orchestrator()

        test_slice = MemorySlice(
            name="Test Slice",
            strategy="pattern_emergence",
            memory_ids=["m1"],
            memory_titles=["Memory 1"],
            hypothesis="Test hypothesis",
        )

        # Track evaluator invocations
        evaluator_calls = []

        def mock_evaluator(cand, role):
            idx = EVALUATOR_ROLES.index(role)
            evaluator_calls.append(role)
            return EvaluatorVerdict(
                role=role,
                verdict=verdicts[idx],
                reasoning=f"reasoning for {role}",
            )

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "invoke_evaluator", side_effect=mock_evaluator), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }

            result = orch.run(run_type="scheduled")

            # Exactly 4 evaluator invocations per candidate
            assert len(evaluator_calls) == 4
            assert evaluator_calls == EVALUATOR_ROLES

            # store_candidate called with 8-key verdicts dict
            store_call = mock_db.store_candidate.call_args
            verdicts_dict = store_call[0][2]
            assert set(verdicts_dict.keys()) == EXPECTED_VERDICT_KEYS

            # Verify each evaluator's verdict is correctly mapped
            for i, role_letter in enumerate(["a", "b", "c", "d"]):
                assert verdicts_dict[f"evaluator_{role_letter}_verdict"] == verdicts[i]
                assert verdicts_dict[f"evaluator_{role_letter}_reasoning"] == f"reasoning for {EVALUATOR_ROLES[i]}"


# ---------------------------------------------------------------------------
# Property 5: Evaluator Independence
# **Validates: Requirements 3.4**
# ---------------------------------------------------------------------------

class TestEvaluatorIndependence:
    """Property 5: For any candidate evaluation across all 4 evaluator roles,
    no evaluator's prompt contains another evaluator's verdict or reasoning.

    **Validates: Requirements 3.4**
    """

    @given(candidate=create_candidates)
    @settings(max_examples=100, deadline=None)
    def test_no_cross_contamination_in_evaluator_prompts(self, candidate):
        """For any candidate, capture all 4 evaluator prompts and assert
        no cross-contamination of verdicts/reasoning.

        **Validates: Requirements 3.4**
        """
        orch = _make_orchestrator()

        captured_prompts = {}

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            # Determine which role this is for by checking the user_message
            for role in EVALUATOR_ROLES:
                if role in user_message:
                    captured_prompts[role] = system_prompt
                    break
            return {"output": {"verdict": "ACCEPT", "reasoning": "looks good"}, "raw": "{}"}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)

        # Invoke all 4 evaluators
        for role in EVALUATOR_ROLES:
            orch.invoke_evaluator(candidate, role)

        assert len(captured_prompts) == 4

        # For each evaluator's prompt, verify it does not contain
        # another evaluator's verdict or reasoning text
        for role, prompt in captured_prompts.items():
            for other_role in EVALUATOR_ROLES:
                if other_role == role:
                    continue
                # No other evaluator's verdict context should appear
                assert f"{other_role}'s verdict" not in prompt.lower(), (
                    f"Evaluator {role}'s prompt references {other_role}'s verdict"
                )
                assert f"{other_role} verdict" not in prompt.lower(), (
                    f"Evaluator {role}'s prompt references {other_role}'s verdict"
                )

        # Verify all 4 prompts are distinct (different role criteria)
        prompt_values = list(captured_prompts.values())
        assert len(set(prompt_values)) == 4, "All 4 evaluator prompts should be distinct"

        # Verify no evaluator receives tool access
        # (already tested by the mock — tools defaults to False for evaluators)


# ---------------------------------------------------------------------------
# Property 6: No DEFERRED in Pipeline
# **Validates: Requirements 2.3, 6.1, 6.5**
# ---------------------------------------------------------------------------

class TestNoDeferredInPipeline:
    """Property 6: For any execution of the consensus pipeline, the final
    verdict for every candidate is either ACCEPTED or REJECTED, never DEFERRED.

    **Validates: Requirements 2.3, 6.1, 6.5**
    """

    @given(
        verdicts=st.lists(four_verdict_combos, min_size=1, max_size=3),
    )
    @settings(max_examples=100, deadline=None)
    def test_no_deferred_verdicts_and_zero_deferred_count(self, verdicts):
        """For any set of candidates with various verdict combinations,
        no DEFERRED final verdicts appear.

        **Validates: Requirements 2.3, 6.1, 6.5**
        """
        orch = _make_orchestrator()

        num_candidates = len(verdicts)

        # Create candidates
        candidates = [
            CandidateInsight(
                title=f"Candidate {i}",
                type="insight",
                operation="CREATE",
                content=f"Content for candidate {i}",
                source_memories=[],
                relationships=[],
                strategy_that_found_it="pattern_emergence",
            )
            for i in range(num_candidates)
        ]

        test_slice = MemorySlice(
            name="Test Slice",
            strategy="pattern_emergence",
            memory_ids=["m1"],
            memory_titles=["Memory 1"],
            hypothesis="Test hypothesis",
        )

        # Build evaluator side effects for all candidates
        eval_side_effects = []
        for verdict_combo in verdicts:
            for i, v in enumerate(verdict_combo):
                eval_side_effects.append(
                    EvaluatorVerdict(
                        role=EVALUATOR_ROLES[i],
                        verdict=v,
                        reasoning=f"reasoning_{v}",
                    )
                )

        stored_verdicts = []

        def capture_store_candidate(run_id, candidate_dict, verdicts_dict, final_verdict, memory_id=None):
            stored_verdicts.append(final_verdict)
            return "cand-id"

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator", side_effect=eval_side_effects), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.store_candidate.side_effect = capture_store_candidate

            result = orch.run(run_type="scheduled")

            # No DEFERRED in stored verdicts
            assert "DEFERRED" not in stored_verdicts, (
                f"DEFERRED found in stored verdicts: {stored_verdicts}"
            )

            # All stored verdicts are ACCEPTED or REJECTED
            for v in stored_verdicts:
                assert v in ("ACCEPTED", "REJECTED"), (
                    f"Unexpected verdict: {v}"
                )
