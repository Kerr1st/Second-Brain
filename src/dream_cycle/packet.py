"""Memory context packets for tool-less model backends.

Agentic-CLI backends (Kiro, Claude Code, Codex) read memories themselves over
MCP, so the Thinker only receives memory ids and titles. A Direct-API backend
(Bedrock) has no tools, so the orchestrator must deliver the slice contents in
the prompt instead — the "B2 context packet" from the original Fable plan.

The same contents are cached per run so evaluators can ground a candidate
against the *actual text* of its cited source memories (the Skeptic's first
criterion), instead of a list of UUIDs.

Everything here is bounded and defensive: content is truncated per memory and
in total, only well-formed UUIDs are looked up, and a database failure degrades
to an empty packet rather than aborting the run.
"""

from __future__ import annotations

import logging
import uuid

from src.db import get_memories_by_ids

logger = logging.getLogger(__name__)

#: Per-memory content cap (chars). ~1K tokens; enough for grounding, not a dump.
MAX_CONTENT_CHARS_PER_MEMORY = 4_000
#: Whole-packet content cap (chars). ~30K tokens ≈ $0.30 of Fable input per slice.
MAX_TOTAL_CONTENT_CHARS = 120_000

TOOLLESS_NOTE = (
    "Tools are not available in this run. The full content of every memory in "
    "the slice is included above under memory_slice.memories — reason from that "
    "text only; do not attempt to search or read memories."
)

TOOLED_NOTE = (
    "The full content of every memory in the slice is included above under "
    "memory_slice.memories — do not re-read them with memory_read. Use your tools "
    "only for what the packet cannot give you: memory_search to find existing "
    "semantic memories on the same topic (CLS interleaving) or to check whether a "
    "candidate already exists, and memory_graph to follow a relationship. Budget "
    "about 6-10 tool calls; each costs ~20 seconds."
)

#: Schema for an evaluator verdict. Passed as ``schema=`` to Direct-API
#: backends so the verdict arrives as a tool call rather than JSON-in-prose:
#: in the first live Bedrock run, Sonnet 5 wrote ~3.5K tokens of markdown
#: analysis and then a JSON object whose unescaped quotes broke the text
#: backstop on 4 of 7 Methodologist calls.
EVALUATOR_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ACCEPT", "REJECT"]},
        "reasoning": {"type": "string", "description": "Full reasoning behind the verdict."},
    },
    "required": ["verdict", "reasoning"],
}


def _valid_uuids(ids) -> list[str]:
    out: list[str] = []
    for raw in ids or []:
        try:
            out.append(str(uuid.UUID(str(raw))))
        except (ValueError, AttributeError, TypeError):
            logger.debug("Skipping non-UUID memory id in packet: %r", raw)
    return out


def load_memories_by_ids(memory_ids) -> list[dict]:
    """Fetch memories for a packet. Never raises; returns [] on any failure."""
    ids = _valid_uuids(memory_ids)
    if not ids:
        return []
    try:
        return get_memories_by_ids(ids)
    except Exception:
        logger.exception("Failed to load %d memories for context packet; continuing without contents", len(ids))
        return []


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def packet_entry(memory: dict, content_cap: int = MAX_CONTENT_CHARS_PER_MEMORY) -> dict:
    """The compact, JSON-safe view of one memory carried in a packet."""
    content = memory.get("content") or ""
    truncated = len(content) > content_cap
    return {
        "id": str(memory.get("id")),
        "title": memory.get("title"),
        "type": memory.get("type"),
        "project": memory.get("project"),
        "tags": list(memory.get("tags") or []),
        "created_at": _iso(memory.get("created_at")),
        "content": content[:content_cap] + (" …[truncated]" if truncated else ""),
    }


def build_packet(memories: list[dict]) -> list[dict]:
    """Format memories for the prompt, enforcing per-memory and total caps.

    The memory that crosses the total budget is cut to what remains; every
    memory after it keeps its id/title/metadata but carries no content.
    """
    entries: list[dict] = []
    remaining = MAX_TOTAL_CONTENT_CHARS
    budget_hit = False
    for memory in memories:
        entry = packet_entry(memory)
        if budget_hit:
            entry["content"] = ""
        elif len(entry["content"]) > remaining:
            entry["content"] = entry["content"][:remaining] + " …[truncated: packet budget]"
            budget_hit = True
            logger.warning(
                "Context packet hit the %d-char total budget; later memories carry titles only.",
                MAX_TOTAL_CONTENT_CHARS,
            )
        else:
            remaining -= len(entry["content"])
        entries.append(entry)
    return entries


def format_sources_for_evaluator(cache: dict[str, dict], source_ids) -> str:
    """Render the cited source memories' text for an evaluator prompt.

    Only sources present in ``cache`` (the current run's slice contents) are
    rendered; returns "" when none are available so the existing prompt is
    unchanged on tool-capable runs.
    """
    lines: list[str] = []
    for sid in source_ids or []:
        memory = cache.get(str(sid))
        if not memory:
            continue
        entry = packet_entry(memory)
        lines.append(
            f"- [{entry['id']}] {entry['title']!s} ({entry['type']}, {entry['created_at']}):\n"
            f"  {entry['content']}"
        )
    if not lines:
        return ""
    return "Source memory contents:\n" + "\n".join(lines)
