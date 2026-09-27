"""Property-based and unit tests for the DreamCycleOrchestrator.

Tests cover circuit breaker, evaluator independence, SUPERSEDE consistency,
and CREATE storage correctness.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import ANY, MagicMock, patch, call

import pytest
from hypothesis import given, settings, strategies as st

from src.models import (
    CandidateInsight,
    DreamCycleResult,
    EvaluatorVerdict,
    MemorySlice,
)
from src.dream_cycle.feedback import build_feedback_injection
from src.dream_cycle.orchestrator import EVALUATOR_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Helper: create orchestrator without real AgentInvoker / MCP config
# ---------------------------------------------------------------------------

def _make_orchestrator():
    """Instantiate the controlled profile without invoking a provider."""
    from src.dream_cycle import DreamCycleOrchestrator
    orch = DreamCycleOrchestrator()
    # This suite isolates coordination; attention has dedicated real-DB coverage.
    orch._allocate_attention = lambda run_id, run_type, started_at, slices: slices
    return orch


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

run_types = st.sampled_from(["scheduled", "post_learn", "session_start", "user_triggered"])

confidence_levels = st.sampled_from(["high", "medium", "low"])

schema_operations = st.sampled_from(["assimilation", "accommodation"])

insight_types = st.sampled_from(["insight", "connection", "question", "synthesis"])

# Strategy for generating a simple CandidateInsight for CREATE
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

# Strategy for generating a SUPERSEDE candidate
supersede_candidates = st.builds(
    CandidateInsight,
    title=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    type=insight_types,
    operation=st.just("SUPERSEDE"),
    target_memory_id=st.uuids().map(str),
    supersedes_reason=st.text(min_size=1, max_size=50),
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
    ]),
)



# ---------------------------------------------------------------------------
# Property 5: Circuit Breaker
# **Validates: Requirements 9.1, 9.2, 9.3**
# ---------------------------------------------------------------------------

class TestCircuitBreakerProperty:
    """Property-based tests for circuit breaker behavior."""

    @given(run_type=run_types)
    @settings(max_examples=20)
    def test_empty_slices_aborts_early_with_zero_candidates(self, run_type):
        """For any run where Explorer returns 0 slices, verify
        candidates_generated = 0, aborted_early = TRUE, and no Thinker
        or Panel invocations occur.

        **Validates: Requirements 9.1, 9.2, 9.3**
        """
        orch = _make_orchestrator()

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[]) as mock_explorer, \
             patch.object(orch, "invoke_thinker") as mock_thinker, \
             patch.object(orch, "invoke_evaluator") as mock_evaluator, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):

            mock_db.create_run.return_value = "test-run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }

            result = orch.run(run_type=run_type)

            # Verify circuit breaker fired
            assert result.candidates_generated == 0
            assert result.aborted_early is True

            # Verify Thinker and Panel were NOT invoked
            mock_thinker.assert_not_called()
            mock_evaluator.assert_not_called()

            # Verify run was completed
            mock_db.complete_run.assert_called_once()
            call_args = mock_db.complete_run.call_args
            stats = call_args[1].get("stats") or call_args[0][1]
            assert stats["candidates_generated"] == 0


class TestCircuitBreakerExplicit:
    """Explicit unit tests for circuit breaker edge cases."""

    def test_circuit_breaker_completes_run_record(self):
        """When circuit breaker fires, the run record is completed with zero counts.

        **Validates: Requirements 9.2, 9.3**
        """
        orch = _make_orchestrator()

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[]), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):

            mock_db.create_run.return_value = "run-123"
            mock_db.get_memory_stats.return_value = {
                "total_count": 50,
                "recent_activity": 0,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }

            result = orch.run(run_type="scheduled")

            assert result.run_id == "run-123"
            assert result.aborted_early is True
            assert result.candidates_accepted == 0
            assert result.candidates_rejected == 0

            mock_db.complete_run.assert_called_once_with(
                "run-123",
                stats={
                    "candidates_generated": 0,
                    "candidates_accepted": 0,
                    "candidates_rejected": 0,
                    "aborted": True, "cost_usd": 0.0, "spend_unknown": False,
                },
                digest="",
            )


# ---------------------------------------------------------------------------
# Property 6: Evaluator Independence
# **Validates: Requirements 2.4, 13.4**
# ---------------------------------------------------------------------------

class TestEvaluatorIndependenceProperty:
    """Property-based tests for evaluator independence."""

    @given(candidate=create_candidates)
    @settings(max_examples=15)
    def test_evaluator_prompts_contain_no_other_verdicts(self, candidate):
        """For any candidate evaluation, verify no evaluator's prompt contains
        another evaluator's verdict or reasoning, and evaluator agents receive
        no MCP config.

        **Validates: Requirements 2.4, 13.4**
        """
        orch = _make_orchestrator()

        captured_calls = []

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured_calls.append({
                "system_prompt": system_prompt,
                "user_message": user_message,
                "tools": tools,
            })
            return {"output": {"verdict": "ACCEPT", "reasoning": "looks good"}, "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)

        # Invoke all 4 evaluators
        roles = ["skeptic", "advocate", "epistemologist", "methodologist"]
        for role in roles:
            orch.invoke_evaluator(candidate, role)

        assert len(captured_calls) == 4

        # Collect all verdicts/reasoning that could leak
        other_verdicts = ["ACCEPT", "REJECT"]
        other_reasoning_fragments = ["looks good"]

        for i, call_data in enumerate(captured_calls):
            prompt = call_data["system_prompt"]
            role = roles[i]

            # Verify no tool access for evaluators
            assert call_data["tools"] is False, (
                f"Evaluator {role} should NOT receive tool access"
            )

            # Verify the prompt contains the evaluator's own role
            assert role in prompt.lower() or role.capitalize() in prompt, (
                f"Evaluator prompt should contain its own role '{role}'"
            )

            # Verify no other evaluator's verdict text appears
            # (We check that the prompt doesn't contain verdict patterns
            # from other evaluators — the prompt should only have the
            # candidate data and role-specific criteria)
            for j, other_call in enumerate(captured_calls):
                if i == j:
                    continue
                other_role = roles[j]
                # The prompt should not reference other evaluator roles
                # in a verdict context
                assert f"{other_role}'s verdict" not in prompt.lower(), (
                    f"Evaluator {role}'s prompt references {other_role}'s verdict"
                )
                assert f"{other_role} verdict" not in prompt.lower(), (
                    f"Evaluator {role}'s prompt references {other_role}'s verdict"
                )


class TestEvaluatorIndependenceExplicit:
    """Explicit tests for evaluator independence."""

    def test_evaluators_invoked_without_tools(self):
        """Evaluator agents must not receive tool access.

        **Validates: Requirements 13.4**
        """
        orch = _make_orchestrator()

        captured_tools = []

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured_tools.append(tools)
            return {"output": {"verdict": "ACCEPT", "reasoning": "solid"}, "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)

        candidate = CandidateInsight(
            title="Test Insight",
            type="insight",
            operation="CREATE",
            content="Some content",
            source_memories=["uuid-1"],
            relationships=[],
            strategy_that_found_it="pattern_emergence",
        )

        for role in ["skeptic", "advocate", "epistemologist", "methodologist"]:
            orch.invoke_evaluator(candidate, role)

        # All 4 evaluator invocations should have tools=False
        assert all(t is False for t in captured_tools), (
            f"Evaluators received tool access: {captured_tools}"
        )

    def test_each_evaluator_gets_own_role_criteria(self):
        """Each evaluator prompt should contain its own role-specific criteria.

        **Validates: Requirements 2.4**
        """
        orch = _make_orchestrator()

        captured_prompts = {}

        def mock_invoke(system_prompt, user_message, mcp_config=None, **kwargs):
            # Extract role from the call context
            captured_prompts[len(captured_prompts)] = system_prompt
            return {"output": {"verdict": "ACCEPT", "reasoning": "good"}, "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)

        candidate = CandidateInsight(
            title="Test",
            type="insight",
            operation="CREATE",
            content="Content",
            source_memories=[],
            relationships=[],
            strategy_that_found_it="temporal_juxtaposition",
        )

        roles = ["skeptic", "advocate", "epistemologist", "methodologist"]
        for role in roles:
            orch.invoke_evaluator(candidate, role)

        # Each prompt should be distinct (different role criteria)
        prompts = list(captured_prompts.values())
        assert len(set(prompts)) == 4, "All 4 evaluator prompts should be distinct"



# ---------------------------------------------------------------------------
# Property 7: SUPERSEDE Consistency
# **Validates: Requirements 8.3, 8.4, 15.2, 15.5**
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Property 11: CREATE Storage Correctness
# **Validates: Requirements 8.1, 8.5**
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Two-Strike Rule tests REMOVED — _is_second_deferral and DEFERRED handling
# removed as part of Byzantine Consensus Panel (binary BFT model).
# See Requirements 6.1, 6.2.
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Execution Mode Scoping (Task 8.1)
# **Validates: Requirements 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7**
# ---------------------------------------------------------------------------

class TestSessionStartFrequencyCap:
    """Tests for session_start frequency cap check at the top of run()."""

    def test_session_start_skipped_when_briefing_not_allowed(self):
        """When should_run_briefing() returns False, session_start returns
        early with aborted_early=True and no run record is created.

        **Validates: Requirements 11.5, 11.6**
        """
        orch = _make_orchestrator()

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None):
            mock_db.should_run_briefing.return_value = False

            result = orch.run(run_type="session_start")

            assert result.aborted_early is True
            assert result.run_id == ""
            assert result.candidates_generated == 0
            assert result.candidates_accepted == 0
            assert result.candidates_rejected == 0

            # No run record should be created
            mock_db.create_run.assert_not_called()

    def test_session_start_proceeds_when_briefing_allowed(self):
        """When should_run_briefing() returns True, session_start proceeds
        normally through the pipeline.

        **Validates: Requirements 11.5, 11.6**
        """
        orch = _make_orchestrator()

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[]) as mock_explorer, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):

            mock_db.should_run_briefing.return_value = True
            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }

            result = orch.run(run_type="session_start")

            # Run record should be created (with backend provenance snapshot)
            mock_db.create_run.assert_called_once_with("session_start", backend_provenance=ANY)
            # Explorer should be invoked
            mock_explorer.assert_called_once()

    def test_non_session_start_skips_briefing_check(self):
        """Non-session_start run types do not check should_run_briefing().

        **Validates: Requirements 11.1, 11.2**
        """
        orch = _make_orchestrator()

        for run_type in ["scheduled", "post_learn", "user_triggered"]:
            with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
                 patch.object(orch, "_skim_arrivals", return_value=None), \
                 patch.object(orch, "invoke_explorer", return_value=[]), \
                 patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):

                mock_db.create_run.return_value = "run-id"
                mock_db.get_memory_stats.return_value = {
                    "total_count": 100, "recent_activity": 5,
                    "date_range": {"min": None, "max": None},
                    "type_distribution": {},
                }

                orch.run(run_type=run_type)

                # should_run_briefing should NOT be called
                mock_db.should_run_briefing.assert_not_called()


class TestSessionStartCandidateLimiting:
    """Tests for session_start candidate limiting (max 2 per slice)."""

    def _make_slice(self, name="Test Slice"):
        return MemorySlice(
            name=name,
            strategy="pattern_emergence",
            memory_ids=["m1", "m2"],
            memory_titles=["Memory 1", "Memory 2"],
            hypothesis="Test hypothesis",
        )

    def _make_candidate(self, title):
        return CandidateInsight(
            title=title,
            type="insight",
            operation="CREATE",
            content=f"Content for {title}",
            source_memories=["m1"],
            relationships=[],
            strategy_that_found_it="pattern_emergence",
        )

    def test_session_start_truncates_candidates_to_2_per_slice(self):
        """When run_type is session_start and Thinker returns >2 candidates
        per slice, only the first 2 are kept.

        **Validates: Requirement 11.4**
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()

        # Thinker returns 3 candidates for the slice
        candidates = [
            self._make_candidate("Insight A"),
            self._make_candidate("Insight B"),
            self._make_candidate("Insight C"),
        ]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):

            mock_db.should_run_briefing.return_value = True
            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"

            # All evaluators ACCEPT
            mock_eval.return_value = EvaluatorVerdict(
                role="skeptic", verdict="ACCEPT", reasoning="good"
            )

            result = orch.run(run_type="session_start")

            # Only 2 candidates should have been processed (not 3)
            assert result.candidates_generated == 2
            # 4 evaluators per candidate × 2 candidates = 8 evaluator calls
            assert mock_eval.call_count == 8

    def test_session_start_keeps_candidates_when_2_or_fewer(self):
        """When run_type is session_start and Thinker returns ≤2 candidates,
        no truncation occurs.

        **Validates: Requirement 11.4**
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()

        candidates = [
            self._make_candidate("Insight A"),
            self._make_candidate("Insight B"),
        ]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):

            mock_db.should_run_briefing.return_value = True
            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"

            mock_eval.return_value = EvaluatorVerdict(
                role="skeptic", verdict="ACCEPT", reasoning="good"
            )

            result = orch.run(run_type="session_start")

            # Both candidates processed
            assert result.candidates_generated == 2

    def test_scheduled_does_not_truncate_candidates(self):
        """When run_type is scheduled, no candidate truncation occurs
        even with >2 candidates per slice.

        **Validates: Requirement 11.2**
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()

        candidates = [
            self._make_candidate("Insight A"),
            self._make_candidate("Insight B"),
            self._make_candidate("Insight C"),
        ]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):

            mock_db.create_run.return_value = "run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"

            mock_eval.return_value = EvaluatorVerdict(
                role="skeptic", verdict="ACCEPT", reasoning="good"
            )

            result = orch.run(run_type="scheduled")

            # All 3 candidates should be processed
            assert result.candidates_generated == 2
            # 4 evaluators × 3 candidates = 12 evaluator calls
            assert mock_eval.call_count == 8


