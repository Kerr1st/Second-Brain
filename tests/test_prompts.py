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
            "Scope to evidence",
            "claim_scope:",
        ]
        for section in key_sections:
            assert section in prompt, f"Missing section: {section}"

    def test_thinker_prompt_scopes_claims_to_evidence(self):
        """The Thinker prompt carries the grounding + scope rule (Decisions
        2026-09-18): claims at the scope the cited memories support, verbatim
        quotes, a claim_scope sentence, and at most 2 candidates per slice."""
        prompt = get_thinker_prompt()

        assert "## Scope to evidence" in prompt
        assert "at most 2 candidates per slice" in prompt
        assert "Quote verbatim" in prompt
        assert 'one sentence beginning "claim_scope:"' in prompt
        # The EVIDENCE bullet of the depth framework now demands verbatim quotes
        # and ends with the claim_scope sentence.
        assert (
            "- EVIDENCE: Which memories support this (IDs and brief verbatim quotes), "
            "ending with a claim_scope sentence."
        ) in prompt


    def test_thinker_prompt_restricts_mutations_to_marked_targets(self):
        """Run 9bf4ade8: two UPDATEs to captured insights were proposed and blocked.
        The prompt points the Thinker at the packet's may_update marker."""
        prompt = get_thinker_prompt()
        assert 'UPDATE and SUPERSEDE may only target a memory marked "may_update": true' in prompt
        assert "use CREATE instead" in prompt


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

        for role in ("skeptic", "advocate", "epistemologist", "methodologist"):
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


# ---------------------------------------------------------------------------
# Evaluator prompts: the acceptance Standard and the graded verdict
# (docs/PLAN-panel-acceptance.md, Decisions 2026-09-18)
# ---------------------------------------------------------------------------

EVALUATOR_ROLES = ("skeptic", "advocate", "epistemologist", "methodologist")

_CANDIDATE_JSON = '{"title": "Test Insight", "content": "WHAT: some content"}'
_SOURCES = "Source memories: ['m1']\nSource memory contents:\n- [m1] First memory content"


def _prompt_for(role: str) -> str:
    return get_evaluator_prompt(
        role=role,
        candidate_json=_CANDIDATE_JSON,
        source_memories_content=_SOURCES,
    )


class TestEvaluatorPromptStandard:
    """The graded 'standard' prompt is THE evaluator prompt for every seat."""

    @pytest.mark.parametrize("role", EVALUATOR_ROLES)
    def test_every_role_gets_the_standard(self, role):
        prompt = _prompt_for(role)

        assert "## Standard" in prompt
        assert "ACCEPT when all four hold" in prompt
        for letter in ("(a)", "(b)", "(c)", "(d)"):
            assert letter in prompt, f"{role}: Standard letter {letter} missing"
        assert "observation about the user's current week qualifies" in prompt
        assert "name the letter(s) that fail" in prompt

    @pytest.mark.parametrize("role", EVALUATOR_ROLES)
    def test_every_role_separates_blocking_from_advisory(self, role):
        prompt = _prompt_for(role)

        assert "BLOCKING defects" in prompt
        assert "ADVISORY defects" in prompt

    @pytest.mark.parametrize("role", EVALUATOR_ROLES)
    def test_every_role_gets_the_graded_verdict(self, role):
        prompt = _prompt_for(role)

        assert '"score"' in prompt
        assert '"blocking_issues"' in prompt
        assert '"advisory_issues"' in prompt
        assert '"verdict": "ACCEPT"' in prompt
        assert '"verdict": "REJECT"' in prompt
        # The old binary-only verdict section is gone.
        assert "well-grounded in 4 independent memories" not in prompt
        assert "exactly two fields" not in prompt

    @pytest.mark.parametrize("role", EVALUATOR_ROLES)
    def test_verdict_examples_are_valid_json(self, role):
        """The examples the model is asked to imitate must themselves be JSON
        (the earlier variant leaked f-string brace doubling into them)."""
        import json

        prompt = _prompt_for(role)
        examples = [line for line in prompt.splitlines() if line.startswith('{"verdict"')]

        assert len(examples) == 2
        verdicts = {json.loads(line)["verdict"] for line in examples}
        assert verdicts == {"ACCEPT", "REJECT"}
        assert "{{" not in prompt

    @pytest.mark.parametrize("role", EVALUATOR_ROLES)
    def test_section_order_standard_then_criteria_then_verdict(self, role):
        prompt = _prompt_for(role)

        assert prompt.index("## The candidate insight:") < prompt.index("## The source memories it references:")
        assert prompt.index("## The source memories it references:") < prompt.index("## Standard")
        assert prompt.index("## Standard") < prompt.index("Evaluate through")
        assert prompt.index("Evaluate through") < prompt.index("## Your verdict")
        assert "Evaluate on these criteria" not in prompt

    @pytest.mark.parametrize("role", EVALUATOR_ROLES)
    def test_role_line_names_the_seat_and_the_panel(self, role):
        prompt = _prompt_for(role)

        assert prompt.startswith("You are evaluating a candidate insight. Your role is THE ")
        assert "one of four independent evaluator roles" in prompt
        assert "verdict, score and issue lists are combined" in prompt

    def test_only_the_advocate_gets_user_context(self):
        for role in EVALUATOR_ROLES:
            has_context = "## Context about the user:" in _prompt_for(role)
            assert has_context is (role == "advocate"), role



