"""Property-based tests for orchestrator — 4-evaluator consensus panel.

Four-seat invocation and evaluator input independence. Binary mapping is tested
by test_consensus; persisted multi-stage results by test_evaluation_trial and
test_dream_cycle_db. The former no-deferred test accepted empty aborted runs.
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


@pytest.fixture(autouse=True)
def _coordination_only_authorization(monkeypatch):
    """This suite isolates pipeline coordination with placeholder memory ids
    ("m1", "uuid-1") and no slice snapshots. Pre-spend authorization (R3 fix 4)
    is pinned with real UUIDs on the disposable database in
    tests/test_discovery_integrity.py and tests/test_integration.py."""
    monkeypatch.setattr("src.dream_cycle.orchestrator.authorize_candidate", lambda candidate, context: set())

def _make_orchestrator():
    """Instantiate the controlled profile without invoking a provider."""
    from src.dream_cycle import DreamCycleOrchestrator
    orchestrator = DreamCycleOrchestrator()
    # These properties isolate panel coordination. Real attention/provenance and
    # complete persistence are exercised by the native trial and ledger suites.
    orchestrator._allocate_attention = lambda run_id, run_type, started_at, slices: slices
    return orchestrator


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
    # Whitespace-only content is rejected before the panel (grounding path), so the
    # "four evaluators per candidate" property holds only for supportable content.
    content=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z")))
    .filter(lambda s: s.strip()),
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
    "evaluator_details",  # graded detail per seat (migration 013)
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
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator", side_effect=mock_evaluator), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id") as accepted_store:

            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }

            result = orch.run(run_type="scheduled")
            assert result.aborted_early is False
            assert result.candidates_generated == 1
            assert result.candidates_accepted + result.candidates_rejected == 1
            assert mock_db.store_candidate.call_count + accepted_store.call_count == 1

            # Exactly 4 evaluator invocations per candidate
            assert len(evaluator_calls) == 4
            assert evaluator_calls == EVALUATOR_ROLES

            # Accepted storage owns the atomic memory+ledger transaction;
            # rejection goes directly to the candidate ledger. Both receive the
            # exact same four-seat projection and one persistence request.
            if result.candidates_accepted:
                verdicts_dict = accepted_store.call_args.kwargs["verdicts"]
                assert accepted_store.call_args.kwargs["run_id"] == "run-id"
            else:
                verdicts_dict = mock_db.store_candidate.call_args.args[2]
                assert mock_db.store_candidate.call_args.args[3] == "REJECTED"
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
        captured_audits = []
        audit_marker = "SYNTHETIC_PRIOR_AUDIT_DO_NOT_LEAK"
        candidate.revision = {
            "original_title": "Before repair",
            "audit_score": 12,
            "blocking_issues": [audit_marker],
            "advisory_issues": ["SYNTHETIC_PRIOR_ADVISORY"],
        }

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            assert tools is False
            if kwargs.get("stage") == "grounding_audit":
                captured_audits.append(system_prompt)
            # Determine which role this is for by checking the user_message
            else:
                for role in EVALUATOR_ROLES:
                    if role in user_message:
                        captured_prompts[role] = system_prompt
                        break
            return {"output": {"verdict": "ACCEPT", "reasoning": "looks good"}, "raw": "{}",
                    "usage": {"inputTokens": 0, "outputTokens": 0}, "usage_source": "real"}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)

        # Invoke all 4 evaluators
        for role in EVALUATOR_ROLES:
            orch.invoke_evaluator(candidate, role)

        assert len(captured_prompts) == 4

        # For each evaluator's prompt, verify it does not contain
        # another evaluator's verdict or reasoning text
        for role, prompt in captured_prompts.items():
            assert audit_marker not in prompt
            assert "SYNTHETIC_PRIOR_ADVISORY" not in prompt
            assert '"revision"' not in prompt
            assert '"audit_score"' not in prompt
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

        # The audit and stored candidate retain their context; projecting a vote
        # must neither mutate the source record nor erase its review history.
        orch.invoke_evaluator(candidate, "skeptic", stage="grounding_audit")
        assert len(captured_audits) == 1
        assert audit_marker in captured_audits[0]
        assert '"audit_score": 12' in captured_audits[0]
        assert candidate.revision["blocking_issues"] == [audit_marker]
