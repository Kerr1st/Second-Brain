"""Numeric depth scorer for memory content.

Computes a depth score in [0.0, 1.0] measuring the explanatory depth
of a memory's content. Replaces the binary _DEPTH_RE regex check in
mcp_server.py with a multi-signal weighted score.

Pure function — no database, network, or file system dependencies.
"""

import re

# Maximum characters to analyze (bound computation for long content)
_MAX_ANALYSIS_CHARS = 10_000

# --- Signal patterns ---

# Causal connectors: explain WHY something happens
_CAUSAL_RE = re.compile(
    r"\bbecause\b"
    r"|\bwhen\b.{5,40}\bthen\b"
    r"|\bwhich\s+(?:causes|leads|means)\b"
    r"|\bso\s+that\b"
    r"|\bthe\s+fix\s+was\b"
    r"|\bthe\s+fix\b"
    r"|\bthis\s+means\b"
    r"|\bthe\s+cost\b"
    r"|\bcost\s+us\b"
    r"|\bif\s+you\s+don.t\b"
    r"|\bwithout\b.{3,30}\byou\b"
    r"|\bso\s+(?:each|every)\b"
    r"|→"
    r"|\bproducing\s+\d",
    re.IGNORECASE,
)

# Code blocks (fenced with ```)
_CODE_BLOCK_RE = re.compile(r"```")

# Specific numbers (digits that indicate concrete data, not just list markers)
_SPECIFIC_NUMBER_RE = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s*(?:%|ms|s|MB|GB|KB|bytes|requests?|errors?|times?|x))\b"
    r"|\b\d{2,}\b",
    re.IGNORECASE,
)

# Named tools/libraries (common patterns for tool/library names)
_NAMED_TOOL_RE = re.compile(
    r"\b(?:React|Django|Flask|FastAPI|Express|PostgreSQL|Redis|Docker"
    r"|Kubernetes|Terraform|AWS|Lambda|S3|DynamoDB|Webpack|Vite"
    r"|pytest|Hypothesis|numpy|pandas|scikit-learn|TensorFlow|PyTorch"
    r"|Node\.js|TypeScript|Rust|Go|Python|Java|Bedrock|pgvector"
    r"|Git|GitHub|npm|pip|cargo|Maven|Gradle)\b",
    re.IGNORECASE,
)

# "Questions this answers:" section
_QUESTIONS_RE = re.compile(r"questions\s+this\s+answers\s*:", re.IGNORECASE)

# Connection phrases (linking to other knowledge)
_CONNECTION_RE = re.compile(
    r"\b(?:extends|contradicts|relates\s+to|builds\s+on|derived\s+from"
    r"|inspired\s+by|supports|conflicts\s+with)\b",
    re.IGNORECASE,
)


def extract_questions(content: str) -> tuple[str, str]:
    """Extract the 'Questions this answers:' section from content.

    Mirrors the PL/pgSQL ``extract_questions_text()`` parser exactly:

    * Case-insensitive header match for ``Questions this answers:``
    * Inline queries after the colon on the header line are extracted
    * Subsequent lines starting with ``- `` or ``* `` are collected as
      question lines (list marker stripped)
    * Collection stops at an empty line or a line not starting with
      ``- `` / ``* ``
    * The header line is kept in *remaining_content*
    * Question list lines are removed from *remaining_content*

    Returns ``(questions_text, remaining_content)`` where
    *questions_text* is the space-joined question lines.  When no header
    is found, returns ``('', content)``.
    """
    lines = content.split("\n")
    header_found = False
    in_questions = False
    q_lines: list[str] = []
    r_lines: list[str] = []

    for line in lines:
        if not header_found and line.lower().startswith("questions this answers:"):
            header_found = True
            in_questions = True

            # Keep header line in remaining_content
            r_lines.append(line)

            # Extract inline query after the colon
            colon_pos = line.find(":")
            if colon_pos >= 0 and colon_pos < len(line) - 1:
                inline_text = line[colon_pos + 1:].strip(" \t\r\n")
                if inline_text:
                    q_lines.append(inline_text)

        elif in_questions:
            if line.startswith("- "):
                q_lines.append(line[2:])
            elif line.startswith("* "):
                q_lines.append(line[2:])
            else:
                # Empty line or non-list content terminates questions section
                in_questions = False
                r_lines.append(line)
        else:
            r_lines.append(line)

    questions_text = " ".join(q_lines)
    remaining_content = "\n".join(r_lines)
    return (questions_text, remaining_content)


def compute_depth_score(content: str) -> float:
    """Compute a numeric depth score in [0.0, 1.0].

    Signals detected:
    - Causal connectors: "because", "when...then", "which causes", "which leads",
      "which means", "so that", "the fix was", "this means"
    - Concrete examples: code blocks (```), specific numbers, named tools/libraries
    - "Questions this answers:" section
    - Content length (word count)
    - Connection phrases: "extends", "contradicts", "relates to"

    Each signal contributes a weighted sub-score. The total is clamped to [0.0, 1.0].
    """
    # Truncate to bound computation
    text = content[:_MAX_ANALYSIS_CHARS]

    score = 0.0

    # 1. Causal connectors (up to 0.35)
    #    Each match adds 0.15, capped at 0.35
    causal_count = len(_CAUSAL_RE.findall(text))
    score += min(0.35, causal_count * 0.15)

    # 2. Code blocks (up to 0.20)
    #    Count opening ``` markers; each pair is one block
    backtick_count = len(_CODE_BLOCK_RE.findall(text))
    code_block_count = backtick_count // 2
    score += min(0.20, code_block_count * 0.15)

    # 3. "Questions this answers:" section (0.25 if present)
    if _QUESTIONS_RE.search(text):
        score += 0.25

    # 4. Content length by word count (up to 0.10)
    #    Ramp from 0 at 0 words to 0.10 at 100+ words
    word_count = len(text.split())
    score += min(0.10, (word_count / 100.0) * 0.10)

    # 5. Specific numbers (up to 0.08)
    number_count = len(_SPECIFIC_NUMBER_RE.findall(text))
    score += min(0.08, number_count * 0.04)

    # 6. Named tools/libraries (up to 0.08)
    tool_count = len(set(_NAMED_TOOL_RE.findall(text)))
    score += min(0.08, tool_count * 0.04)

    # 7. Connection phrases (up to 0.08)
    connection_count = len(_CONNECTION_RE.findall(text))
    score += min(0.08, connection_count * 0.04)

    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, score))
