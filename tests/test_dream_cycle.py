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
    """Instantiate DreamCycleOrchestrator (all-Kiro laptop profile; construction is subprocess-free)."""
    from src.dream_cycle import DreamCycleOrchestrator
    return DreamCycleOrchestrator()


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
            return {"output": {"verdict": "ACCEPT", "reasoning": "looks good"}, "raw": "{}"}

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
            return {"output": {"verdict": "ACCEPT", "reasoning": "solid"}, "raw": "{}"}

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
            return {"output": {"verdict": "ACCEPT", "reasoning": "good"}, "raw": "{}"}

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

class TestSupersedeConsistencyProperty:
    """Property-based tests for SUPERSEDE operation consistency."""

    @given(candidate=supersede_candidates)
    @settings(max_examples=20)
    def test_supersede_sets_old_status_and_creates_relationship(self, candidate):
        """For any accepted SUPERSEDE, verify old memory status='superseded',
        superseded_by relationship exists, and old relationships are preserved.

        **Validates: Requirements 8.3, 8.4, 15.2, 15.5**
        """
        from src.dream_cycle.storage import store_accepted
        new_memory_id = "new-memory-uuid"

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024) as mock_embed, \
             patch("src.dream_cycle.storage.create_memory", return_value=new_memory_id) as mock_create, \
             patch("src.dream_cycle.storage.update_memory") as mock_update, \
             patch("src.dream_cycle.storage.get_memory") as mock_get, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            # Simulate target memory exists and is active
            mock_get.return_value = {
                "id": candidate.target_memory_id,
                "status": "active",
                "content": "old content",
            }

            result_id = store_accepted(candidate)

            # 1. New memory was created
            assert result_id == new_memory_id
            mock_create.assert_called_once()

            # 2. Old memory status set to 'superseded'
            update_calls = mock_update.call_args_list
            superseded_call = [
                c for c in update_calls
                if c[0][0] == candidate.target_memory_id and c[1].get("status") == "superseded"
            ]
            assert len(superseded_call) == 1, (
                "Old memory must be marked as 'superseded'"
            )

            # 3. superseded_by relationship created from old → new
            rel_calls = mock_rel.call_args_list
            superseded_by_calls = [
                c for c in rel_calls
                if c[0][0] == candidate.target_memory_id
                and c[0][1] == new_memory_id
                and c[0][2] == "superseded_by"
            ]
            assert len(superseded_by_calls) == 1, (
                "A 'superseded_by' relationship must be created from old → new"
            )

            # 4. All proposed relationships are also created
            expected_rel_count = len(candidate.relationships) + 1  # +1 for superseded_by
            assert len(rel_calls) == expected_rel_count, (
                f"Expected {expected_rel_count} relationship calls "
                f"(1 superseded_by + {len(candidate.relationships)} proposed), "
                f"got {len(rel_calls)}"
            )


class TestSupersedeConsistencyExplicit:
    """Explicit tests for SUPERSEDE edge cases."""

    def test_supersede_target_not_found_downgrades_to_create(self):
        """If SUPERSEDE target doesn't exist, downgrade to CREATE.

        **Validates: Requirements 8.6**
        """
        from src.dream_cycle.storage import store_accepted

        candidate = CandidateInsight(
            title="Replacement Insight",
            type="insight",
            operation="SUPERSEDE",
            target_memory_id="nonexistent-uuid",
            supersedes_reason="Better version",
            content="New content",
            source_memories=[],
            relationships=[],
            strategy_that_found_it="stale_synthesis_check",
        )

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.create_memory", return_value="created-id") as mock_create, \
             patch("src.dream_cycle.storage.update_memory") as mock_update, \
             patch("src.dream_cycle.storage.get_memory", return_value=None), \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            result_id = store_accepted(candidate)

            assert result_id == "created-id"
            mock_create.assert_called_once()
            # Should NOT try to update old memory status (it doesn't exist)
            superseded_calls = [
                c for c in mock_update.call_args_list
                if c[1].get("status") == "superseded"
            ]
            assert len(superseded_calls) == 0

    def test_supersede_already_superseded_downgrades_to_create(self):
        """If SUPERSEDE target is already superseded, downgrade to CREATE.

        **Validates: Requirements 8.6**
        """
        from src.dream_cycle.storage import store_accepted

        candidate = CandidateInsight(
            title="Double Supersede",
            type="insight",
            operation="SUPERSEDE",
            target_memory_id="already-superseded-uuid",
            supersedes_reason="Even better version",
            content="Newest content",
            source_memories=[],
            relationships=[],
            strategy_that_found_it="stale_synthesis_check",
        )

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.create_memory", return_value="new-id") as mock_create, \
             patch("src.dream_cycle.storage.update_memory") as mock_update, \
             patch("src.dream_cycle.storage.get_memory", return_value={"id": "already-superseded-uuid", "status": "superseded"}), \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            result_id = store_accepted(candidate)

            assert result_id == "new-id"
            mock_create.assert_called_once()
            # Should NOT create superseded_by relationship
            superseded_by_calls = [
                c for c in mock_rel.call_args_list
                if len(c[0]) >= 3 and c[0][2] == "superseded_by"
            ]
            assert len(superseded_by_calls) == 0


