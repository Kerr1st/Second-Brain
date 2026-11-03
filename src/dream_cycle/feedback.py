"""Feedback injection — format rejection history for Explorer prompt.

Queries recent evaluator and user rejections from dream_cycle_db and
formats them into a text block for the Explorer agent's prompt.

Requirements: 6.1, 6.2
"""

from __future__ import annotations

import json

import src.dream_cycle_db as dream_cycle_db

# Bounds (see build_feedback_injection). 4,000 chars ≈ 7% of a 55K-char Explorer
# prompt, down from 91% unbounded.
FEEDBACK_CYCLES = 2
MAX_CANDIDATES_PER_CYCLE = 6
MAX_DISSENTS = 3
MAX_REASON_CHARS = 220
MAX_FEEDBACK_CHARS = 4000


def build_feedback_injection() -> str:
    """Query recent cycles' rejections and format a *bounded* lessons block.

    Calls get_recent_rejections(n_cycles=FEEDBACK_CYCLES) and formats a
    "Lessons from recent cycles" text block with evaluator reasoning grouped by
    run/cycle, plus dissents on accepted insights and user rejections.

    Bounded on purpose. On 2026-09-17 the unbounded version reached 83K chars
    (91% of the Explorer's system prompt), consisting entirely of the panel's
    reasoning about one project; the Explorer then spent every slice on that
    project. Each reasoning is cut to its first sentence, at most
    MAX_CANDIDATES_PER_CYCLE candidates per cycle are kept, and the whole block
    is trimmed oldest-cycle-first to MAX_FEEDBACK_CHARS. The header tells the
    Explorer these are lessons about *how slices fail*, not topics to revisit.

    Returns:
        Formatted feedback string, or empty string if no rejections exist.
    """
    rejections = dream_cycle_db.get_recent_rejections(n_cycles=FEEDBACK_CYCLES)
    accepted_dissents = dream_cycle_db.get_accepted_dissents(n_cycles=FEEDBACK_CYCLES)
    user_rejections = dream_cycle_db.get_user_rejections(n_cycles=FEEDBACK_CYCLES)

    if not rejections and not accepted_dissents and not user_rejections:
        return ""

    parts: list[str] = []

    evaluator_roles = {
        "evaluator_a": "Skeptic",
        "evaluator_b": "User Advocate",
        "evaluator_c": "Epistemologist",
        "evaluator_d": "Methodologist",
    }

    # Part 1: Evaluator rejections grouped by cycle (newest cycle first so trimming
    # drops the oldest).
    cycle_sections: list[str] = []
    if rejections:
        cycles: dict[str, list[dict]] = {}
        for row in rejections:
            cycles.setdefault(row["run_id"], []).append(row)

        for _run_id, rows in cycles.items():
            completed_at = rows[0]["completed_at"]
            cycle_date = completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, "strftime") else str(completed_at)
            rejected_rows = [r for r in rows if r["final_verdict"] == "REJECTED"]
            lines: list[str] = [f"Cycle {cycle_date}: {len(rejected_rows)} rejected"]
            for row in rejected_rows[:MAX_CANDIDATES_PER_CYCLE]:
                title = _candidate_title(row)
                for key_prefix, role_name in evaluator_roles.items():
                    verdict = row.get(f"{key_prefix}_verdict", "")
                    reasoning = row.get(f"{key_prefix}_reasoning", "")
                    if verdict == "REJECT" and reasoning:
                        lines.append(f'- {role_name} rejected{title} for: "{_first_sentence(reasoning)}"')
            if len(rejected_rows) > MAX_CANDIDATES_PER_CYCLE:
                lines.append(f"- (+{len(rejected_rows) - MAX_CANDIDATES_PER_CYCLE} more rejected candidates omitted)")
            cycle_sections.append("\n".join(lines))
        parts.append("\n\n".join(cycle_sections))

    # Part 2: Dissenting concerns on accepted insights
    if accepted_dissents:
        dissent_lines: list[str] = ["## Dissenting concerns on accepted insights"]
        for row in accepted_dissents[:MAX_DISSENTS]:
            for key_prefix, role_name in evaluator_roles.items():
                verdict = row.get(f"{key_prefix}_verdict", "")
                reasoning = row.get(f"{key_prefix}_reasoning", "")
                if verdict == "REJECT" and reasoning:
                    title = _candidate_json(row).get("title", "Unknown")
                    dissent_lines.append(
                        f'- {role_name} dissented on accepted "{title}": "{_first_sentence(reasoning)}"'
                    )
        if len(dissent_lines) > 1:
            parts.append("\n".join(dissent_lines))

    # Part 3: User rejections — the strongest signal there is; never trimmed.
    if user_rejections:
        user_lines: list[str] = ["## User rejections"]
        for ur in user_rejections:
            title = _candidate_json(ur).get("title", "Unknown insight")
            reason = ur.get("user_rejection_reason") or "No reason given"
            user_lines.append(f'- "{title}": "{_first_sentence(reason)}"')
        parts.append("\n".join(user_lines))

    header = (
        f"## Lessons from recent cycles (last {FEEDBACK_CYCLES} runs)\n\n"
        "These describe HOW candidates failed evaluation — most often evidence that was "
        "not independent (several syntheses of the same conversation), claims stronger than "
        "the sources support, or sources not actually supplied. Use them to compose better "
        "slices: independent episodes, more than one channel. They are NOT topics to revisit.\n\n"
    )
    body = _trim_to_budget(parts, cycle_sections, MAX_FEEDBACK_CHARS - len(header))
    return header + body


def _trim_to_budget(parts: list[str], cycle_sections: list[str], budget: int) -> str:
    """Drop the oldest cycles first until the block fits the character budget."""
    body = "\n\n".join(parts)
    sections = list(cycle_sections)
    while len(body) > budget and len(sections) > 1:
        sections.pop()  # oldest cycle (rejections are returned newest first)
        parts = ["\n\n".join(sections)] + parts[1:]
        body = "\n\n".join(parts)
    if len(body) > budget:
        body = body[: max(0, budget - 15)].rstrip() + "\n(trimmed)"
    return body


def _first_sentence(text: str, limit: int = MAX_REASON_CHARS) -> str:
    text = " ".join(str(text).split())
    end = len(text)
    for sep in (". ", "! ", "? "):
        idx = text.find(sep)
        if 0 < idx < end:
            end = idx + 1
    sentence = text[:end].strip()
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def _candidate_json(row: dict) -> dict:
    cj = row.get("candidate_json") or {}
    if isinstance(cj, str):
        try:
            cj = json.loads(cj)
        except (json.JSONDecodeError, ValueError):
            cj = {}
    return cj if isinstance(cj, dict) else {}


def _candidate_title(row: dict) -> str:
    title = _candidate_json(row).get("title")
    return f' "{str(title)[:80]}"' if title else ""
