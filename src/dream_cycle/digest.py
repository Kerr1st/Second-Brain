"""Digest generation — markdown rendering of dream cycle results."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import src.dream_cycle_db as dream_cycle_db
from src.models import CandidateInsight

logger = logging.getLogger(__name__)


def generate_digest(
    run_id: str,
    accepted: list[CandidateInsight],
    rejected: list[CandidateInsight],
) -> str:
    """Generate a static markdown digest of the dream cycle run.

    Writes a markdown file to ``logs/dream-cycle-digest-{date}.md`` with
    accepted insights grouped by strategy type, run statistics, Explorer
    strategies used, and a feedback loop summary.

    Calls dream_cycle_db.get_evaluator_verdicts_for_run() and
    dream_cycle_db.was_feedback_injected() instead of inline SQL.

    Args:
        run_id: The dream cycle run UUID.
        accepted: Candidates that passed BFT consensus (≥3/4 ACCEPT).
        rejected: Candidates that were rejected (≤2/4 ACCEPT).

    Returns:
        The file path of the written digest markdown file.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = len(accepted) + len(rejected)

    # Query evaluator verdicts for accepted candidates from the DB
    verdicts_by_title: dict[str, dict] = {}
    try:
        verdicts_by_title = dream_cycle_db.get_evaluator_verdicts_for_run(run_id)
    except Exception:
        logger.warning("Could not query evaluator verdicts for digest, continuing without them")

    # Group accepted insights by strategy
    by_strategy: dict[str, list[CandidateInsight]] = {}
    for c in accepted:
        key = c.strategy_that_found_it or "unknown"
        by_strategy.setdefault(key, []).append(c)

    # Collect Explorer strategies used
    strategies_used = sorted({c.strategy_that_found_it for c in accepted if c.strategy_that_found_it})

    # Check if feedback was injected
    feedback_injected = False
    try:
        feedback_injected = dream_cycle_db.was_feedback_injected(run_id)
    except Exception:
        pass

    # Build markdown
    lines: list[str] = []
    lines.append(f"# Dream Cycle Digest — {today}")
    lines.append("")
    lines.append(f"Run ID: `{run_id}`")
    lines.append("")

    # Run statistics
    lines.append("## Run Statistics")
    lines.append("")
    lines.append(f"- Candidates generated: {total}")
    lines.append(f"- Accepted: {len(accepted)}")
    lines.append(f"- Rejected: {len(rejected)}")
    lines.append("")

    # Explorer strategies
    lines.append("## Explorer Strategies Used")
    lines.append("")
    if strategies_used:
        for s in strategies_used:
            lines.append(f"- {s}")
    else:
        lines.append("- (none)")
    lines.append("")

    # Feedback loop summary
    lines.append("## Feedback Loop Summary")
    lines.append("")
    if feedback_injected:
        lines.append("Feedback from previous cycles was injected into the Explorer prompt.")
    else:
        lines.append("No feedback was injected (no prior rejections or first run).")
    lines.append("")

    # Accepted insights grouped by strategy
    lines.append("## Accepted Insights")
    lines.append("")
    if not accepted:
        lines.append("No insights were accepted in this run.")
        lines.append("")
    else:
        for strategy in sorted(by_strategy.keys()):
            candidates = by_strategy[strategy]
            lines.append(f"### Strategy: {strategy}")
            lines.append("")
            for c in candidates:
                lines.append(f"#### {c.title}")
                lines.append("")

                # Operation type note for UPDATE/SUPERSEDE
                if c.operation in ("UPDATE", "SUPERSEDE"):
                    target = c.target_memory_id or "unknown"
                    lines.append(f"**Operation**: {c.operation} (target memory: `{target}`)")
                    lines.append("")

                # Full content
                lines.append(c.content)
                lines.append("")

                # Source memory IDs
                if c.source_memories:
                    lines.append("**Source memories**: " + ", ".join(f"`{m}`" for m in c.source_memories))
                else:
                    lines.append("**Source memories**: (none)")
                lines.append("")

                # Evaluator reasoning
                v = verdicts_by_title.get(c.title)
                if v:
                    lines.append("**Evaluator reasoning**:")
                    lines.append("")
                    for role in ("skeptic", "advocate", "epistemologist", "methodologist"):
                        rv = v.get(role, {})
                        verdict = rv.get("verdict", "N/A")
                        reasoning = rv.get("reasoning", "")
                        lines.append(f"- **{role.capitalize()}** ({verdict}): {reasoning}")
                    lines.append("")

                    # Acceptance annotation
                    accept_count = sum(
                        1 for role in ("skeptic", "advocate", "epistemologist", "methodologist")
                        if v.get(role, {}).get("verdict") == "ACCEPT"
                    )
                    if accept_count == 4:
                        lines.append("**Accepted (4/4 — unanimous)**")
                    elif accept_count == 3:
                        dissenter = next(
                            role for role in ("skeptic", "advocate", "epistemologist", "methodologist")
                            if v.get(role, {}).get("verdict") != "ACCEPT"
                        )
                        dissent_reasoning = v.get(dissenter, {}).get("reasoning", "")
                        lines.append(f"**Accepted (3/4) — {dissenter.capitalize()} dissented: {dissent_reasoning}**")
                    lines.append("")

                lines.append("---")
                lines.append("")

    # Write file. Resolve logs/ relative to the repo root (not cwd): scheduled
    # launchd runs have cwd=/ (read-only), which broke the previous Path("logs").
    # Built as a single Path() call over an os.path string so it stays both
    # cwd-independent and simple to mock in tests.
    logs_dir = Path(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "logs")))
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_path = logs_dir / f"dream-cycle-digest-{today}.md"
    file_path.write_text("\n".join(lines), encoding="utf-8")

    logger.info("Digest written to %s", file_path)
    return str(file_path)
