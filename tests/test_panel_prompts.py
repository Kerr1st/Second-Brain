"""Property-based tests for consensus panel evaluator prompt templates.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 4.1, 4.2**
"""

import json

import pytest
from hypothesis import given, settings, strategies as st

from src.prompts.panel import get_evaluator_prompt


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Generate plausible candidate JSON strings
candidate_json_st = st.builds(
    lambda title, content: json.dumps({"title": title, "content": content}),
    title=st.text(min_size=1, max_size=100),
    content=st.text(min_size=1, max_size=500),
)

# Generate plausible source memories content strings
source_content_st = st.text(min_size=1, max_size=500)


# ---------------------------------------------------------------------------
# Property 3: Methodologist Prompt Completeness
# **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 4.1, 4.2**
# ---------------------------------------------------------------------------


class TestMethodologistPromptCompletenessProperty:
    """For random candidate JSON and source content, assert prompt contains
    all four Methodologist criteria keywords: 'internal consistency',
    'source independence', 'reasoning structure', 'reproducibility'.
    """

    @given(candidate_json=candidate_json_st, source_content=source_content_st)
    @settings(max_examples=100)
    def test_prompt_contains_all_criteria_keywords(
        self, candidate_json: str, source_content: str
    ):
        prompt = get_evaluator_prompt(
            role="methodologist",
            candidate_json=candidate_json,
            source_memories_content=source_content,
        )
        prompt_lower = prompt.lower()

        assert "internal consistency" in prompt_lower
        assert "source independence" in prompt_lower
        assert "reasoning structure" in prompt_lower
        assert "reproducibility" in prompt_lower


# ---------------------------------------------------------------------------
# Unit tests for Methodologist prompt and error handling
# ---------------------------------------------------------------------------


class TestMethodologistPromptUnit:
    """Unit tests for Methodologist prompt generation."""

    def test_methodologist_prompt_contains_role_description(self):
        prompt = get_evaluator_prompt(
            role="methodologist",
            candidate_json='{"title": "test"}',
            source_memories_content="some memories",
        )
        assert "THE METHODOLOGIST" in prompt

    def test_methodologist_prompt_contains_candidate_json(self):
        candidate = '{"title": "my insight"}'
        prompt = get_evaluator_prompt(
            role="methodologist",
            candidate_json=candidate,
            source_memories_content="memories",
        )
        assert candidate in prompt

    def test_methodologist_prompt_contains_source_content(self):
        source = "Memory 1: important finding"
        prompt = get_evaluator_prompt(
            role="methodologist",
            candidate_json='{"title": "test"}',
            source_memories_content=source,
        )
        assert source in prompt

    def test_methodologist_prompt_contains_traceable_reasoning(self):
        """UPDATE/SUPERSEDE criterion (Req 1.5)."""
        prompt = get_evaluator_prompt(
            role="methodologist",
            candidate_json='{"title": "test"}',
            source_memories_content="memories",
        )
        assert "TRACEABLE REASONING" in prompt

    def test_invalid_role_raises_value_error_listing_four_roles(self):
        """Req 4.3: error message lists all four valid roles."""
        with pytest.raises(ValueError, match="skeptic.*advocate.*epistemologist.*methodologist"):
            get_evaluator_prompt(
                role="invalid",
                candidate_json='{"title": "test"}',
                source_memories_content="memories",
            )
