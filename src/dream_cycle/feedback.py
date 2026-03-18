"""Feedback injection — format rejection history for Explorer prompt.

Queries recent evaluator and user rejections from dream_cycle_db and
formats them into a text block for the Explorer agent's prompt.

Requirements: 6.1, 6.2
"""

from __future__ import annotations

import json

import src.dream_cycle_db as dream_cycle_db


def build_feedback_injection() -> str:
    """Query last 3 cycles' rejections and format as feedback text.

    Calls get_recent_rejections(n_cycles=3) and formats a "Lessons from
    recent cycles" text block with actual evaluator reasoning grouped by
    run/cycle. Also includes user rejections from get_user_rejections
    so the Explorer learns from both evaluator AND user feedback.

    Returns:
        Formatted feedback string, or empty string if no rejections exist.
    """
    rejections = dream_cycle_db.get_recent_rejections(n_cycles=3)
    accepted_dissents = dream_cycle_db.get_accepted_dissents(n_cycles=3)
    user_rejections = dream_cycle_db.get_user_rejections(n_cycles=3)

    if not rejections and not accepted_dissents and not user_rejections:
        return ""

    parts: list[str] = []

    evaluator_roles = {
        "evaluator_a": "Skeptic",
        "evaluator_b": "User Advocate",
        "evaluator_c": "Epistemologist",
        "evaluator_d": "Methodologist",
    }

    # Part 1: Evaluator rejections grouped by cycle
    if rejections:
        # Group rejections by run_id, preserving insertion order
        cycles: dict[str, list[dict]] = {}
        for row in rejections:
            rid = row["run_id"]
            if rid not in cycles:
                cycles[rid] = []
            cycles[rid].append(row)

        sections: list[str] = []
        for _run_id, rows in cycles.items():
            # Use completed_at from the first row (same for all rows in a cycle)
            completed_at = rows[0]["completed_at"]
            if hasattr(completed_at, "strftime"):
                cycle_date = completed_at.strftime("%Y-%m-%d")
            else:
                cycle_date = str(completed_at)

            rejected_count = sum(1 for r in rows if r["final_verdict"] == "REJECTED")

            lines: list[str] = [
                f"Cycle {cycle_date}: {rejected_count} rejected"
            ]

            for row in rows:
                for key_prefix, role_name in evaluator_roles.items():
                    verdict = row.get(f"{key_prefix}_verdict", "")
                    reasoning = row.get(f"{key_prefix}_reasoning", "")
                    if verdict == "REJECT" and reasoning:
                        lines.append(
                            f'- {role_name} rejected for: "{reasoning}"'
                        )

            sections.append("\n".join(lines))

        parts.append("\n\n".join(sections))

    # Part 2: Dissenting concerns on accepted insights
    if accepted_dissents:
        dissent_lines: list[str] = ["## Dissenting concerns on accepted insights"]
        for row in accepted_dissents:
            for key_prefix, role_name in evaluator_roles.items():
                verdict = row.get(f"{key_prefix}_verdict", "")
                reasoning = row.get(f"{key_prefix}_reasoning", "")
                if verdict == "REJECT" and reasoning:
                    candidate_json = row.get("candidate_json") or {}
                    if isinstance(candidate_json, str):
                        try:
                            candidate_json = json.loads(candidate_json)
                        except (json.JSONDecodeError, ValueError):
                            candidate_json = {}
                    title = candidate_json.get("title", "Unknown")
                    dissent_lines.append(
                        f'- {role_name} dissented on accepted "{title}": "{reasoning}"'
                    )
        if len(dissent_lines) > 1:
            parts.append("\n".join(dissent_lines))

    # Part 3: User rejections
    if user_rejections:
        user_lines: list[str] = ["## User rejections"]
        for ur in user_rejections:
            candidate_json = ur.get("candidate_json") or {}
            if isinstance(candidate_json, str):
                try:
                    candidate_json = json.loads(candidate_json)
                except (json.JSONDecodeError, ValueError):
                    candidate_json = {}
            title = candidate_json.get("title", "Unknown insight")
            reason = ur.get("user_rejection_reason") or "No reason given"
            user_lines.append(f'- "{title}": "{reason}"')
        parts.append("\n".join(user_lines))

    header = "## Lessons from recent cycles (last 3 runs)\n\n"
    return header + "\n\n".join(parts)
