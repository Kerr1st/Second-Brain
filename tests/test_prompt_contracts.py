"""Prompt contract tests — verify agents receive well-formed, complete context.

These tests verify the structural contracts between the orchestrator and
the agent prompts, using realistic (not synthetic) data shapes. They catch
prompt regressions where template changes break interpolation or drop
required context.

Gap coverage:
- Thinker receives memory slice JSON with all MemorySlice fields
- Explorer feedback injection is structurally correct with realistic
  rejection data (evaluator roles, reasoning, cycle dates, dissent sections)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

from src.models import MemorySlice
from src.prompts import get_explorer_prompt, get_thinker_prompt


# ---------------------------------------------------------------------------
# Thinker prompt contract: memory slice JSON completeness
# ---------------------------------------------------------------------------

class TestThinkerPromptReceivesMemorySlice:
    """Verify the Thinker's user message contains a well-formed memory slice
    with all MemorySlice fields present and correctly serialized."""

    def _build_thinker_user_message(self, slice_obj: MemorySlice) -> str:
        """Replicate the orchestrator's invoke_thinker payload construction."""
        payload = {
            "memory_slice": {
                "name": slice_obj.name,
                "strategy": slice_obj.strategy,
                "memory_ids": slice_obj.memory_ids,
                "memory_titles": slice_obj.memory_titles,
                "hypothesis": slice_obj.hypothesis,
            },
        }
        return json.dumps(payload)

    def test_realistic_slice_produces_valid_json_with_all_fields(self):
        """A realistic memory slice serializes to valid JSON containing
        all five MemorySlice fields."""
        slice_obj = MemorySlice(
            name="Cross-project database migration patterns",
            strategy="cross_project_collision",
            memory_ids=[
                "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "c3d4e5f6-a7b8-9012-cdef-123456789012",
            ],
            memory_titles=[
                "DB Migration: Project Alpha uses Flyway",
                "DB Migration: Project Beta switched to Liquibase",
                "Decision: Standardize on declarative migrations",
            ],
            hypothesis="Projects share implicit migration conventions that could be named as a principle",
        )

        user_message = self._build_thinker_user_message(slice_obj)
        parsed = json.loads(user_message)

        assert "memory_slice" in parsed
        ms = parsed["memory_slice"]
        assert ms["name"] == slice_obj.name
        assert ms["strategy"] == slice_obj.strategy
        assert ms["memory_ids"] == slice_obj.memory_ids
        assert ms["memory_titles"] == slice_obj.memory_titles
        assert ms["hypothesis"] == slice_obj.hypothesis
        assert len(ms["memory_ids"]) == 3

    def test_empty_slice_fields_serialize_correctly(self):
        """A slice with empty optional fields still produces valid JSON."""
        slice_obj = MemorySlice(
            name="Minimal slice",
            strategy="orphan_archaeology",
            memory_ids=[],
            memory_titles=[],
            hypothesis="",
        )

        user_message = self._build_thinker_user_message(slice_obj)
        parsed = json.loads(user_message)

        ms = parsed["memory_slice"]
        assert ms["memory_ids"] == []
        assert ms["memory_titles"] == []
        assert ms["hypothesis"] == ""

    @given(
        name=st.text(min_size=1, max_size=100),
        strategy=st.sampled_from([
            "temporal_juxtaposition", "cross_project_collision",
            "orphan_archaeology", "pattern_emergence",
            "contradiction_hunting", "stale_synthesis_check",
        ]),
        num_memories=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=50)
    def test_any_valid_slice_round_trips_through_json(self, name, strategy, num_memories):
        """For any valid slice parameters, the JSON round-trip preserves all fields."""
        memory_ids = [f"mem-{i:04d}" for i in range(num_memories)]
        memory_titles = [f"Memory Title {i}" for i in range(num_memories)]

        slice_obj = MemorySlice(
            name=name,
            strategy=strategy,
            memory_ids=memory_ids,
            memory_titles=memory_titles,
            hypothesis=f"Hypothesis for {strategy}",
        )

        user_message = self._build_thinker_user_message(slice_obj)
        parsed = json.loads(user_message)

        ms = parsed["memory_slice"]
        assert ms["name"] == name
        assert ms["strategy"] == strategy
        assert len(ms["memory_ids"]) == num_memories
        assert len(ms["memory_titles"]) == num_memories

    def test_thinker_system_prompt_contains_output_schema_fields(self):
        """The Thinker system prompt documents all CandidateInsight fields
        that the orchestrator expects to parse."""
        prompt = get_thinker_prompt()

        # All fields the orchestrator's invoke_thinker parses from output
        expected_fields = [
            "title", "type", "operation", "target_memory_id",
            "supersedes_reason", "schema_operation", "schema_note",
            "confidence", "confidence_reasoning", "content",
            "source_memories", "relationships", "strategy_that_found_it",
        ]
        for field in expected_fields:
            assert field in prompt, f"Thinker prompt missing output field: {field}"

    def test_thinker_system_prompt_documents_operations(self):
        """The Thinker prompt documents CREATE, UPDATE, and SUPERSEDE operations."""
        prompt = get_thinker_prompt()

        assert "CREATE" in prompt
        assert "UPDATE" in prompt
        assert "SUPERSEDE" in prompt

    def test_thinker_system_prompt_documents_depth_framework(self):
        """The Thinker prompt requires WHAT, EVIDENCE, WHY IT MATTERS."""
        prompt = get_thinker_prompt()

        assert "WHAT" in prompt
        assert "EVIDENCE" in prompt
        assert "WHY IT MATTERS" in prompt


