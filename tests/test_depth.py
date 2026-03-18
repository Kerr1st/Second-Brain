"""Property tests for depth scorer.

Feature: retrieval-quality, Properties 5, 6, 7

Validates that compute_depth_score() returns values in the correct range
for arbitrary inputs, produces high scores for rich content, and produces
low scores for shallow content.
"""

import re

from hypothesis import given, settings, strategies as st

from src.depth import compute_depth_score, _CAUSAL_RE, _CODE_BLOCK_RE, _QUESTIONS_RE


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Causal connector phrases to inject into rich content
_CAUSAL_PHRASES = [
    "because the system failed",
    "this means we need a fix",
    "which causes the error",
    "which leads to downtime",
    "which means the test breaks",
    "so that users can recover",
    "the fix was to restart",
]

# Strategy: pick 2+ distinct causal phrases
causal_connectors = st.lists(
    st.sampled_from(_CAUSAL_PHRASES),
    min_size=2,
    max_size=5,
    unique=True,
)

# Strategy: a fenced code block
code_block = st.just("```python\nprint('hello')\n```")

# Strategy: the questions section
questions_section = st.just("Questions this answers:\n- Why does it fail?\n- How to fix it?")

# Strategy: filler text (no depth signals)
filler_text = st.from_regex(r"[a-z ]{10,60}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 5: Depth Score Range Invariant
# ---------------------------------------------------------------------------

@given(content=st.text(min_size=0, max_size=12000))
@settings(max_examples=100)
def test_depth_score_range_invariant(content):
    """Feature: retrieval-quality, Property 5: Depth Score Range Invariant

    For any string input (empty, whitespace, unicode, long),
    compute_depth_score returns a float in [0.0, 1.0].

    **Validates: Requirements 5.1**
    """
    score = compute_depth_score(content)

    assert isinstance(score, float), (
        f"Expected float, got {type(score).__name__}"
    )
    assert 0.0 <= score <= 1.0, (
        f"Score {score} out of range [0.0, 1.0] for content length {len(content)}"
    )


# ---------------------------------------------------------------------------
# Property 6: Rich Content Produces High Depth Score
# ---------------------------------------------------------------------------

@given(
    causals=causal_connectors,
    code=code_block,
    questions=questions_section,
    filler=filler_text,
)
@settings(max_examples=100)
def test_rich_content_high_depth_score(causals, code, questions, filler):
    """Feature: retrieval-quality, Property 6: Rich Content Produces High Depth Score

    Content with 2+ causal connectors, 1+ code block, and
    "Questions this answers:" section produces a score > 0.7.

    **Validates: Requirements 5.3**
    """
    # Assemble rich content with all required signals
    content = "\n\n".join([filler] + causals + [code, questions])

    score = compute_depth_score(content)

    assert score > 0.7, (
        f"Rich content scored {score} (expected > 0.7). "
        f"Causal count: {len(causals)}, has code block, has questions section. "
        f"Content preview: {content[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Property 7: Shallow Content Produces Low Depth Score
# ---------------------------------------------------------------------------

# Characters that won't accidentally form depth signals
_SAFE_CHARS = "abcdefghijklmnopqrstuvwxyz "

# Strategy: short strings (<50 chars) from safe alphabet, filtered to exclude
# any accidental depth signal matches
shallow_content = st.from_regex(r"[a-z ]{1,49}", fullmatch=True).filter(
    lambda s: (
        not _CAUSAL_RE.search(s)
        and not _CODE_BLOCK_RE.search(s)
        and not _QUESTIONS_RE.search(s)
        and len(s) < 50
    )
)


@given(content=shallow_content)
@settings(max_examples=100)
def test_shallow_content_low_depth_score(content):
    """Feature: retrieval-quality, Property 7: Shallow Content Produces Low Depth Score

    A single short sentence (<50 chars) with no depth signals
    produces a score < 0.3.

    **Validates: Requirements 5.4**
    """
    score = compute_depth_score(content)

    assert score < 0.3, (
        f"Shallow content scored {score} (expected < 0.3). "
        f"Content: {content!r}"
    )
