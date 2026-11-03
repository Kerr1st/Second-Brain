"""Property-based tests for digest dissent annotation.

Rendering preserves all four votes and any dissent.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from src.dream_cycle.digest import render_digest
from src.models import CandidateInsight


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

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

ROLES = ("skeptic", "advocate", "epistemologist", "methodologist")


def _make_candidate(title: str) -> CandidateInsight:
    """Build a minimal CandidateInsight for testing."""
    return CandidateInsight(
        title=title,
        type="insight",
        operation="CREATE",
        content="Test content",
        strategy_that_found_it="test_strategy",
    )


def _make_verdicts_unanimous(title: str, reasonings: tuple[str, str, str, str]) -> dict:
    """Build a verdicts_by_title entry where all 4 evaluators ACCEPT."""
    return {
        title: {
            role: {"verdict": "ACCEPT", "reasoning": r}
            for role, r in zip(ROLES, reasonings)
        }
    }


def _make_verdicts_3_of_4(
    title: str,
    dissenter_idx: int,
    reasonings: tuple[str, str, str, str],
) -> dict:
    """Build a verdicts_by_title entry where 3/4 ACCEPT and 1 dissents."""
    verdicts = {}
    for i, role in enumerate(ROLES):
        if i == dissenter_idx:
            verdicts[role] = {"verdict": "REJECT", "reasoning": reasonings[i]}
        else:
            verdicts[role] = {"verdict": "ACCEPT", "reasoning": reasonings[i]}
    return {title: verdicts}


# Strategy: generate unanimous (4/4) case
unanimous_strategy = st.tuples(
    insight_titles,
    st.tuples(reasoning_text, reasoning_text, reasoning_text, reasoning_text),
)

# Strategy: generate 3/4 case with one dissenter
three_of_four_strategy = st.tuples(
    insight_titles,
    st.integers(min_value=0, max_value=3),  # dissenter index
    st.tuples(reasoning_text, reasoning_text, reasoning_text, reasoning_text),
)


# ---------------------------------------------------------------------------
# Property 8: Digest Annotates Non-Unanimous Accepts
# **Validates: Requirements 7.1, 7.2, 7.3**
# ---------------------------------------------------------------------------

class TestDigestAnnotatesNonUnanimousAccepts:
    """Property 8: For any accepted candidate with 3/4 verdicts (one dissenter),
    render_digest() includes 'Accepted (3/4)' with the dissenter's role and
    reasoning. For any accepted candidate with 4/4 verdicts, the digest includes
    'Accepted (4/4 — unanimous)'.

    **Validates: Requirements 7.1, 7.2, 7.3**
    """

    @given(data=unanimous_strategy)
    @settings(max_examples=100, deadline=None)
    def test_unanimous_accept_annotated_4_of_4(self, data):
        """For any accepted candidate with 4/4 ACCEPT verdicts, the digest
        includes 'Accepted (4/4 — unanimous)'.

        **Validates: Requirements 7.2**
        """
        title, reasonings = data
        candidate = _make_candidate(title)
        verdicts = _make_verdicts_unanimous(title, reasonings)

        digest_text = render_digest("test-run", [candidate], [], date="2026-09-26",
                                    verdicts_by_title=verdicts)
        assert "Accepted (4/4 — unanimous)" in digest_text, (
            f"'Accepted (4/4 — unanimous)' not found in digest for unanimous accept"
        )

    @given(data=three_of_four_strategy)
    @settings(max_examples=100, deadline=None)
    def test_non_unanimous_accept_annotated_3_of_4_with_dissenter(self, data):
        """For any accepted candidate with 3/4 ACCEPT verdicts, the digest
        includes 'Accepted (3/4)' with the dissenter's role and reasoning.

        **Validates: Requirements 7.1, 7.3**
        """
        title, dissenter_idx, reasonings = data
        candidate = _make_candidate(title)
        verdicts = _make_verdicts_3_of_4(title, dissenter_idx, reasonings)

        dissenter_role = ROLES[dissenter_idx]
        dissent_reasoning = reasonings[dissenter_idx]

        digest_text = render_digest("test-run", [candidate], [], date="2026-09-26",
                                    verdicts_by_title=verdicts)
        assert "Accepted (3/4)" in digest_text, (
            f"'Accepted (3/4)' not found in digest for 3/4 accept"
        )
        assert dissenter_role.capitalize() in digest_text, (
            f"Dissenter role '{dissenter_role.capitalize()}' not found in digest"
        )
        assert dissent_reasoning in digest_text, (
            f"Dissent reasoning not found in digest"
        )

    @given(data=unanimous_strategy)
    @settings(max_examples=100, deadline=None)
    def test_methodologist_in_evaluator_role_loop(self, data):
        """For any accepted candidate, the digest includes 'Methodologist'
        in the evaluator reasoning section.

        **Validates: Requirements 7.3**
        """
        title, reasonings = data
        candidate = _make_candidate(title)
        verdicts = _make_verdicts_unanimous(title, reasonings)

        digest_text = render_digest("test-run", [candidate], [], date="2026-09-26",
                                    verdicts_by_title=verdicts)
        assert "Methodologist" in digest_text, (
            f"'Methodologist' not found in evaluator reasoning section"
        )