class TestEvaluatorCriteriaWording:
    """Criteria reworded so a correctly scoped this-week observation can pass."""

    def test_skeptic_is_hard_to_fool_not_hard_to_satisfy(self):
        prompt = _prompt_for("skeptic")

        assert "hard to fool, not hard to satisfy" in prompt
        assert "Misquotation, invented detail or unestablished attribution fails (a)" in prompt
        assert "two occurrences stated as a rule are overreach" in prompt
        assert "a paraphrase of one episode cannot" in prompt
        # Pinned headings survive the rewording.
        for heading in ("FACTUAL GROUNDING", "NON-OBVIOUSNESS", "LOGICAL VALIDITY", "ACTIONABILITY"):
            assert heading in prompt
        assert "would this make the user stop and think" not in prompt

    def test_epistemologist_labels_scope_instead_of_setting_a_bar(self):
        prompt = _prompt_for("epistemologist")

        assert "no more and no less" in prompt
        assert "labels for the scope a claim may take, not a bar" in prompt
        assert "CHECKABILITY" in prompt
        assert "DURABILITY OR TIMELINESS" in prompt
        assert "a time-bound insight is not defective for being time-bound" in prompt
        assert 'do not downgrade it for being labelled "assimilation"' in prompt
        for heading in ("NOVELTY", "RETRIEVABILITY"):
            assert heading in prompt

        assert "Based on 3+ from different periods is a pattern" not in prompt
        assert "Will this still be relevant in 6 months?" not in prompt
        assert "FALSIFIABILITY" not in prompt
        assert "EVIDENCE SUFFICIENCY" not in prompt

    def test_advocate_depth_allows_one_sentence_for_a_this_week_observation(self):
        prompt = _prompt_for("advocate")

        assert "one sentence naming what the user would do or decide differently is enough depth" in prompt
        for heading in ("RELEVANCE", "TIMING", "SIGNAL-TO-NOISE", "DEPTH"):
            assert heading in prompt

    def test_methodologist_independence_scales_with_the_claim(self):
        prompt = _prompt_for("methodologist")

        assert "SOURCE INDEPENDENCE" in prompt
        assert "to the extent the claim needs" in prompt
        assert "two memories from the same conversation are the right evidence" in prompt
        assert "a memory and its own chunk counted twice" in prompt
        for heading in ("INTERNAL CONSISTENCY", "REASONING STRUCTURE", "REPRODUCIBILITY", "TRACEABLE REASONING"):
            assert heading in prompt
