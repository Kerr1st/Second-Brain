"""Shared project-tag normalization for the Second Brain pipeline."""

from __future__ import annotations


def normalize_project_tag(raw: str | None) -> str | None:
    """Normalize a raw project tag value.

    Rules applied in order:
    1. None → None
    2. Non-string types → None
    3. Strip whitespace, lowercase
    4. Extract final path component (split on ``/`` and ``\\``)
    5. Empty string after normalization → None
    6. Dot-prefixed final component (e.g. ``.kiro``, ``.git``) → None
    7. If original (stripped) path is absolute (starts with ``/``) and has
       fewer than 3 components → None  (home dir / root guard).
       This rule does NOT apply to relative paths or bare directory names.

    Returns the normalized tag or *None*.
    """
    # Rule 1 & 2
    if not isinstance(raw, str):
        return None

    # Rule 3 – strip + lowercase
    stripped = raw.strip().lower()

    # Rule 5 – empty after strip
    if not stripped:
        return None

    # Rule 7 – absolute-path short-component guard (checked before we
    # extract the final component so we still have the full path).
    if stripped.startswith("/"):
        components = [c for c in stripped.split("/") if c]
        if len(components) < 3:
            return None

    # Rule 4 – extract final path component (split on both separators)
    # Replace backslash with forward slash so a single split works.
    unified = stripped.replace("\\", "/")
    parts = [p for p in unified.split("/") if p]

    if not parts:
        return None

    tag = parts[-1].strip()

    # Rule 5 again – final component could be empty after split
    if not tag:
        return None

    # Rule 6 – dot-prefixed
    if tag.startswith("."):
        return None

    return tag