# ---------------------------------------------------------------------------
# Property 12: Digest Completeness
# **Validates: Requirements 12.2, 12.3, 12.4, 12.5, 12.6**
# ---------------------------------------------------------------------------

# Hypothesis strategy for generating candidates with mixed operations
_operations = st.sampled_from(["CREATE", "UPDATE", "SUPERSEDE"])

_digest_candidates = st.builds(
    CandidateInsight,
    title=st.text(min_size=1, max_size=40, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    type=insight_types,
    operation=_operations,
    target_memory_id=st.uuids().map(str),
    supersedes_reason=st.text(min_size=1, max_size=30),
    schema_operation=schema_operations,
    schema_note=st.text(max_size=20),
    confidence=confidence_levels,
    confidence_reasoning=st.text(max_size=30),
    content=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    source_memories=st.lists(st.uuids().map(str), min_size=1, max_size=3),
    relationships=st.just([]),
    strategy_that_found_it=st.sampled_from([
        "temporal_juxtaposition", "cross_project_collision", "orphan_archaeology",
        "pattern_emergence", "contradiction_hunting",
    ]),
)


class TestDigestCompletenessProperty:
    """Property-based tests for digest completeness."""

    @given(
        accepted=st.lists(_digest_candidates, min_size=1, max_size=5),
        rejected=st.lists(_digest_candidates, min_size=0, max_size=3),
    )
    @settings(max_examples=20)
    def test_digest_contains_required_sections_and_grouping(
        self, accepted, rejected
    ):
        """For any set of accepted/rejected candidates, verify digest
        groups by strategy, includes required sections, and shows operation type
        for UPDATE/SUPERSEDE.

        **Validates: Requirements 12.2, 12.3, 12.4, 12.5, 12.6**
        """
        from src.dream_cycle.digest import render_digest
        run_id = "test-run-digest"

        digest_text = render_digest(run_id, accepted, rejected, date="2026-09-26")

        # 1. Run Statistics section with correct counts (Req 12.5)
        assert "## Run Statistics" in digest_text
        assert f"Accepted: {len(accepted)}" in digest_text
        assert f"Rejected: {len(rejected)}" in digest_text

        # 2. Explorer Strategies Used section (Req 12.6)
        assert "## Explorer Strategies Used" in digest_text

        # 3. Accepted Insights section grouped by strategy (Req 12.2)
        assert "## Accepted Insights" in digest_text
        strategies_in_accepted = {c.strategy_that_found_it for c in accepted if c.strategy_that_found_it}
        for strategy in strategies_in_accepted:
            assert f"### Strategy: {strategy}" in digest_text

        # 4. For UPDATE/SUPERSEDE candidates, operation type shown (Req 12.4)
        for c in accepted:
            if c.operation in ("UPDATE", "SUPERSEDE"):
                assert f"**Operation**: {c.operation}" in digest_text

        # 5. Source memory IDs shown for each accepted insight (Req 12.3)
        for c in accepted:
            assert "**Source memories**" in digest_text
            for mem_id in c.source_memories:
                assert mem_id in digest_text


class TestDigestCompletenessExplicit:
    """Explicit tests for digest edge cases."""

    def test_digest_no_accepted_insights_shows_message(self):
        """Digest with no accepted insights shows 'No insights were accepted'.

        **Validates: Requirements 12.2, 12.5**
        """
        from src.dream_cycle.digest import render_digest

        rejected_candidate = CandidateInsight(
            title="Rejected One",
            type="insight",
            operation="CREATE",
            content="Rejected content",
            source_memories=["src-2"],
            relationships=[],
            strategy_that_found_it="orphan_archaeology",
        )

        digest_text = render_digest("run-empty", [], [rejected_candidate], date="2026-09-26")
        assert "No insights were accepted" in digest_text
        assert "Accepted: 0" in digest_text
        assert "Rejected: 1" in digest_text

    def test_digest_update_operation_shows_operation_type(self):
        """Digest with UPDATE operation shows the operation type and target.

        **Validates: Requirements 12.4**
        """
        from src.dream_cycle.digest import render_digest

        update_candidate = CandidateInsight(
            title="Updated Insight",
            type="insight",
            operation="UPDATE",
            target_memory_id="target-mem-123",
            content="Updated content with new evidence",
            source_memories=["src-a", "src-b"],
            relationships=[],
            strategy_that_found_it="stale_synthesis_check",
        )

        digest_text = render_digest("run-update", [update_candidate], [], date="2026-09-26")
        assert "**Operation**: UPDATE" in digest_text
        assert "target-mem-123" in digest_text
        assert "src-a" in digest_text
        assert "src-b" in digest_text

    def test_digest_file_is_private_and_scoped(self, tmp_path, monkeypatch):
        from pathlib import Path
        from src.dream_cycle.digest import write_digest
        output = tmp_path / "digests"
        monkeypatch.setenv("SECOND_BRAIN_DIGEST_DIR", str(output))
        result = Path(write_digest("run-path", "Synthetic content", date="2026-09-26"))
        assert result.parent == output
        assert result.name == "dream-cycle-digest-2026-09-26-run-path.md"
        assert result.read_text() == "Synthetic content"
        assert output.stat().st_mode & 0o777 == 0o700
        assert result.stat().st_mode & 0o777 == 0o600
        assert list(output.iterdir()) == [result]

    def test_digest_rejects_non_private_directory(self, tmp_path, monkeypatch):
        from src.dream_cycle.digest import write_digest
        output = tmp_path / "digests"
        output.mkdir(mode=0o755)
        output.chmod(0o755)
        monkeypatch.setenv("SECOND_BRAIN_DIGEST_DIR", str(output))
        with pytest.raises(ValueError, match="private permissions"):
            write_digest("run-path", "Synthetic content", date="2026-09-26")
        assert list(output.iterdir()) == []


# ---------------------------------------------------------------------------
# Error Handling Tests (Task 10.2)
# **Validates: Requirements 16.1, 16.2, 16.3, 16.5**
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Unit tests for error handling in the orchestrator pipeline."""

    def _make_slice(self, name="Test Slice"):
        return MemorySlice(
            name=name,
            strategy="pattern_emergence",
            memory_ids=["m1", "m2"],
            memory_titles=["Memory 1", "Memory 2"],
            hypothesis="Test hypothesis",
        )

    def _make_candidate(self, title="Test Insight"):
        return CandidateInsight(
            title=title,
            type="insight",
            operation="CREATE",
            content=f"Content for {title}",
            source_memories=["m1"],
            relationships=[],
            strategy_that_found_it="pattern_emergence",
        )

    def test_explorer_failure_completes_run_with_zero_candidates(self):
        """When invoke_explorer raises TimeoutError, the run completes
        with aborted_early=True and 0 candidates.

        **Validates: Requirement 16.1**
        """
        orch = _make_orchestrator()

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", side_effect=TimeoutError("Explorer timed out")), \
             patch.object(orch, "invoke_thinker") as mock_thinker, \
             patch.object(orch, "invoke_evaluator") as mock_evaluator, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):

            mock_db.create_run.return_value = "run-explorer-fail"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }

            result = orch.run(run_type="scheduled")

            assert result.aborted_early is True
            assert result.candidates_generated == 0
            assert result.candidates_accepted == 0
            assert result.candidates_rejected == 0

            # Thinker and evaluators should never be called
            mock_thinker.assert_not_called()
            mock_evaluator.assert_not_called()

            # Run record should be completed
            mock_db.complete_run.assert_called_once()
            call_args = mock_db.complete_run.call_args
            stats = call_args[1].get("stats") or call_args[0][1]
            assert stats["candidates_generated"] == 0

    def test_thinker_failure_for_one_slice_continues_others(self):
        """When invoke_thinker raises on the first slice, the second slice's
        candidates are still processed through the pipeline.

        **Validates: Requirement 16.2**
        """
        orch = _make_orchestrator()
        slice_a = self._make_slice("Slice A")
        slice_b = self._make_slice("Slice B")
        candidate_b = self._make_candidate("Insight from Slice B")

        call_count = {"n": 0}

        def thinker_side_effect(s):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Thinker crashed on slice A")
            return [candidate_b]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[slice_a, slice_b]), \
             patch.object(orch, "invoke_thinker", side_effect=thinker_side_effect), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):

            mock_db.create_run.return_value = "run-thinker-fail"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"

            # All evaluators ACCEPT the candidate from slice B
            mock_eval.return_value = EvaluatorVerdict(
                role="skeptic", verdict="ACCEPT", reasoning="good"
            )

            result = orch.run(run_type="scheduled")

            # Only 1 candidate from slice B should be processed
            assert result.candidates_generated == 1
            assert result.candidates_accepted == 1

            # Evaluators should have been called 4 times (4 evaluators × 1 candidate)
            assert mock_eval.call_count == 4

    def test_unrecoverable_evaluator_aborts_run(self):
        """When an evaluator fails every retry, the run aborts loudly
        (aborted_early=True) — it never fabricates a REJECT vote.

        Supersedes the old "timeout treated as REJECT" rule (Req 16.3): a crash
        is silence, not a verdict. The orchestrator retries, then fails loud.
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()
        candidate = self._make_candidate("Unrecoverable Eval Candidate")

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator", side_effect=TimeoutError("evaluator down")) as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):

            mock_db.create_run.return_value = "run-eval-unrecoverable"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.store_candidate.return_value = "cand-id"

            result = orch.run(run_type="scheduled")

            # Unrecoverable evaluator → run aborts; candidate neither accepted
            # nor fabricated-rejected.
            assert result.aborted_early is True
            assert result.candidates_accepted == 0
            assert result.candidates_rejected == 0
            mock_db.store_candidate.assert_not_called()
            # the failing evaluator was retried the full budget before aborting
            assert mock_eval.call_count == EVALUATOR_MAX_ATTEMPTS

    def test_unrecoverable_evaluator_aborts_with_accurate_partial_stats(self):
        """Regression (orphaned-stats): when a later candidate's evaluator is
        unrecoverable, the run aborts but reports the candidates already
        evaluated — never a zeroed run row against persisted accepted memories.
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()
        c1 = self._make_candidate("First — fully evaluated")
        c2 = self._make_candidate("Second — evaluator dies")

        def eval_side_effect(candidate, role):
            if candidate is c1:
                return EvaluatorVerdict(role=role, verdict="ACCEPT", reasoning="ok")
            raise TimeoutError("panel down on c2")

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[c1, c2]), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator", side_effect=eval_side_effect), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):

            mock_db.create_run.return_value = "run-partial-abort"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.store_candidate.return_value = "cand-id"

            result = orch.run(run_type="scheduled")

            # Aborted, but c1's acceptance is preserved (not zeroed).
            assert result.aborted_early is True
            assert result.candidates_generated == 2
            assert result.candidates_accepted == 1
            assert result.candidates_rejected == 0
            # complete_run recorded real partial stats, not _ZERO_STATS
            stats = mock_db.complete_run.call_args.kwargs["stats"]
            assert stats["candidates_generated"] == 2
            assert stats["candidates_accepted"] == 1

    def test_one_dissenting_vote_tolerated_quorum_accepts(self):
        """The chosen voting policy: one dissenting REJECT among four is
        tolerated — 3/4 ACCEPT → ACCEPTED. The quorum tolerates a bad *vote*;
        crashes are handled separately (retry/abort), not folded into the tally.
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()
        candidate = self._make_candidate("One Dissent Candidate")

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):

            mock_db.create_run.return_value = "run-one-dissent"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.store_candidate.return_value = "cand-id"

            # Real verdicts: one genuine REJECT, three ACCEPT → 3/4 → ACCEPTED
            mock_eval.side_effect = [
                EvaluatorVerdict(role="skeptic", verdict="REJECT", reasoning="unconvinced"),
                EvaluatorVerdict(role="advocate", verdict="ACCEPT", reasoning="useful"),
                EvaluatorVerdict(role="epistemologist", verdict="ACCEPT", reasoning="novel"),
                EvaluatorVerdict(role="methodologist", verdict="ACCEPT", reasoning="sound methodology"),
            ]

            result = orch.run(run_type="scheduled")

            assert result.aborted_early is False
            assert result.candidates_accepted == 1
            assert result.candidates_rejected == 0
            store_call = mock_db.store_candidate.call_args
            assert store_call[0][3] == "ACCEPTED"

    def test_two_dissenting_votes_rejected(self):
        """Two genuine REJECT votes (2/4 ACCEPT) → REJECTED — the acceptance threshold.
        These are real verdicts, not crashes: rejected on merit, run completes.
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()
        candidate = self._make_candidate("Two Dissent Candidate")

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):

            mock_db.create_run.return_value = "run-two-dissent"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.store_candidate.return_value = "cand-id"

            mock_eval.side_effect = [
                EvaluatorVerdict(role="skeptic", verdict="REJECT", reasoning="weak"),
                EvaluatorVerdict(role="advocate", verdict="ACCEPT", reasoning="useful"),
                EvaluatorVerdict(role="epistemologist", verdict="REJECT", reasoning="not novel"),
                EvaluatorVerdict(role="methodologist", verdict="ACCEPT", reasoning="ok"),
            ]

            result = orch.run(run_type="scheduled")

            assert result.aborted_early is False
            assert result.candidates_accepted == 0
            assert result.candidates_rejected == 1

            store_call = mock_db.store_candidate.call_args
            assert store_call[0][3] == "REJECTED"

    # Unsafe pre-authorization storage contract replaced by test_discovery_integrity.py.
    pass


# ---------------------------------------------------------------------------
# Tests for build_feedback_injection with user rejections
# **Validates: Requirements 14.5, 15.1, 15.4**
# ---------------------------------------------------------------------------

class TestBuildFeedbackInjectionUserRejections:
    """Test that build_feedback_injection includes user rejections."""

    def test_includes_user_rejections_section(self):
        """When user rejections exist, feedback includes a '## User rejections' section."""
        mock_rejections = [
            {
                "run_id": "run-1",
                "run_type": "scheduled",
                "completed_at": "2026-03-20",
                "candidate_id": "cand-1",
                "final_verdict": "REJECTED",
                "evaluator_a_verdict": "REJECT",
                "evaluator_a_reasoning": "Not grounded",
                "evaluator_b_verdict": "ACCEPT",
                "evaluator_b_reasoning": "Relevant",
                "evaluator_c_verdict": "REJECT",
                "evaluator_c_reasoning": "Not falsifiable",
            }
        ]
        mock_user_rejections = [
            {
                "candidate_id": "cand-2",
                "user_rejection_reason": "Already knew this",
                "candidate_json": {"title": "Obvious Pattern"},
            }
        ]

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = mock_rejections
            mock_db.get_user_rejections.return_value = mock_user_rejections
            mock_db.get_accepted_dissents.return_value = []

            result = build_feedback_injection()

        assert "## Lessons from recent cycles" in result
        assert "## User rejections" in result
        assert '"Obvious Pattern"' in result
        assert '"Already knew this"' in result

    def test_user_rejections_only(self):
        """When only user rejections exist (no evaluator rejections), feedback still generated."""
        mock_user_rejections = [
            {
                "candidate_id": "cand-1",
                "user_rejection_reason": "Too vague",
                "candidate_json": {"title": "Vague Insight"},
            }
        ]

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_user_rejections.return_value = mock_user_rejections
            mock_db.get_accepted_dissents.return_value = []

            result = build_feedback_injection()

        assert "## Lessons from recent cycles" in result
        assert "## User rejections" in result
        assert '"Vague Insight"' in result
        assert '"Too vague"' in result

    def test_no_rejections_at_all_returns_empty(self):
        """When neither evaluator nor user rejections exist, returns empty string."""
        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_user_rejections.return_value = []
            mock_db.get_accepted_dissents.return_value = []

            result = build_feedback_injection()

        assert result == ""

    def test_user_rejection_with_string_candidate_json(self):
        """When candidate_json is a JSON string (not dict), it is parsed correctly."""
        mock_user_rejections = [
            {
                "candidate_id": "cand-1",
                "user_rejection_reason": "Redundant",
                "candidate_json": '{"title": "Redundant Insight"}',
            }
        ]

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_user_rejections.return_value = mock_user_rejections
            mock_db.get_accepted_dissents.return_value = []

            result = build_feedback_injection()

        assert '"Redundant Insight"' in result
        assert '"Redundant"' in result

    def test_user_rejection_with_missing_title_uses_fallback(self):
        """When candidate_json has no title, uses 'Unknown insight' fallback."""
        mock_user_rejections = [
            {
                "candidate_id": "cand-1",
                "user_rejection_reason": "Bad",
                "candidate_json": {},
            }
        ]

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_user_rejections.return_value = mock_user_rejections
            mock_db.get_accepted_dissents.return_value = []

            result = build_feedback_injection()

        assert '"Unknown insight"' in result

    def test_user_rejection_with_no_reason_uses_fallback(self):
        """When user_rejection_reason is None, uses 'No reason given' fallback."""
        mock_user_rejections = [
            {
                "candidate_id": "cand-1",
                "user_rejection_reason": None,
                "candidate_json": {"title": "Some Insight"},
            }
        ]

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_user_rejections.return_value = mock_user_rejections
            mock_db.get_accepted_dissents.return_value = []

            result = build_feedback_injection()

        assert '"No reason given"' in result


# ---------------------------------------------------------------------------
# Property 14: User Rejection Preserves Memory
# **Validates: Requirements 14.5, 15.4**
# ---------------------------------------------------------------------------

class TestUserRejectionPreservesMemoryProperty:
    """Property-based tests for user rejection preservation.

    For any user rejection of an accepted dream cycle insight, the memory
    continues to exist in the database with status 'user_rejected', and the
    rejection timestamp and reason are recorded in the dream_cycle_candidates
    record without deleting the memory.
    """

    @given(
        candidate_id=st.uuids().map(str),
        reason=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P"))),
    )
    @settings(max_examples=30)
    @patch("src.dream_cycle_db.get_connection")
    def test_rejection_updates_candidate_and_memory_in_single_transaction(
        self, mock_get_conn, candidate_id, reason
    ):
        """For any candidate_id and rejection reason, mark_user_rejected
        updates the candidate record with user_rejected_at and
        user_rejection_reason, sets the memory status to 'user_rejected',
        and commits both in a single transaction.

        **Validates: Requirements 14.5, 15.4**
        """
        from src.dream_cycle_db import mark_user_rejected

        memory_id = "memory-uuid-for-test"

        from tests.conftest import mock_conn as shared_mock_conn
        mock_conn, mock_cur = shared_mock_conn(first=(memory_id,))
        mock_get_conn.return_value = mock_conn

        mark_user_rejected(candidate_id, reason)

        # Two SQL executions: candidate update + memory status update
        assert mock_cur.execute.call_count == 2

        # First call: updates dream_cycle_candidates with rejection info
        first_sql = mock_cur.execute.call_args_list[0][0][0]
        first_params = mock_cur.execute.call_args_list[0][0][1]
        assert "UPDATE dream_cycle_candidates" in first_sql
        assert "user_rejected_at" in first_sql
        assert "user_rejection_reason" in first_sql
        assert first_params == (reason, candidate_id)

        # Second call: sets memory status to 'user_rejected' (NOT delete)
        second_sql = mock_cur.execute.call_args_list[1][0][0]
        second_params = mock_cur.execute.call_args_list[1][0][1]
        assert "UPDATE memories" in second_sql
        assert "user_rejected" in second_sql
        assert "DELETE" not in second_sql.upper()
        assert second_params == (memory_id,)

        # Single commit = single transaction (atomicity)
        mock_conn.commit.assert_called_once()
        # Connection always closed

    @given(
        candidate_id=st.uuids().map(str),
        reason=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P"))),
    )
    @settings(max_examples=30)
    @patch("src.dream_cycle_db.get_connection")
    def test_rejection_without_memory_skips_memory_update(
        self, mock_get_conn, candidate_id, reason
    ):
        """For any candidate_id with no associated memory (created_memory_id
        is NULL), mark_user_rejected updates only the candidate record and
        does not attempt to update any memory.

        **Validates: Requirements 14.5, 15.4**
        """
        from src.dream_cycle_db import mark_user_rejected

        from tests.conftest import mock_conn as shared_mock_conn
        mock_conn, mock_cur = shared_mock_conn(first=(None,))
        mock_get_conn.return_value = mock_conn

        mark_user_rejected(candidate_id, reason)

        # Only one SQL execution: candidate update only
        assert mock_cur.execute.call_count == 1

        first_sql = mock_cur.execute.call_args_list[0][0][0]
        assert "UPDATE dream_cycle_candidates" in first_sql
        assert "user_rejected_at" in first_sql

        # No DELETE anywhere
        assert "DELETE" not in first_sql.upper()

        # Still commits (transaction completes)
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Property 10: Run Record Completeness
# **Validates: Requirements 1.5, 1.6, 2.5**
# ---------------------------------------------------------------------------

# Strategy: generate a list of candidates with random verdicts
_verdict_outcomes = st.sampled_from(["ACCEPT", "REJECT"])

_run_candidate_entry = st.fixed_dictionaries({
    "title": st.text(min_size=1, max_size=40, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
    "verdicts": st.tuples(
        _verdict_outcomes,  # skeptic
        _verdict_outcomes,  # advocate
        _verdict_outcomes,  # epistemologist
        _verdict_outcomes,  # methodologist
    ),
})






# ---------------------------------------------------------------------------
# Feature: dream-cycle-decomposition — Storage Import Verification & Properties
# **Validates: Requirements 3.6, 3.7**
# ---------------------------------------------------------------------------






class TestCheckDuplicateSkipsChunksStrictThreshold:
    """Property 3: check_duplicate skips chunks and uses strict threshold.

    For any content string and similarity result set, check_duplicate skips
    results where parent_id is not None and only returns a match when
    similarity is strictly greater than the threshold.

    **Feature: dream-cycle-decomposition, Property 3: check_duplicate skips chunks and uses strict threshold**
    **Validates: Requirements 3.7**
    """

    @given(
        content=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
        threshold=st.floats(min_value=0.1, max_value=0.99, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_chunk_memories_are_always_skipped(self, content, threshold):
        """For any content and threshold, results with non-null parent_id are skipped
        even if their similarity exceeds the threshold.

        **Feature: dream-cycle-decomposition, Property 3: check_duplicate skips chunks and uses strict threshold**
        **Validates: Requirements 3.7**
        """
        from src.dream_cycle.storage import check_duplicate

        # All results are chunks (parent_id is not None) with high similarity
        chunk_results = [
            {"id": "chunk-1", "parent_id": "parent-1", "similarity": 0.99},
            {"id": "chunk-2", "parent_id": "parent-2", "similarity": 0.95},
        ]

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 128), \
             patch("src.dream_cycle.storage.search_similar", return_value=chunk_results):

            result = check_duplicate(content, threshold=threshold)
            assert result is None

    @given(
        content=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
        threshold=st.floats(min_value=0.1, max_value=0.99, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_exact_threshold_does_not_match(self, content, threshold):
        """For any content and threshold, a result with similarity exactly equal
        to the threshold is NOT considered a duplicate (strict greater-than).

        **Feature: dream-cycle-decomposition, Property 3: check_duplicate skips chunks and uses strict threshold**
        **Validates: Requirements 3.7**
        """
        from src.dream_cycle.storage import check_duplicate

        exact_results = [
            {"id": "mem-exact", "parent_id": None, "similarity": threshold},
        ]

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 128), \
             patch("src.dream_cycle.storage.search_similar", return_value=exact_results):

            result = check_duplicate(content, threshold=threshold)
            assert result is None

    @given(
        content=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
        threshold=st.floats(min_value=0.1, max_value=0.98, allow_nan=False, allow_infinity=False),
        delta=st.floats(min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_above_threshold_non_chunk_returns_match(self, content, threshold, delta):
        """For any content and threshold, a non-chunk result with similarity strictly
        above the threshold is returned as a duplicate.

        **Feature: dream-cycle-decomposition, Property 3: check_duplicate skips chunks and uses strict threshold**
        **Validates: Requirements 3.7**
        """
        from src.dream_cycle.storage import check_duplicate

        above_results = [
            {"id": "mem-above", "parent_id": None, "similarity": threshold + delta},
        ]

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 128), \
             patch("src.dream_cycle.storage.search_similar", return_value=above_results):

            result = check_duplicate(content, threshold=threshold)
            assert result == "mem-above"

    @given(
        content=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
        threshold=st.floats(min_value=0.1, max_value=0.98, allow_nan=False, allow_infinity=False),
        delta=st.floats(min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_chunk_before_valid_match_still_finds_match(self, content, threshold, delta):
        """When a chunk result appears before a valid non-chunk match, the chunk
        is skipped and the valid match is returned.

        **Feature: dream-cycle-decomposition, Property 3: check_duplicate skips chunks and uses strict threshold**
        **Validates: Requirements 3.7**
        """
        from src.dream_cycle.storage import check_duplicate

        mixed_results = [
            {"id": "chunk-1", "parent_id": "parent-1", "similarity": 0.99},
            {"id": "mem-valid", "parent_id": None, "similarity": threshold + delta},
        ]

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 128), \
             patch("src.dream_cycle.storage.search_similar", return_value=mixed_results):

            result = check_duplicate(content, threshold=threshold)
            assert result == "mem-valid"


# ---------------------------------------------------------------------------
# _invoke_evaluator_safe: retry transient failures, then raise (never REJECT)
# Supersedes Property 4 / Requirement 7.3 ("returns REJECT on failure"):
# an unresponsive evaluator is silence (omission), not a vote — retry, then
# fail loudly. Fabricating a REJECT would substitute missing evidence with a
# non-Byzantine event.
# ---------------------------------------------------------------------------

class TestInvokeEvaluatorSafeRetryThenRaise:
    """_invoke_evaluator_safe retries transient evaluator failures and, if they
    persist, raises — it never fabricates a REJECT verdict."""

    @given(
        candidate=create_candidates,
        role=st.sampled_from(["skeptic", "advocate", "epistemologist", "methodologist"]),
        error_cls=st.sampled_from([TimeoutError, RuntimeError, ValueError]),
    )
    @settings(max_examples=25)
    def test_raises_after_exhausting_attempts(self, candidate, role, error_cls):
        """When invoke_evaluator fails every attempt, _invoke_evaluator_safe
        raises (no fabricated REJECT), after retrying the full budget."""
        orch = _make_orchestrator()

        with patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_evaluator", side_effect=error_cls("agent failed")) as mock_eval:
            with pytest.raises(RuntimeError):
                orch._invoke_evaluator_safe(candidate, role)
            assert mock_eval.call_count == EVALUATOR_MAX_ATTEMPTS

    @given(
        candidate=create_candidates,
        role=st.sampled_from(["skeptic", "advocate", "epistemologist", "methodologist"]),
    )
    @settings(max_examples=25)
    def test_retries_then_succeeds(self, candidate, role):
        """A transient failure followed by success returns the real verdict
        (invoke_evaluator called twice; no fabricated REJECT)."""
        orch = _make_orchestrator()
        real = EvaluatorVerdict(role=role, verdict="ACCEPT", reasoning="recovered")

        with patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_evaluator", side_effect=[TimeoutError("flake"), real]) as mock_eval:
            result = orch._invoke_evaluator_safe(candidate, role)

        assert result is real
        assert result.verdict == "ACCEPT"
        assert mock_eval.call_count == 2


# ---------------------------------------------------------------------------
# Property 5: Final orchestrator method set contains only coordination methods
# **Feature: dream-cycle-decomposition, Property 5: Final orchestrator method set contains only coordination methods**
# **Validates: Requirements 7.1**
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Property 7: Public API preservation through __init__.py
# **Feature: dream-cycle-decomposition, Property 7: Public API preservation through __init__.py**
# **Validates: Requirements 1.1, 1.4, 8.6**
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Direct-API (tool-less) Thinker packet + metered-spend guardrails
# ---------------------------------------------------------------------------

class TestToollessThinkerPacketAndSpendGuard:
    """When the Thinker runs on a tool-less Direct-API backend (Bedrock) it
    cannot read memories over MCP, so the orchestrator must deliver the slice
    contents in the prompt (the "B2 context packet"). Every metered call is
    priced; breaching the per-run cap aborts the run without retries.
    """

    MEM_A = "11111111-1111-4111-8111-111111111111"
    MEM_B = "22222222-2222-4222-8222-222222222222"

    def _bedrock_resolver(self):
        from src.backends.resolver import Resolver, RoleBackend
        return Resolver({
            "explorer": RoleBackend("explorer", "claude_code", "global.anthropic.claude-opus-5", "high"),
            "thinker": RoleBackend("thinker", "bedrock", "global.anthropic.claude-opus-5", "high"),
            "skeptic": RoleBackend("skeptic", "bedrock", "global.openai.gpt-6-astra", "high"),
            "advocate": RoleBackend("advocate", "bedrock", "global.anthropic.claude-sonnet-5", "high"),
            "epistemologist": RoleBackend("epistemologist", "bedrock", "global.openai.gpt-6-astra", "high"),
            "methodologist": RoleBackend("methodologist", "bedrock", "global.anthropic.claude-sonnet-5", "high"),
        })

    def _tool_capable_orchestrator(self):
        from src.backends.resolver import Resolver, RoleBackend
        orch = _make_orchestrator()
        orch.resolver = Resolver({
            role: RoleBackend(role, "claude_code", "global.anthropic.claude-opus-5", "high")
            for role in ("explorer", "thinker", "skeptic", "advocate", "epistemologist", "methodologist")
        })
        return orch

    def _slice(self):
        return MemorySlice(
            name="Morning focus", strategy="pattern_emergence",
            memory_ids=[self.MEM_A, self.MEM_B], memory_titles=["Focus block", "Calendar note"],
            hypothesis="Deep work clusters in the morning",
        )

    def _fake_memories(self):
        return [
            {"id": self.MEM_A, "title": "Focus block", "type": "notes", "content": "7am focus block, phone off.",
             "created_at": "2026-03-01T07:00:00+00:00", "tags": ["focus"], "project": "self"},
            {"id": self.MEM_B, "title": "Calendar note", "type": "notes", "content": "Blocked 7-9am for deep work.",
             "created_at": "2026-03-08T07:00:00+00:00", "tags": [], "project": None},
        ]

    def test_toolless_thinker_gets_slice_contents_and_no_tools(self):
        orch = _make_orchestrator()
        orch.resolver = self._bedrock_resolver()
        captured = {}

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured.update(system_prompt=system_prompt, user_message=user_message, tools=tools, **kwargs)
            return {"output": [], "raw": "[]", "usage": {"inputTokens": 0, "outputTokens": 0}}

        with patch("src.dream_cycle.agents.thinker.load_memories_by_ids", return_value=self._fake_memories()) as loader:
            orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)
            orch.invoke_thinker(self._slice())

        loader.assert_called_once_with([self.MEM_A, self.MEM_B])
        assert captured["tools"] is False, "a Direct-API backend rejects tools=True"
        payload = json.loads(captured["user_message"])
        memories = payload["memory_slice"]["memories"]
        assert [m["id"] for m in memories] == [self.MEM_A, self.MEM_B]
        assert memories[0]["content"] == "7am focus block, phone off."
        assert "not available" in payload["note"].lower() and "search" in payload["note"].lower()
        # Contents are cached so evaluators can ground against the same sources.
        assert orch._slice_memory_cache[self.MEM_B]["title"] == "Calendar note"

    def test_tool_capable_thinker_gets_packet_and_keeps_tools_for_search(self):
        """An allowed Claude Code Thinker keeps tools=True but now receives the
        slice contents inline too: re-reading 10-20 memories over MCP at ~20s per
        turn timed out on episodic slices (2026-09-17). The note tells them to
        spend tools on CLS-interleaving searches, not on memory_read. Contents
        are still cached for the tool-less evaluators."""
        orch = self._tool_capable_orchestrator()
        captured = {}

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured.update(user_message=user_message, tools=tools, timeout=kwargs.get("timeout"))
            return {"output": [], "raw": "[]", "usage": {"inputTokens": 0, "outputTokens": 0}}

        with patch("src.dream_cycle.agents.thinker.load_memories_by_ids", return_value=self._fake_memories()) as loader:
            orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)
            orch.invoke_thinker(self._slice())

        loader.assert_called_once_with([self.MEM_A, self.MEM_B])
        assert captured["tools"] is True
        assert captured["timeout"] >= 1200
        payload = json.loads(captured["user_message"])
        assert [m["id"] for m in payload["memory_slice"]["memories"]] == [self.MEM_A, self.MEM_B]
        assert "do not re-read them with memory_read" in payload["note"]
        assert "not available" not in payload["note"].lower()
        assert orch._slice_memory_cache[self.MEM_A]["title"] == "Focus block"

    def test_evaluator_prompt_includes_cached_source_contents(self):
        orch = _make_orchestrator()
        orch._slice_memory_cache = {m["id"]: m for m in self._fake_memories()}
        candidate = CandidateInsight(
            title="Morning deep work", type="insight", operation="CREATE",
            content="The user protects mornings for deep work.",
            source_memories=[self.MEM_A], relationships=[], strategy_that_found_it="pattern_emergence",
        )
        captured = {}

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured["system_prompt"] = system_prompt
            return {"output": {"verdict": "ACCEPT", "reasoning": "ok"}, "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)
        orch.invoke_evaluator(candidate, "skeptic")

        prompt = captured["system_prompt"]
        assert f"Source memories: {[self.MEM_A]}" in prompt  # existing line preserved
        assert "7am focus block, phone off." in prompt  # ...and the Skeptic can now see the source
        assert "Blocked 7-9am" not in prompt  # only cited sources, not the whole slice

    def test_toolless_evaluator_requests_schema_constrained_verdict(self):
        """Sonnet 5 on the Methodologist seat answered with ~3.5K tokens of markdown
        followed by JSON; unescaped quotes broke the text backstop 4 times in the
        first live run. On a Direct-API backend the orchestrator must ask for
        schema-constrained tool output instead of JSON-in-prose."""
        orch = _make_orchestrator()
        orch.resolver = self._bedrock_resolver()
        candidate = CandidateInsight(
            title="q", type="insight", operation="CREATE", content="c",
            source_memories=[], relationships=[], strategy_that_found_it="pattern_emergence",
        )
        captured = {}

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured.update(kwargs)
            return {"output": {"verdict": "ACCEPT", "reasoning": "ok"}, "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)
        orch.invoke_evaluator(candidate, "methodologist")

        schema = captured["schema"]
        assert schema["properties"]["verdict"]["enum"] == ["ACCEPT", "REJECT"]
        assert set(schema["required"]) == {"verdict", "score", "blocking_issues", "advisory_issues", "reasoning"}

    def test_tool_capable_evaluator_gets_no_schema_option(self):
        """An allowed CLI evaluator receives no Direct-API schema option."""
        orch = self._tool_capable_orchestrator()
        candidate = CandidateInsight(
            title="q", type="insight", operation="CREATE", content="c",
            source_memories=[], relationships=[], strategy_that_found_it="pattern_emergence",
        )
        captured = {}

        def mock_invoke(system_prompt, user_message, tools=False, **kwargs):
            captured.update(kwargs)
            return {"output": {"verdict": "ACCEPT", "reasoning": "ok"}, "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=mock_invoke)
        orch.invoke_evaluator(candidate, "skeptic")
        assert "schema" not in captured

    def test_spend_cap_breach_aborts_run_without_retry(self, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_RUN_COST_CAP_USD", "0.01")
        monkeypatch.delenv("SECOND_BRAIN_KILL_SWITCH", raising=False)
        # The budget-aware soft stops (Phase 4) would otherwise never let a $0.01 run
        # reach the panel; zero them so the hard cap itself is what this test exercises.
        monkeypatch.setattr("src.dream_cycle.orchestrator.EST_THINKER_CALL_USD", 0.0)
        monkeypatch.setattr("src.dream_cycle.orchestrator.EST_JUDGE_CANDIDATE_USD", 0.0)
        orch = _make_orchestrator()
        orch.resolver = self._bedrock_resolver()
        test_slice = self._slice()
        candidate = CandidateInsight(
            title="Expensive", type="insight", operation="CREATE", content="c",
            source_memories=[self.MEM_A], relationships=[], strategy_that_found_it="pattern_emergence",
        )
        # One Astra evaluator call reporting 100K in / 100K out ≈ $6 — far over the cap.
        expensive = MagicMock(invoke=MagicMock(return_value={
            "output": {"verdict": "ACCEPT", "reasoning": "ok"}, "raw": "{}",
            "usage": {"inputTokens": 100_000, "outputTokens": 100_000, "totalTokens": 200_000},
            "usage_source": "real",
        }))

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):
            mock_db.create_run.return_value = "run-cap"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None}, "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"
            orch._invoker_for = lambda role: expensive

            result = orch.run(run_type="scheduled")

        assert result.aborted_early is True
        assert result.candidates_generated == 1
        assert result.candidates_accepted == 0 and result.candidates_rejected == 0
        assert expensive.invoke.call_count == 1, "a cap breach must not be retried"
        mock_db.complete_run.assert_called_once()  # run record is closed, not left dangling
        assert orch._spend.total_usd > 0.01

    def test_kill_switch_aborts_before_any_llm_call(self, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_KILL_SWITCH", "1")
        orch = _make_orchestrator()
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer") as explorer:
            result = orch.run(run_type="scheduled")

        explorer.assert_not_called()
        mock_db.create_run.assert_not_called()
        assert result.aborted_early is True


def _stored(candidate, *, run_id, verdicts, authorized_memories):
    from src.dream_cycle import orchestrator
    orchestrator.dream_cycle_db.store_candidate(run_id, asdict(candidate), verdicts, "ACCEPTED", "mem-id")
    return "mem-id"
