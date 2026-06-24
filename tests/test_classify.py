"""Property tests for memory classifier correctness.

Feature: retrieval-quality, Property 4: Classifier Correctness

Validates that classify_memory() returns the correct classification
according to the priority rules for any combination of type and content.
"""

import re

from hypothesis import given, settings, strategies as st

from src.classify import PROCEDURAL_MARKERS, SEMANTIC_TYPES, classify_memory


# --- Strategies ---

semantic_types = st.sampled_from(sorted(SEMANTIC_TYPES))
non_semantic_types = st.text(min_size=1, max_size=20).filter(
    lambda t: t not in SEMANTIC_TYPES and t != "source"
)
all_types = st.one_of(
    semantic_types,
    st.just("source"),
    non_semantic_types,
)

# Content that definitely contains procedural markers
procedural_content = st.one_of(
    st.just("Here is a step-by-step guide to do it"),
    st.just("step by step instructions for setup"),
    st.just("Learn how to build a web app"),
    st.just("1. First do this\n2. Then do that\n3. Finally finish"),
    # Dynamically build numbered lists with 3+ items
    st.integers(min_value=3, max_value=8).flatmap(
        lambda n: st.just(
            "\n".join(f"{i}. Do step {i}" for i in range(1, n + 1))
        )
    ),
)

# Content that definitely does NOT contain procedural markers
safe_content = st.from_regex(r"[a-z ]{0,80}", fullmatch=True).filter(
    lambda c: not PROCEDURAL_MARKERS.search(c)
)


@given(
    mem_type=all_types,
    content=st.one_of(procedural_content, safe_content),
)
@settings(max_examples=100)
def test_classifier_correctness(mem_type, content):
    """Feature: retrieval-quality, Property 4: Classifier Correctness

    For any (type, content), classify_memory returns the correct
    class per priority rules:
      1. Content with procedural markers → "procedural" (regardless of type)
      2. type in SEMANTIC_TYPES → "semantic"
      3. type == "source" → "episodic"
      4. Default → "episodic"

    **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
    """
    result = classify_memory(mem_type, content)

    # Independently compute expected classification using the same priority rules
    has_procedural = bool(PROCEDURAL_MARKERS.search(content))

    if has_procedural:
        expected = "procedural"
    elif mem_type in SEMANTIC_TYPES:
        expected = "semantic"
    elif mem_type == "source":
        expected = "episodic"
    else:
        expected = "episodic"

    assert result == expected, (
        f"classify_memory({mem_type!r}, {content!r}) "
        f"returned {result!r}, expected {expected!r}"
    )