# ---------------------------------------------------------------------------
# Property 11: CREATE Storage Correctness
# **Validates: Requirements 8.1, 8.5**
# ---------------------------------------------------------------------------

class TestCreateStorageCorrectnessProperty:
    """Property-based tests for CREATE storage correctness."""

    @given(candidate=create_candidates)
    @settings(max_examples=20)
    def test_create_stores_memory_with_correct_fields(self, candidate):
        """For any accepted CREATE candidate, verify memory has embedding,
        correct tags, correct metadata fields, and all relationships created.

        **Validates: Requirements 8.1, 8.5**
        """
        from src.dream_cycle.storage import store_accepted
        fake_embedding = [0.5] * 1024
        created_id = "created-memory-uuid"

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding) as mock_embed, \
             patch("src.dream_cycle.storage.create_memory", return_value=created_id) as mock_create, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            result_id = store_accepted(candidate)

            # 1. Memory was created
            assert result_id == created_id

            # 2. Embedding was generated from content
            mock_embed.assert_called_once_with(candidate.content)

            # 3. create_memory called with correct args
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args[1]

            # Tags include 'dream-cycle' and schema_operation
            assert "dream-cycle" in call_kwargs["tags"]
            assert candidate.schema_operation in call_kwargs["tags"]

            # Embedding passed
            assert call_kwargs["embedding"] == fake_embedding

            # Metadata contains required fields
            metadata = call_kwargs["metadata"]
            assert metadata["dream_cycle"] is True
            assert metadata["strategy"] == candidate.strategy_that_found_it
            assert metadata["source_memories"] == candidate.source_memories
            assert metadata["confidence"] == candidate.confidence

            # Type and title match
            assert call_kwargs["type"] == candidate.type
            assert call_kwargs["title"] == candidate.title
            assert call_kwargs["content"] == candidate.content

            # 4. All proposed relationships created
            assert mock_rel.call_count == len(candidate.relationships)
            for i, rel in enumerate(candidate.relationships):
                rel_call = mock_rel.call_args_list[i]
                assert rel_call[0][0] == created_id
                assert rel_call[0][1] == rel["target_id"]
                assert rel_call[0][2] == rel["relation_type"]


