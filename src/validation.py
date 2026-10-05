"""Input validation for the MCP boundary (``src/mcp_server.py``).

Pure functions — no database access, no I/O. Every check either returns the
normalised value or raises :class:`ValidationError`, whose message is short,
actionable and safe to show to an agent: it names the field and the accepted
values, never SQL text, table or constraint names.

The vocabularies below are the single source of truth for what the MCP tools
accept. Migration 016 mirrors these vocabularies with NOT VALID CHECK constraints:
new writes are checked while historical rows remain available for review.
This module provides actionable input errors before a database statement.
"""

from __future__ import annotations

import uuid
import math

#: Memory types an agent may create or assign through the MCP tools.
MEMORY_TYPES = frozenset({
    "idea", "synthesis", "research", "insight", "question",
    "decision", "priority", "project", "connection", "source", "correction_episode",
})

#: Types accepted as a *filter* by memory_search / memory_list. ``schema`` memories are
#: produced by the dream cycle (``migrations/007_schema_type.sql``): they can be read
#: and filtered on, but not created or assigned via MCP.
FILTERABLE_TYPES = MEMORY_TYPES | {"schema"}

MEMORY_STATUSES = frozenset({"active", "superseded", "archived"})

RELATION_TYPES = frozenset({
    "supports", "contradicts", "extends", "inspires",
    "derived_from", "related_to", "superseded_by",
})

# Historical corpus values plus explicit current writer channels. Source labels are
# provenance, so API capture uses its own labels rather than impersonating pullers.
SOURCE_TYPES = frozenset({
    "article", "youtube", "kiro_cli_chat", "kiro_ide_chat", "codex_task",
    "distilled_agent_task", "distilled_chat", "weekly_digest", "browser_history",
    "outlook_email", "outlook_calendar", "slack", "dream_cycle", "file", "manual",
    "quick_desktop", "quick_desktop_doc", "quick_desktop_chat", "quick_desktop_feed",
    "quick_desktop_session_event", "quick_desktop_people", "quick_desktop_terminology",
    "quick_desktop_decision", "quick_desktop_source", "quick_desktop_procedure",
    "quick_desktop_tool_strategy", "quick_desktop_preference", "quick_desktop_anti_pattern",
    "quick_desktop_profile", "api_slack", "api_email", "api_browser",
})
SEMANTIC_TYPES = (FILTERABLE_TYPES - {"source"})
MEMORY_CLASSES = frozenset({"semantic", "episodic", "procedural", "source"})

MAX_TAGS = 20
LIMIT_MIN, LIMIT_MAX = 1, 100
DAYS_MIN, DAYS_MAX = 0, 365


class ValidationError(ValueError):
    """An input the caller can fix. ``str(exc)`` is the message to show them."""


def _short(value, width: int = 60) -> str:
    """``repr`` of a rejected value, truncated so a misplaced document body never
    gets echoed back whole."""
    text = repr(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _choices(allowed) -> str:
    return ", ".join(sorted(allowed))


def uuid_or_error(value, field: str) -> str:
    """Return ``value`` as a canonical UUID string, or raise :class:`ValidationError`.

    Postgres would otherwise reject a malformed id with ``invalid input syntax for
    type uuid`` plus the full SQL statement; checking here keeps that off the wire.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise ValidationError(f"{field} is not a valid UUID: {_short(value)}") from None


def one_of(value, allowed, field: str) -> str:
    """Return ``value`` if it is exactly one of ``allowed``; raise otherwise."""
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(
            f"{field} must be one of: {_choices(allowed)} (got {_short(value)})"
        )
    return value


def optional_one_of(value, allowed, field: str) -> str | None:
    """Like :func:`one_of` for optional filters: ``None`` or an empty/blank string
    means "no filter" and comes back as ``None``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return one_of(value, allowed, field)


def require_text(value, field: str) -> str:
    """Require a string that is non-empty after stripping. Returns the original
    (unstripped) value so stored text is exactly what the caller sent."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def require_mapping(value, field: str) -> dict | None:
    """``None`` passes through; anything else must be a dict (a JSON object)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be a JSON object (got {_short(value)})")
    return value


def _as_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError(f"{field} must be an integer (got {_short(value)})")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValidationError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(f"{field} must be an integer (got {_short(value)})") from None


def clamp_limit(limit, lo: int = LIMIT_MIN, hi: int = LIMIT_MAX) -> int:
    """Coerce ``limit`` to an int inside ``[lo, hi]`` (0 → 1, 10_000 → 100)."""
    return max(lo, min(_as_int(limit, "limit"), hi))


def clamp_days(days, field: str = "since_days", lo: int = DAYS_MIN, hi: int = DAYS_MAX) -> int | None:
    """``None`` stays ``None`` (no time window); otherwise an int clamped to ``[lo, hi]``.
    ``0`` means "no window" to the callers, so negatives collapse to that."""
    if days is None:
        return None
    return max(lo, min(_as_int(days, field), hi))


def clean_tags(tags) -> list[str] | None:
    """Normalise a tag list: strip each tag, drop empties, dedupe (order kept).

    ``None`` → ``None`` (leave tags alone); ``[]`` → ``[]`` (clear them). Anything
    that is not a list of strings, or more than :data:`MAX_TAGS` distinct tags, is
    rejected rather than silently truncated.
    """
    if tags is None:
        return None
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple)):
        raise ValidationError(f"tags must be a list of strings (got {_short(tags)})")
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            raise ValidationError(f"tags must be a list of strings (got {_short(tag)})")
        tag = tag.strip()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValidationError(f"tags: at most {MAX_TAGS} tags allowed (got {len(out)})")
    return out

def clean_topics(topics: str) -> list[str]:
    """Bound work before any retrieval or model invocation."""
    require_text(topics, "topics")
    result = list(dict.fromkeys(topic.strip() for topic in topics.split(",") if topic.strip()))
    if not 1 <= len(result) <= 4:
        raise ValidationError("topics must contain between one and four distinct topics")
    if any(len(topic) > 200 for topic in result):
        raise ValidationError("Each topic must be at most 200 characters")
    return result
