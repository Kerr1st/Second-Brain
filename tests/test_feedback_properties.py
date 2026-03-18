"""Property-based tests for feedback injection — Methodologist and accepted dissents.

Property 7 from the Byzantine Consensus Panel design document.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from src.dream_cycle.feedback import build_feedback_injection


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

verdict_values = st.sampled_from(["ACCEPT", "REJECT"])

reasoning_text = st.text(
    min_size=1,
    max_size=80,
    alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
)

insight_titles = st.text(
    min_size=1,
    max_size=40,
    alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
)

EVALUATOR_PREFIXES = ["evaluator_a", "evaluator_b", "evaluator_c", "evaluator_d"]


def _make_rejection_row(title, verdicts_and_reasoning):
    """Build a rejection row dict as returned by get_recent_rejections."""
    row = {
        "run_id": "run-1",
        "run_type": "scheduled",
        "completed_at": "2024-01-01",
        "candidate_id": "cand-1",
        "final_verdict": "REJECTED",
    }
    for prefix, (verdict, reasoning) in zip(EVALUATOR_PREFIXES, verdicts_and_reasoning):
        row[f"{prefix}_verdict"] = verdict
        row[f"{prefix}_reasoning"] = reasoning
    return row


def _make_accepted_dissent_row(title, verdicts_and_reasoning):
    """Build an accepted-dissent row dict as returned by get_accepted_dissents."""
    row = {
        "candidate_id": "cand-2",
        "candidate_json": {"title": title},
        "final_verdict": "ACCEPTED",
    }
    for prefix, (verdict, reasoning) in zip(EVALUATOR_PREFIXES, verdicts_and_reasoning):
        row[f"{prefix}_verdict"] = verdict
        row[f"{prefix}_reasoning"] = reasoning
    return row


# Strategy: generate 4 (verdict, reasoning) pairs where evaluator_d is REJECT
rejection_with_methodologist_reject = st.tuples(
    insight_titles,
    st.tuples(
        st.tuples(verdict_values, reasoning_text),  # evaluator_a
        st.tuples(verdict_values, reasoning_text),  # evaluator_b
        st.tuples(verdict_values, reasoning_text),  # evaluator_c
        st.tuples(st.just("REJECT"), reasoning_text),  # evaluator_d always REJECT
    ),
)

# Strategy: generate accepted dissent rows (3 ACCEPT, 1 REJECT)
# At least one evaluator must REJECT for it to be a dissent
accepted_dissent_strategy = st.tuples(
    insight_titles,
    st.tuples(
        st.tuples(verdict_values, reasoning_text),  # evaluator_a
        st.tuples(verdict_values, reasoning_text),  # evaluator_b
        st.tuples(verdict_values, reasoning_text),  # evaluator_c
        st.tuples(verdict_values, reasoning_text),  # evaluator_d
    ),
).filter(
    lambda x: any(v == "REJECT" for v, _ in x[1])
)


# ---------------------------------------------------------------------------
# Property 7: Feedback Injection Includes Methodologist and Accepted Dissents
# **Validates: Requirements 8.1, 8.2, 8.3**
# ---------------------------------------------------------------------------

class TestFeedbackInjectionMethodologistAndDissents:
    """Property 7: For any set of rejection records where evaluator_d_verdict
    is 'REJECT', build_feedback_injection() includes the Methodologist's
    rejection reasoning. Additionally, for any accepted candidate with at
    least one REJECT verdict, the feedback injection includes a
    'Dissenting concerns on accepted insights' section.

    **Validates: Requirements 8.1, 8.2, 8.3**
    """

    @given(data=rejection_with_methodologist_reject)
    @settings(max_examples=100, deadline=None)
    def test_methodologist_rejection_appears_in_feedback(self, data):
        """For any rejected candidate where evaluator_d (Methodologist) rejected,
        the feedback output includes 'Methodologist' with the rejection reasoning.

        **Validates: Requirements 8.3**
        """
        title, verdicts_tuple = data
        row = _make_rejection_row(title, verdicts_tuple)

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = [row]
            mock_db.get_accepted_dissents.return_value = []
            mock_db.get_user_rejections.return_value = []

            result = build_feedback_injection()

        assert "Methodologist" in result, (
            f"Methodologist not found in feedback output: {result!r}"
        )
        # The Methodologist's reasoning should appear
        methodologist_reasoning = verdicts_tuple[3][1]
        assert methodologist_reasoning in result, (
            f"Methodologist reasoning {methodologist_reasoning!r} not in output"
        )

    @given(data=accepted_dissent_strategy)
    @settings(max_examples=100, deadline=None)
    def test_dissenting_concerns_section_present_for_accepted_dissents(self, data):
        """For any accepted candidate with at least one REJECT verdict,
        the feedback injection includes a 'Dissenting concerns on accepted
        insights' section.

        **Validates: Requirements 8.1, 8.2**
        """
        title, verdicts_tuple = data
        row = _make_accepted_dissent_row(title, verdicts_tuple)

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_accepted_dissents.return_value = [row]
            mock_db.get_user_rejections.return_value = []

            result = build_feedback_injection()

        assert "Dissenting concerns on accepted insights" in result, (
            f"Dissenting concerns section not found in output: {result!r}"
        )

        # Each dissenting evaluator's role name should appear
        role_names = {
            "evaluator_a": "Skeptic",
            "evaluator_b": "User Advocate",
            "evaluator_c": "Epistemologist",
            "evaluator_d": "Methodologist",
        }
        for i, (prefix, role_name) in enumerate(role_names.items()):
            verdict, reasoning = verdicts_tuple[i]
            if verdict == "REJECT" and reasoning:
                assert role_name in result, (
                    f"{role_name} dissent not found in output for REJECT verdict"
                )
                assert reasoning in result, (
                    f"Dissent reasoning {reasoning!r} not found in output"
                )

    @given(data=accepted_dissent_strategy)
    @settings(max_examples=100, deadline=None)
    def test_dissent_includes_insight_title(self, data):
        """For any accepted dissent, the feedback includes the insight title.

        **Validates: Requirements 8.2**
        """
        title, verdicts_tuple = data
        row = _make_accepted_dissent_row(title, verdicts_tuple)

        with patch("src.dream_cycle.feedback.dream_cycle_db") as mock_db:
            mock_db.get_recent_rejections.return_value = []
            mock_db.get_accepted_dissents.return_value = [row]
            mock_db.get_user_rejections.return_value = []

            result = build_feedback_injection()

        assert title in result, (
            f"Insight title {title!r} not found in dissent output"
        )