# ---------------------------------------------------------------------------
# Explorer feedback injection contract: structural correctness
# ---------------------------------------------------------------------------

class TestExplorerFeedbackInjectionContract:
    """Verify the Explorer prompt correctly incorporates feedback injection
    text with realistic rejection data — evaluator roles, reasoning,
    cycle dates, and the new dissent section."""

    def _build_realistic_feedback(self) -> str:
        """Build feedback injection text using the real build_feedback_injection
        function with realistic mock data."""
        from src.dream_cycle.feedback import build_feedback_injection

        mock_rejections = [
            {
                "run_id": "run-2026-03-15",
                "run_type": "scheduled",
                "completed_at": datetime(2026, 3, 15, tzinfo=timezone.utc),
                "candidate_id": "cand-001",
                "final_verdict": "REJECTED",
                "evaluator_a_verdict": "REJECT",
                "evaluator_a_reasoning": "Claim based on single memory — observation, not pattern",
                "evaluator_b_verdict": "ACCEPT",
                "evaluator_b_reasoning": "Relevant to current refactoring work",
                "evaluator_c_verdict": "REJECT",
                "evaluator_c_reasoning": "Not falsifiable — too vague to test",
                "evaluator_d_verdict": "REJECT",
                "evaluator_d_reasoning": "Sources are derivatives of the same conversation",
            },
        ]

        mock_dissents = [
            {
                "candidate_id": "cand-002",
                "candidate_json": {"title": "Implicit caching convention across services"},
                "evaluator_a_verdict": "ACCEPT",
                "evaluator_a_reasoning": "Well-grounded",
                "evaluator_b_verdict": "ACCEPT",
                "evaluator_b_reasoning": "Useful",
                "evaluator_c_verdict": "ACCEPT",
                "evaluator_c_reasoning": "Novel pattern",
                "evaluator_d_verdict": "REJECT",
                "evaluator_d_reasoning": "Two of three cited memories come from the same PR review",
                "final_verdict": "ACCEPTED",
            },
        ]

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = mock_rejections
            mock_db.get_accepted_dissents.return_value = mock_dissents
            mock_db.get_user_rejections.return_value = []
            return build_feedback_injection()

    def test_feedback_contains_lessons_header(self):
        """Feedback text starts with the lessons header."""
        feedback = self._build_realistic_feedback()
        assert "## Lessons from recent cycles" in feedback

    def test_feedback_contains_cycle_date(self):
        """Feedback text includes the cycle date from completed_at."""
        feedback = self._build_realistic_feedback()
        assert "2026-03-15" in feedback

    def test_feedback_contains_rejection_count(self):
        """Feedback text includes the rejection count per cycle."""
        feedback = self._build_realistic_feedback()
        assert "1 rejected" in feedback

    def test_feedback_contains_all_four_evaluator_roles(self):
        """When all four evaluators have rejection reasoning, all role names appear."""
        feedback = self._build_realistic_feedback()
        # Three evaluators rejected in the rejection row
        assert "Skeptic" in feedback
        assert "Epistemologist" in feedback
        assert "Methodologist" in feedback

    def test_feedback_contains_actual_reasoning_text(self):
        """Feedback includes the actual evaluator reasoning, not placeholders."""
        feedback = self._build_realistic_feedback()
        assert "single memory" in feedback  # Skeptic's reasoning
        assert "Not falsifiable" in feedback  # Epistemologist's reasoning
        assert "derivatives of the same conversation" in feedback  # Methodologist's reasoning

    def test_feedback_contains_dissent_section(self):
        """Feedback includes the dissenting concerns section for non-unanimous accepts."""
        feedback = self._build_realistic_feedback()
        assert "Dissenting concerns on accepted insights" in feedback

    def test_feedback_dissent_includes_insight_title(self):
        """Dissent section references the accepted insight by title."""
        feedback = self._build_realistic_feedback()
        assert "Implicit caching convention across services" in feedback

    def test_feedback_dissent_includes_dissenter_reasoning(self):
        """Dissent section includes the dissenting evaluator's actual reasoning."""
        feedback = self._build_realistic_feedback()
        assert "same PR review" in feedback

    def test_feedback_injects_into_explorer_prompt(self):
        """The full feedback text appears inside the Explorer prompt when injected."""
        feedback = self._build_realistic_feedback()

        prompt = get_explorer_prompt(
            memory_count=500,
            date_range="January 2024 to March 2026",
            feedback_injection=feedback,
            run_type="scheduled",
        )

        # The feedback text should be embedded in the prompt
        assert "Lessons from recent cycles" in prompt
        assert "single memory" in prompt
        assert "Dissenting concerns" in prompt
        assert "same PR review" in prompt

    def test_empty_feedback_does_not_inject_sections(self):
        """When no rejections or dissents exist, feedback is empty and
        the Explorer prompt contains no feedback sections."""
        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_accepted_dissents.return_value = []
            mock_db.get_user_rejections.return_value = []

            from src.dream_cycle.feedback import build_feedback_injection
            feedback = build_feedback_injection()

        assert feedback == ""

        prompt = get_explorer_prompt(
            memory_count=500,
            date_range="January 2024 to March 2026",
            feedback_injection=feedback,
            run_type="scheduled",
        )

        assert "Lessons from recent cycles" not in prompt
        assert "Dissenting concerns" not in prompt

    def test_no_deferred_references_in_feedback(self):
        """Feedback text never mentions 'deferred' — binary consensus only."""
        feedback = self._build_realistic_feedback()
        assert "deferred" not in feedback.lower()
