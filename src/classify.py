"""Deterministic memory classifier.

Classifies memories as semantic, episodic, or procedural based on
type, source_type, and content signals. Pure function — no database,
network, or file system dependencies.
"""

import re

SEMANTIC_TYPES = {
    "idea", "synthesis", "insight", "decision",
    "connection", "priority", "project", "question",
}

# Matches procedural content:
#   - "step-by-step" or "step by step" (case-insensitive)
#   - "how to" (case-insensitive)
#   - Numbered instruction lists: 3+ lines starting with "N." pattern
PROCEDURAL_MARKERS = re.compile(
    r"step[- ]by[- ]step"
    r"|\bhow\s+to\b"
    r"|(?:^|\n)\s*1\.\s+.+(?:\n\s*2\.\s+.+)(?:\n\s*3\.\s+.+)",
    re.IGNORECASE,
)


def classify_memory(type: str, content: str) -> str:
    """Deterministic classification of a memory.

    Rules (evaluated in priority order):
    1. If content contains procedural markers → "procedural"
    2. If type in SEMANTIC_TYPES → "semantic"
    3. If type == "source" → "episodic"
    4. Default → "episodic"

    Returns: "semantic" | "episodic" | "procedural"
    """
    if PROCEDURAL_MARKERS.search(content):
        return "procedural"
    if type in SEMANTIC_TYPES:
        return "semantic"
    if type == "source":
        return "episodic"
    return "episodic"
