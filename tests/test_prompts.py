"""Property-based and unit tests for prompt template interpolation.

**Validates: Requirements 17.2, 17.4, 17.5**
- THE Explorer prompt template SHALL accept memory_count, date_range,
  feedback_injection, run_type, and optional scope as interpolation variables.
- THE evaluator prompt templates SHALL accept the evaluator role, candidate JSON,
  and source memories content as interpolation variables.
- THE Prompt module SHALL support scoped prompt variants for different execution
  modes (session_start restricts strategies, post_learn restricts scope).
"""

import pytest
from hypothesis import given, strategies as st

from src.prompts import get_explorer_prompt, get_thinker_prompt, get_evaluator_prompt


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

positive_ints = st.integers(min_value=1, max_value=10_000_000)

non_empty_strings = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=100,
)

feedback_strings = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=200,
)

run_types = st.sampled_from(["scheduled", "post_learn", "session_start", "user_triggered"])


# ---------------------------------------------------------------------------
# Property 15: Prompt Template Interpolation
# **Validates: Requirements 17.2, 17.4, 17.5**
# ---------------------------------------------------------------------------

class TestExplorerPromptProperty:
    """Property-based tests for Explorer prompt interpolation."""

    @given(
        memory_count=positive_ints,
        date_range=non_empty_strings,
        feedback_injection=feedback_strings,
        run_type=run_types,
    )
    def test_explorer_prompt_contains_interpolated_values(
        self, memory_count, date_range, feedback_injection, run_type
    ):
        """For any valid interpolation variables, get_explorer_prompt returns a
        non-empty string containing the memory_count and date_range values.

        **Validates: Requirements 17.2**
        """
        prompt = get_explorer_prompt(
            memory_count=memory_count,
            date_range=date_range,
            feedback_injection=feedback_injection,
            run_type=run_type,
        )

        assert isinstance(prompt, str)
        assert len(prompt) > 0
        assert str(memory_count) in prompt
        assert date_range in prompt

    @given(
        memory_count=positive_ints,
        date_range=non_empty_strings,
        feedback_injection=feedback_strings,
    )
    def test_session_start_restricts_strategies(self, memory_count, date_range, feedback_injection):
        """For session_start mode, the prompt contains strategies 6, 8, 10
        (PATTERN EMERGENCE, STALE SYNTHESIS CHECK, DESIRABLE DIFFICULTY)
        but NOT strategies like TEMPORAL JUXTAPOSITION, ORPHAN ARCHAEOLOGY,
        CROSS-PROJECT COLLISION.

        **Validates: Requirements 17.5**
        """
        prompt = get_explorer_prompt(
            memory_count=memory_count,
            date_range=date_range,
            feedback_injection=feedback_injection,
            run_type="session_start",
        )

        # Allowed strategies present
        assert "PATTERN EMERGENCE" in prompt
        assert "STALE SYNTHESIS CHECK" in prompt
        assert "DESIRABLE DIFFICULTY" in prompt

        # Disallowed strategies absent
        assert "TEMPORAL JUXTAPOSITION" not in prompt
        assert "ORPHAN ARCHAEOLOGY" not in prompt
        assert "CROSS-PROJECT COLLISION" not in prompt


# ---------------------------------------------------------------------------
# Explicit tests for Explorer prompt
# ---------------------------------------------------------------------------