class TestCreateStorageCorrectnessExplicit:
    """Explicit tests for CREATE storage edge cases."""

    def test_create_with_no_relationships(self):
        """CREATE with empty relationships list creates memory but no relationships.

        **Validates: Requirements 8.1**
        """
        from src.dream_cycle.storage import store_accepted

        candidate = CandidateInsight(
            title="Standalone Insight",
            type="insight",
            operation="CREATE",
            content="A standalone insight with no relationships",
            source_memories=["src-1"],
            relationships=[],
            strategy_that_found_it="orphan_archaeology",
            schema_operation="assimilation",
            confidence="high",
        )

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.create_memory", return_value="mem-1") as mock_create, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            result_id = store_accepted(candidate)

            assert result_id == "mem-1"
            mock_create.assert_called_once()
            mock_rel.assert_not_called()

    def test_create_with_multiple_relationships(self):
        """CREATE with multiple relationships creates all of them.

        **Validates: Requirements 8.5**
        """
        from src.dream_cycle.storage import store_accepted

        candidate = CandidateInsight(
            title="Connected Insight",
            type="connection",
            operation="CREATE",
            content="An insight connecting multiple memories",
            source_memories=["src-1", "src-2"],
            relationships=[
                {"target_id": "target-1", "relation_type": "supports", "note": "evidence"},
                {"target_id": "target-2", "relation_type": "contradicts", "note": "conflict"},
                {"target_id": "target-3", "relation_type": "extends"},
            ],
            strategy_that_found_it="cross_project_collision",
            schema_operation="accommodation",
            confidence="medium",
        )

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.2] * 1024), \
             patch("src.dream_cycle.storage.create_memory", return_value="mem-2") as mock_create, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            result_id = store_accepted(candidate)

            assert result_id == "mem-2"
            assert mock_rel.call_count == 3

            # Verify each relationship
            calls = mock_rel.call_args_list
            assert calls[0][0] == ("mem-2", "target-1", "supports", "evidence")
            assert calls[1][0] == ("mem-2", "target-2", "contradicts", "conflict")
            assert calls[2][0] == ("mem-2", "target-3", "extends", None)


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

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db:
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
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

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
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

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
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
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
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"

            mock_eval.return_value = EvaluatorVerdict(
                role="skeptic", verdict="ACCEPT", reasoning="good"
            )

            result = orch.run(run_type="scheduled")

            # All 3 candidates should be processed
            assert result.candidates_generated == 3
            # 4 evaluators × 3 candidates = 12 evaluator calls
            assert mock_eval.call_count == 12


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
        from src.dream_cycle.digest import generate_digest
        run_id = "test-run-digest"

        # Capture the written content via a patched write_text
        written_content = {}

        def _patched_write_text(self_path, text, encoding=None):
            written_content["text"] = text

        with patch("src.dream_cycle.digest.dream_cycle_db") as mock_digest_db, \
             patch("pathlib.Path.write_text", _patched_write_text):
            mock_digest_db.get_evaluator_verdicts_for_run.return_value = {}
            mock_digest_db.was_feedback_injected.return_value = False
            generate_digest(run_id, accepted, rejected)

        assert "text" in written_content, "Digest file should have been written"
        digest_text = written_content["text"]

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
        from src.dream_cycle.digest import generate_digest

        rejected_candidate = CandidateInsight(
            title="Rejected One",
            type="insight",
            operation="CREATE",
            content="Rejected content",
            source_memories=["src-2"],
            relationships=[],
            strategy_that_found_it="orphan_archaeology",
        )

        written_content = {}

        def _capture_write(self_path, text, encoding=None):
            written_content["text"] = text

        with patch("src.dream_cycle.digest.dream_cycle_db") as mock_digest_db, \
             patch("pathlib.Path.write_text", _capture_write):
            mock_digest_db.get_evaluator_verdicts_for_run.return_value = {}
            mock_digest_db.was_feedback_injected.return_value = False
            generate_digest("run-empty", [], [rejected_candidate])

        digest_text = written_content["text"]
        assert "No insights were accepted" in digest_text
        assert "Accepted: 0" in digest_text
        assert "Rejected: 1" in digest_text

    def test_digest_update_operation_shows_operation_type(self):
        """Digest with UPDATE operation shows the operation type and target.

        **Validates: Requirements 12.4**
        """
        from src.dream_cycle.digest import generate_digest

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

        written_content = {}

        def _capture_write(self_path, text, encoding=None):
            written_content["text"] = text

        with patch("src.dream_cycle.digest.dream_cycle_db") as mock_digest_db, \
             patch("pathlib.Path.write_text", _capture_write):
            mock_digest_db.get_evaluator_verdicts_for_run.return_value = {}
            mock_digest_db.was_feedback_injected.return_value = False
            generate_digest("run-update", [update_candidate], [])

        digest_text = written_content["text"]
        assert "**Operation**: UPDATE" in digest_text
        assert "target-mem-123" in digest_text
        assert "src-a" in digest_text
        assert "src-b" in digest_text

    def test_digest_file_written_to_logs_directory(self):
        """Digest file is written to logs/ directory with correct naming.

        **Validates: Requirements 12.1**
        """
        from src.dream_cycle.digest import generate_digest

        candidate = CandidateInsight(
            title="Simple Insight",
            type="insight",
            operation="CREATE",
            content="Simple content",
            source_memories=["src-1"],
            relationships=[],
            strategy_that_found_it="pattern_emergence",
        )

        written_content = {}

        def _capture_write(self_path, text, encoding=None):
            written_content["text"] = text
            written_content["path"] = str(self_path)

        with patch("src.dream_cycle.digest.dream_cycle_db") as mock_digest_db, \
             patch("pathlib.Path.write_text", _capture_write):
            mock_digest_db.get_evaluator_verdicts_for_run.return_value = {}
            mock_digest_db.was_feedback_injected.return_value = False
            result_path = generate_digest("run-path", [candidate], [])

        # Result path is a string pointing to a logs/ directory (absolute since
        # digest resolves logs/ from the module path, not cwd).
        assert isinstance(result_path, str)
        assert "/logs/" in result_path or result_path.startswith("logs/")
        assert "dream-cycle-digest-" in result_path
        assert result_path.endswith(".md")
        # Content was written
        assert "text" in written_content


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
             patch.object(orch, "invoke_explorer", return_value=[slice_a, slice_b]), \
             patch.object(orch, "invoke_thinker", side_effect=thinker_side_effect), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

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
             patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
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
             patch("src.dream_cycle.orchestrator.time.sleep"), \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[c1, c2]), \
             patch.object(orch, "invoke_evaluator", side_effect=eval_side_effect), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

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
        """Byzantine tolerance (real votes): one dissenting REJECT among four is
        tolerated — 3/4 ACCEPT → ACCEPTED. The quorum tolerates a bad *vote*;
        crashes are handled separately (retry/abort), not folded into the tally.
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()
        candidate = self._make_candidate("One Dissent Candidate")

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

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
        """Two genuine REJECT votes (2/4 ACCEPT) → REJECTED — the quorum bound.
        These are real verdicts, not crashes: rejected on merit, run completes.
        """
        orch = _make_orchestrator()
        test_slice = self._make_slice()
        candidate = self._make_candidate("Two Dissent Candidate")

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
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

    def test_supersede_target_missing_downgrades_to_create_with_warning(self):
        """When a SUPERSEDE target memory does not exist, the operation
        downgrades to CREATE and a warning is logged.

        **Validates: Requirement 16.5**
        """
        from src.dream_cycle.storage import store_accepted

        candidate = CandidateInsight(
            title="Supersede Missing Target",
            type="insight",
            operation="SUPERSEDE",
            target_memory_id="nonexistent-uuid",
            supersedes_reason="Better version",
            content="Improved content",
            source_memories=["src-1"],
            relationships=[],
            strategy_that_found_it="stale_synthesis_check",
        )

        with patch("src.dream_cycle.storage.generate_embedding", return_value=[0.1] * 1024), \
             patch("src.dream_cycle.storage.create_memory", return_value="created-id") as mock_create, \
             patch("src.dream_cycle.storage.update_memory") as mock_update, \
             patch("src.dream_cycle.storage.get_memory", return_value=None), \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5), \
             patch("src.dream_cycle.storage.logger") as mock_logger:

            result_id = store_accepted(candidate)

            # Should create a new memory (downgraded to CREATE)
            assert result_id == "created-id"
            mock_create.assert_called_once()

            # Should NOT mark any memory as superseded
            superseded_calls = [
                c for c in mock_update.call_args_list
                if c[1].get("status") == "superseded"
            ]
            assert len(superseded_calls) == 0

            # Should NOT create a superseded_by relationship
            superseded_by_calls = [
                c for c in mock_rel.call_args_list
                if len(c[0]) >= 3 and c[0][2] == "superseded_by"
            ]
            assert len(superseded_by_calls) == 0

            # A warning should have been logged
            mock_logger.warning.assert_called_once()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "SUPERSEDE" in warning_msg
            assert "downgrading" in warning_msg.lower() or "CREATE" in warning_msg


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

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # Simulate candidate has a created_memory_id
        mock_cur.fetchone.return_value = (memory_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

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

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # No created_memory_id
        mock_cur.fetchone.return_value = (None,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

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


class TestRunRecordCompletenessProperty:
    """Property-based tests for run record completeness.

    For any completed run, verify completed_at is non-null, candidate counts
    sum correctly (generated = accepted + deferred + rejected), and every
    candidate has all 4 evaluator verdicts stored.
    """

    @given(
        run_type=run_types,
        candidate_entries=st.lists(_run_candidate_entry, min_size=1, max_size=8),
    )
    @settings(max_examples=30)
    def test_completed_run_has_correct_counts_and_all_verdicts(
        self, run_type, candidate_entries
    ):
        """For any completed run with a mix of accepted/rejected
        candidates, verify:
        1. completed_at is not None
        2. candidates_generated == candidates_accepted + candidates_rejected
        3. store_candidate was called for every candidate with all 4 evaluator verdicts

        **Validates: Requirements 1.5, 1.6, 2.5**
        """
        orch = _make_orchestrator()

        test_slice = MemorySlice(
            name="Test Slice",
            strategy="pattern_emergence",
            memory_ids=["m1", "m2"],
            memory_titles=["Memory 1", "Memory 2"],
            hypothesis="Test hypothesis",
        )

        # session_start truncates to 2 candidates per slice (Req 11.4)
        effective_entries = candidate_entries
        if run_type == "session_start" and len(candidate_entries) > 2:
            effective_entries = candidate_entries[:2]

        # Build CandidateInsight objects from generated entries
        candidates = []
        for entry in candidate_entries:
            candidates.append(CandidateInsight(
                title=entry["title"],
                type="insight",
                operation="CREATE",
                content=f"Content for {entry['title']}",
                source_memories=["m1"],
                relationships=[],
                strategy_that_found_it="pattern_emergence",
            ))

        # Build evaluator side effects for the effective (post-truncation) entries
        eval_side_effects = []
        for entry in effective_entries:
            skeptic_v, advocate_v, epistemologist_v, methodologist_v = entry["verdicts"]
            eval_side_effects.append(
                EvaluatorVerdict(role="skeptic", verdict=skeptic_v, reasoning=f"skeptic says {skeptic_v}")
            )
            eval_side_effects.append(
                EvaluatorVerdict(role="advocate", verdict=advocate_v, reasoning=f"advocate says {advocate_v}")
            )
            eval_side_effects.append(
                EvaluatorVerdict(role="epistemologist", verdict=epistemologist_v, reasoning=f"epistemologist says {epistemologist_v}")
            )
            eval_side_effects.append(
                EvaluatorVerdict(role="methodologist", verdict=methodologist_v, reasoning=f"methodologist says {methodologist_v}")
            )

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

            # Handle session_start frequency cap
            mock_db.should_run_briefing.return_value = True
            mock_db.create_run.return_value = "test-run-id"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100,
                "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"

            mock_eval.side_effect = eval_side_effects

            result = orch.run(run_type=run_type)

        expected_count = len(effective_entries)

        # 1. completed_at is not None
        assert result.completed_at is not None, (
            "completed_at must be non-null for a completed run"
        )

        # 2. candidates_generated == accepted + rejected
        assert result.candidates_generated == (
            result.candidates_accepted + result.candidates_rejected
        ), (
            f"Count mismatch: generated={result.candidates_generated} != "
            f"accepted({result.candidates_accepted}) "
            f"+ rejected({result.candidates_rejected})"
        )

        # 3. store_candidate was called for every processed candidate
        assert mock_db.store_candidate.call_count == expected_count, (
            f"store_candidate called {mock_db.store_candidate.call_count} times, "
            f"expected {expected_count}"
        )

        # 4. Every store_candidate call has all 4 evaluator verdicts
        for i, sc_call in enumerate(mock_db.store_candidate.call_args_list):
            verdicts_dict = sc_call[0][2]  # third positional arg is verdicts
            assert verdicts_dict.get("evaluator_a_verdict") is not None, (
                f"Candidate {i}: evaluator_a_verdict missing"
            )
            assert verdicts_dict.get("evaluator_a_reasoning") is not None, (
                f"Candidate {i}: evaluator_a_reasoning missing"
            )
            assert verdicts_dict.get("evaluator_b_verdict") is not None, (
                f"Candidate {i}: evaluator_b_verdict missing"
            )
            assert verdicts_dict.get("evaluator_b_reasoning") is not None, (
                f"Candidate {i}: evaluator_b_reasoning missing"
            )
            assert verdicts_dict.get("evaluator_c_verdict") is not None, (
                f"Candidate {i}: evaluator_c_verdict missing"
            )
            assert verdicts_dict.get("evaluator_c_reasoning") is not None, (
                f"Candidate {i}: evaluator_c_reasoning missing"
            )
            assert verdicts_dict.get("evaluator_d_verdict") is not None, (
                f"Candidate {i}: evaluator_d_verdict missing"
            )
            assert verdicts_dict.get("evaluator_d_reasoning") is not None, (
                f"Candidate {i}: evaluator_d_reasoning missing"
            )


class TestRunRecordCompletenessExplicit:
    """Explicit unit tests for run record completeness edge cases."""

    def test_all_accepted_run_counts_match(self):
        """A run where all candidates are accepted has correct counts.

        **Validates: Requirements 1.5, 1.6, 2.5**
        """
        orch = _make_orchestrator()

        test_slice = MemorySlice(
            name="Test Slice",
            strategy="pattern_emergence",
            memory_ids=["m1"],
            memory_titles=["Memory 1"],
            hypothesis="Test",
        )

        candidates = [
            CandidateInsight(
                title=f"Insight {i}",
                type="insight",
                operation="CREATE",
                content=f"Content {i}",
                source_memories=["m1"],
                relationships=[],
                strategy_that_found_it="pattern_emergence",
            )
            for i in range(3)
        ]

        # All 4/4 ACCEPT for each candidate
        eval_returns = [
            EvaluatorVerdict(role=r, verdict="ACCEPT", reasoning="good")
            for _ in range(3)
            for r in ["skeptic", "advocate", "epistemologist", "methodologist"]
        ]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

            mock_db.create_run.return_value = "run-all-accept"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"
            mock_eval.side_effect = eval_returns

            result = orch.run(run_type="scheduled")

        assert result.completed_at is not None
        assert result.candidates_generated == 3
        assert result.candidates_accepted == 3
        assert result.candidates_rejected == 0
        assert result.candidates_generated == (
            result.candidates_accepted + result.candidates_rejected
        )
        assert mock_db.store_candidate.call_count == 3

    def test_mixed_verdicts_run_counts_match(self):
        """A run with mixed verdicts (1 accepted, 2 rejected)
        has correct counts.

        **Validates: Requirements 1.5, 1.6, 2.5**
        """
        orch = _make_orchestrator()

        test_slice = MemorySlice(
            name="Test Slice",
            strategy="pattern_emergence",
            memory_ids=["m1"],
            memory_titles=["Memory 1"],
            hypothesis="Test",
        )

        candidates = [
            CandidateInsight(
                title=f"Insight {i}",
                type="insight",
                operation="CREATE",
                content=f"Content {i}",
                source_memories=["m1"],
                relationships=[],
                strategy_that_found_it="pattern_emergence",
            )
            for i in range(3)
        ]

        # Candidate 0: 4/4 ACCEPT → ACCEPTED
        # Candidate 1: 2/4 ACCEPT → REJECTED (binary BFT: ≤2/4 = REJECTED)
        # Candidate 2: 1/4 ACCEPT → REJECTED
        eval_returns = [
            # Candidate 0
            EvaluatorVerdict(role="skeptic", verdict="ACCEPT", reasoning="good"),
            EvaluatorVerdict(role="advocate", verdict="ACCEPT", reasoning="useful"),
            EvaluatorVerdict(role="epistemologist", verdict="ACCEPT", reasoning="novel"),
            EvaluatorVerdict(role="methodologist", verdict="ACCEPT", reasoning="sound"),
            # Candidate 1
            EvaluatorVerdict(role="skeptic", verdict="ACCEPT", reasoning="ok"),
            EvaluatorVerdict(role="advocate", verdict="ACCEPT", reasoning="relevant"),
            EvaluatorVerdict(role="epistemologist", verdict="REJECT", reasoning="not novel"),
            EvaluatorVerdict(role="methodologist", verdict="REJECT", reasoning="weak methodology"),
            # Candidate 2
            EvaluatorVerdict(role="skeptic", verdict="REJECT", reasoning="weak"),
            EvaluatorVerdict(role="advocate", verdict="REJECT", reasoning="irrelevant"),
            EvaluatorVerdict(role="epistemologist", verdict="ACCEPT", reasoning="interesting"),
            EvaluatorVerdict(role="methodologist", verdict="REJECT", reasoning="not reproducible"),
        ]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
             patch.object(orch, "invoke_explorer", return_value=[test_slice]), \
             patch.object(orch, "invoke_thinker", return_value=candidates), \
             patch.object(orch, "invoke_evaluator") as mock_eval, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):

            mock_db.create_run.return_value = "run-mixed"
            mock_db.get_memory_stats.return_value = {
                "total_count": 100, "recent_activity": 5,
                "date_range": {"min": None, "max": None},
                "type_distribution": {},
            }
            mock_db.get_previous_run_id.return_value = None
            mock_db.store_candidate.return_value = "cand-id"
            mock_eval.side_effect = eval_returns

            result = orch.run(run_type="scheduled")

        assert result.completed_at is not None
        assert result.candidates_generated == 3
        assert result.candidates_accepted == 1
        assert result.candidates_rejected == 2
        assert result.candidates_generated == (
            result.candidates_accepted + result.candidates_rejected
        )
        # All 3 candidates stored
        assert mock_db.store_candidate.call_count == 3

        # Verify each store_candidate call has all 4 verdicts
        for sc_call in mock_db.store_candidate.call_args_list:
            verdicts_dict = sc_call[0][2]
            for key in [
                "evaluator_a_verdict", "evaluator_a_reasoning",
                "evaluator_b_verdict", "evaluator_b_reasoning",
                "evaluator_c_verdict", "evaluator_c_reasoning",
                "evaluator_d_verdict", "evaluator_d_reasoning",
            ]:
                assert verdicts_dict.get(key) is not None, f"Missing {key}"


# ---------------------------------------------------------------------------
# Feature: dream-cycle-decomposition — Storage Import Verification & Properties
# **Validates: Requirements 3.6, 3.7**
# ---------------------------------------------------------------------------


class TestStorageImportVerification:
    """Verify standalone storage functions are importable and behave correctly.

    **Feature: dream-cycle-decomposition, Property 2: store_accepted behavioral equivalence across operations**
    **Feature: dream-cycle-decomposition, Property 3: check_duplicate skips chunks and uses strict threshold**
    """

    def test_import_store_accepted_and_check_duplicate(self):
        """Verify the import path for standalone storage functions works.

        **Validates: Requirements 3.6, 3.7**
        """
        from src.dream_cycle.storage import store_accepted, check_duplicate

        assert callable(store_accepted)
        assert callable(check_duplicate)


class TestStoreAcceptedBehavioralEquivalence:
    """Property 2: store_accepted behavioral equivalence across operations.

    For any accepted CandidateInsight with operation CREATE, UPDATE, or SUPERSEDE,
    the standalone store_accepted() produces the correct memory creation calls,
    relationship creation calls, and status updates.

    **Feature: dream-cycle-decomposition, Property 2: store_accepted behavioral equivalence across operations**
    **Validates: Requirements 3.6**
    """

    @given(candidate=create_candidates)
    @settings(max_examples=100)
    def test_create_operation_stores_with_correct_tags_and_metadata(self, candidate):
        """For any CREATE candidate, store_accepted generates embedding from content,
        creates memory with dream-cycle tag, schema_operation tag, and correct metadata.

        **Feature: dream-cycle-decomposition, Property 2: store_accepted behavioral equivalence across operations**
        **Validates: Requirements 3.6**
        """
        from src.dream_cycle.storage import store_accepted

        fake_embedding = [0.42] * 128
        created_id = "created-mem-id"

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding) as mock_embed, \
             patch("src.dream_cycle.storage.create_memory", return_value=created_id) as mock_create, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic") as mock_classify, \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5) as mock_depth:

            result = store_accepted(candidate)

            assert result == created_id
            mock_embed.assert_called_once_with(candidate.content)
            mock_classify.assert_called_once()
            mock_depth.assert_called_once_with(candidate.content)
            mock_create.assert_called_once()

            kw = mock_create.call_args[1]
            assert "dream-cycle" in kw["tags"]
            assert candidate.schema_operation in kw["tags"]
            assert kw["embedding"] == fake_embedding
            assert kw["mem_class"] == "semantic"
            assert kw["metadata"]["dream_cycle"] is True
            assert kw["metadata"]["depth_score"] == 0.5
            assert kw["metadata"]["strategy"] == candidate.strategy_that_found_it
            assert kw["metadata"]["source_memories"] == candidate.source_memories
            assert kw["metadata"]["confidence"] == candidate.confidence
            assert kw["type"] == candidate.type
            assert kw["title"] == candidate.title
            assert kw["content"] == candidate.content

            assert mock_rel.call_count == len(candidate.relationships)
            for i, rel in enumerate(candidate.relationships):
                assert mock_rel.call_args_list[i][0][0] == created_id
                assert mock_rel.call_args_list[i][0][1] == rel["target_id"]
                assert mock_rel.call_args_list[i][0][2] == rel["relation_type"]

    @given(
        candidate=st.builds(
            CandidateInsight,
            title=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))),
            type=insight_types,
            operation=st.just("UPDATE"),
            target_memory_id=st.uuids().map(str),
            supersedes_reason=st.none(),
            schema_operation=schema_operations,
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
    )
    @settings(max_examples=100)
    def test_update_operation_calls_update_memory_and_creates_relationships(self, candidate):
        """For any UPDATE candidate, store_accepted calls update_memory on the
        target, generates embedding from content, and creates all proposed relationships.

        **Feature: dream-cycle-decomposition, Property 2: store_accepted behavioral equivalence across operations**
        **Validates: Requirements 3.6**
        """
        from src.dream_cycle.storage import store_accepted

        fake_embedding = [0.42] * 128

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding) as mock_embed, \
             patch("src.dream_cycle.storage.update_memory") as mock_update, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.get_memory", return_value={"type": "idea", "source_type": None, "metadata": {"existing_key": "preserved"}}) as mock_get, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic") as mock_classify, \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5) as mock_depth:

            result = store_accepted(candidate)

            assert result == candidate.target_memory_id
            mock_embed.assert_called_once_with(candidate.content)
            mock_get.assert_called_once_with(candidate.target_memory_id)
            mock_classify.assert_called_once()
            mock_depth.assert_called_once_with(candidate.content)
            mock_update.assert_called_once()
            update_args = mock_update.call_args
            assert update_args[0][0] == candidate.target_memory_id
            assert update_args[1]["content"] == candidate.content
            assert update_args[1]["embedding"] == fake_embedding
            # Verify metadata was merged, not overwritten
            assert update_args[1]["metadata"]["existing_key"] == "preserved"
            assert "last_dream_cycle_update" in update_args[1]["metadata"]
            assert update_args[1]["metadata"]["depth_score"] == 0.5
            assert update_args[1]["mem_class"] == "semantic"

            assert mock_rel.call_count == len(candidate.relationships)
            for i, rel in enumerate(candidate.relationships):
                assert mock_rel.call_args_list[i][0][0] == candidate.target_memory_id
                assert mock_rel.call_args_list[i][0][1] == rel["target_id"]

    @given(candidate=supersede_candidates)
    @settings(max_examples=100)
    def test_supersede_operation_creates_new_marks_old_and_links(self, candidate):
        """For any SUPERSEDE candidate with an active target, store_accepted creates
        a new memory, marks the old as superseded, and creates a superseded_by relationship.

        **Feature: dream-cycle-decomposition, Property 2: store_accepted behavioral equivalence across operations**
        **Validates: Requirements 3.6**
        """
        from src.dream_cycle.storage import store_accepted

        new_id = "new-supersede-id"
        fake_embedding = [0.42] * 128

        with patch("src.dream_cycle.storage.generate_embedding", return_value=fake_embedding) as mock_embed, \
             patch("src.dream_cycle.storage.create_memory", return_value=new_id) as mock_create, \
             patch("src.dream_cycle.storage.update_memory") as mock_update, \
             patch("src.dream_cycle.storage.get_memory", return_value={"id": candidate.target_memory_id, "status": "active"}) as mock_get, \
             patch("src.dream_cycle.storage.create_relationship") as mock_rel, \
             patch("src.dream_cycle.storage.classify_memory", return_value="semantic"), \
             patch("src.dream_cycle.storage.compute_depth_score", return_value=0.5):

            result = store_accepted(candidate)

            assert result == new_id
            mock_embed.assert_called_once_with(candidate.content)
            mock_create.assert_called_once()

            kw = mock_create.call_args[1]
            assert "dream-cycle" in kw["tags"]
            assert candidate.schema_operation in kw["tags"]
            assert kw["metadata"]["dream_cycle"] is True

            # Old memory marked superseded
            superseded_calls = [
                c for c in mock_update.call_args_list
                if c[0][0] == candidate.target_memory_id and c[1].get("status") == "superseded"
            ]
            assert len(superseded_calls) == 1

            # superseded_by relationship created
            superseded_by = [
                c for c in mock_rel.call_args_list
                if c[0][0] == candidate.target_memory_id and c[0][1] == new_id and c[0][2] == "superseded_by"
            ]
            assert len(superseded_by) == 1

            # All proposed relationships also created
            assert mock_rel.call_count == len(candidate.relationships) + 1


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
# fail loudly. Fabricating a REJECT would spend the f=1 Byzantine budget on a
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

class TestFinalOrchestratorMethodSet:
    """Verify the set of non-dunder methods on DreamCycleOrchestrator is exactly
    the expected coordination methods after all extractions."""

    def test_method_set_is_exactly_coordination_methods(self):
        """The set of non-dunder methods on DreamCycleOrchestrator must be exactly:
        run, _run_pipeline, invoke_explorer, invoke_thinker, invoke_evaluator,
        _invoke_evaluator_safe, _invoker_for, _capture.

        **Feature: dream-cycle-decomposition, Property 5: Final orchestrator method set contains only coordination methods**
        **Validates: Requirements 7.1**
        """
        from src.dream_cycle.orchestrator import DreamCycleOrchestrator

        expected_methods = {
            "run",
            "_run_pipeline",
            "_aborted_result",
            "invoke_explorer",
            "invoke_thinker",
            "invoke_evaluator",
            "_invoke_evaluator_safe",
            "_invoker_for",
            "_backend_provenance",
            "_capture",
        }

        actual_methods = {
            name for name in dir(DreamCycleOrchestrator)
            if not name.startswith("__") and callable(getattr(DreamCycleOrchestrator, name))
        }

        assert actual_methods == expected_methods, (
            f"Expected methods: {expected_methods}\n"
            f"Actual methods: {actual_methods}\n"
            f"Extra: {actual_methods - expected_methods}\n"
            f"Missing: {expected_methods - actual_methods}"
        )

    def test_extracted_methods_not_on_class(self):
        """Extracted methods (tally_consensus, store_accepted, check_duplicate,
        generate_digest, build_feedback_injection, _get_previous_run_id) must
        NOT exist as methods on the orchestrator class.

        **Feature: dream-cycle-decomposition, Property 5: Final orchestrator method set contains only coordination methods**
        **Validates: Requirements 7.1**
        """
        from src.dream_cycle.orchestrator import DreamCycleOrchestrator

        extracted = [
            "tally_consensus",
            "store_accepted",
            "check_duplicate",
            "generate_digest",
            "build_feedback_injection",
            "_get_previous_run_id",
        ]

        for name in extracted:
            assert not hasattr(DreamCycleOrchestrator, name), (
                f"Extracted method '{name}' should not exist on DreamCycleOrchestrator"
            )


# ---------------------------------------------------------------------------
# Property 7: Public API preservation through __init__.py
# **Feature: dream-cycle-decomposition, Property 7: Public API preservation through __init__.py**
# **Validates: Requirements 1.1, 1.4, 8.6**
# ---------------------------------------------------------------------------

class TestPublicAPIPreservation:
    """Verify that the __init__.py re-export resolves to the same class object
    as a direct import from the orchestrator module."""

    def test_init_reexport_identity(self):
        """from src.dream_cycle import DreamCycleOrchestrator must resolve to
        the same object as from src.dream_cycle.orchestrator import DreamCycleOrchestrator.

        **Feature: dream-cycle-decomposition, Property 7: Public API preservation through __init__.py**
        **Validates: Requirements 1.1, 1.4, 8.6**
        """
        from src.dream_cycle import DreamCycleOrchestrator as FromPackage
        from src.dream_cycle.orchestrator import DreamCycleOrchestrator as FromModule

        assert FromPackage is FromModule, (
            "DreamCycleOrchestrator from __init__.py must be the same object "
            "as from orchestrator.py"
        )

    def test_init_exports_in_all(self):
        """__all__ in src.dream_cycle.__init__ must include DreamCycleOrchestrator.

        **Feature: dream-cycle-decomposition, Property 7: Public API preservation through __init__.py**
        **Validates: Requirements 1.1, 1.4, 8.6**
        """
        import src.dream_cycle as pkg

        assert hasattr(pkg, "__all__")
        assert "DreamCycleOrchestrator" in pkg.__all__
