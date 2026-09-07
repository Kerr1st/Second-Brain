"""Prompt documentation and Explorer feedback formatting contracts.

Actual agent invocation/payload coverage lives in test_agent_invocation.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

from src.prompts import get_explorer_prompt, get_thinker_prompt


# ---------------------------------------------------------------------------
# Thinker documented output contract
# ---------------------------------------------------------------------------

class TestThinkerPromptDocumentation:
    """Keep the documented output contract available to the agent."""

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