class TestExplorerPromptExplicit:
    """Explicit unit tests for Explorer prompt variants."""

    def test_scheduled_mode_includes_all_10_strategies(self):
        """Scheduled mode includes all 10 strategies.

        **Validates: Requirements 17.5**
        """
        prompt = get_explorer_prompt(
            memory_count=1000,
            date_range="2024-01-01 to 2026-03-01",
            feedback_injection="",
            run_type="scheduled",
        )

        strategies = [
            "TEMPORAL JUXTAPOSITION",
            "CROSS-PROJECT COLLISION",
            "ORPHAN ARCHAEOLOGY",
            "QUESTION-ANSWER BRIDGING",
            "CONTRADICTION HUNTING",
            "PATTERN EMERGENCE",
            "DEPTH GRADIENT",
            "STALE SYNTHESIS CHECK",
            "RETRIEVAL FAILURE ANALYSIS",
            "DESIRABLE DIFFICULTY",
        ]
        for strategy in strategies:
            assert strategy in prompt, f"Missing strategy: {strategy}"

    def test_post_learn_with_scope_includes_scope_details(self):
        """Post-learn with scope includes scope details in the prompt.

        **Validates: Requirements 17.5**
        """
        scope = {"details": "New insights about caching patterns from session"}
        prompt = get_explorer_prompt(
            memory_count=500,
            date_range="2025-01-01 to 2026-03-01",
            feedback_injection="",
            run_type="post_learn",
            scope=scope,
        )

        assert "New insights about caching patterns from session" in prompt
        assert "Post-Learn Reflection" in prompt

    def test_user_triggered_with_topic_includes_topic(self):
        """User-triggered with topic includes the topic in the prompt.

        **Validates: Requirements 17.5**
        """
        scope = {"topic": "database migration patterns"}
        prompt = get_explorer_prompt(
            memory_count=800,
            date_range="2024-06-01 to 2026-03-01",
            feedback_injection="",
            run_type="user_triggered",
            scope=scope,
        )

        assert "database migration patterns" in prompt
        assert "User-Triggered Deep Dive" in prompt

    def test_feedback_injection_appears_when_nonempty(self):
        """Feedback injection text appears in the prompt when non-empty.

        **Validates: Requirements 17.2**
        """
        feedback = "## Lessons from recent cycles\n\nSkeptic rejected for weak evidence."
        prompt = get_explorer_prompt(
            memory_count=1000,
            date_range="2024-01-01 to 2026-03-01",
            feedback_injection=feedback,
            run_type="scheduled",
        )

        assert "Lessons from recent cycles" in prompt
        assert "Skeptic rejected for weak evidence" in prompt

    def test_empty_feedback_injection_no_extra_content(self):
        """Empty feedback injection doesn't add extra feedback content.

        **Validates: Requirements 17.2**
        """
        prompt = get_explorer_prompt(
            memory_count=1000,
            date_range="2024-01-01 to 2026-03-01",
            feedback_injection="",
            run_type="scheduled",
        )

        assert "Lessons from recent cycles" not in prompt


# ---------------------------------------------------------------------------
# Explicit tests for Thinker prompt
# ---------------------------------------------------------------------------

class TestThinkerPromptExplicit:
    """Explicit unit tests for the static Thinker prompt."""

    def test_thinker_prompt_is_static_and_contains_key_sections(self):
        """Thinker prompt is static and contains key sections.

        **Validates: Requirements 17.3**
        """
        prompt = get_thinker_prompt()

        assert isinstance(prompt, str)
        assert len(prompt) > 0

        key_sections = [
            "UNNAMED PRINCIPLES",
            "CONTRADICTIONS",
            "RESOLVED QUESTIONS",
            "EMERGING PATTERNS",
            "KNOWLEDGE GAPS",
            "STALE KNOWLEDGE",
            "META-COGNITIVE REFLECTION",
            "CLS INTERLEAVING",
            "DISTILLATION",
        ]
        for section in key_sections:
            assert section in prompt, f"Missing section: {section}"


# ---------------------------------------------------------------------------
# Explicit tests for Evaluator prompts
# ---------------------------------------------------------------------------

class TestEvaluatorPromptExplicit:
    """Explicit unit tests for evaluator prompt interpolation."""

    def test_evaluator_prompts_contain_role_candidate_and_sources(self):
        """Evaluator prompts contain the role, candidate JSON, and source memories.

        **Validates: Requirements 17.4**
        """
        candidate_json = '{"title": "Test Insight", "content": "Some content"}'
        source_memories = "Memory 1: First memory content\nMemory 2: Second memory content"

        for role in ("skeptic", "advocate", "epistemologist"):
            prompt = get_evaluator_prompt(
                role=role,
                candidate_json=candidate_json,
                source_memories_content=source_memories,
            )

            assert isinstance(prompt, str)
            assert len(prompt) > 0
            assert candidate_json in prompt
            assert source_memories in prompt

    def test_invalid_evaluator_role_raises_value_error(self):
        """Invalid evaluator role raises ValueError.

        **Validates: Requirements 17.4**
        """
        with pytest.raises(ValueError, match="Invalid evaluator role"):
            get_evaluator_prompt(
                role="invalid_role",
                candidate_json="{}",
                source_memories_content="",
            )
