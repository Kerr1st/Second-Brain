"""Digest generation — markdown rendering of dream cycle results."""
from __future__ import annotations

import logging
import psycopg2
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import src.dream_cycle_db as dream_cycle_db
from src.dream_cycle.ledger import format_attention_section, run_metrics
from src.models import CandidateInsight

logger = logging.getLogger(__name__)


def render_digest(run_id: str, accepted: list[CandidateInsight], rejected: list[CandidateInsight], *,
                  date: str, generated: int | None = None, feedback_injected: bool = False,
                  verdicts_by_title: dict | None = None, slice_rows: list | None = None,
                  cost_usd: float = 0.0, aborted: bool = False, storage_failed: int = 0) -> str:
    """Pure rendering: callers supply completion facts; no database or filesystem I/O."""
    today = date
    total = generated if generated is not None else len(accepted) + len(rejected)
    verdicts_by_title = verdicts_by_title or {}
    # Group accepted insights by strategy
    by_strategy: dict[str, list[CandidateInsight]] = {}
    for c in accepted:
        key = c.strategy_that_found_it or "unknown"
        by_strategy.setdefault(key, []).append(c)

    # Collect Explorer strategies used
    strategies_used = sorted({c.strategy_that_found_it for c in accepted if c.strategy_that_found_it})

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
    lines.append(f"- Not persisted as accepted/rejected: {max(0, total - len(accepted) - len(rejected))}")
    lines.append(f"- Storage blocked: {storage_failed}")
    lines.append(f"- Known model cost: ${cost_usd:.4f}")
    lines.append(f"- Aborted: {aborted}")
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

    if slice_rows:
        lines += format_attention_section(run_metrics(slice_rows), slice_rows)

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

    return "\n".join(lines)


DEFAULT_DIGEST_SUBDIR = "digests"


def write_digest(run_id: str, text: str, *, date: str) -> str:
    """Write a private run artifact.

    Default location is ``<repo>/logs/digests/`` -- a subdirectory this writer
    owns and creates with mode 0700 -- so the installed ``logs/`` directory
    (0755, shared with job logs) is never required to change mode. An explicit
    ``SECOND_BRAIN_DIGEST_DIR`` is honoured as given; if it is not private the
    file is still written (0600, via mkstemp) and a WARNING is logged rather than
    failing the run (review C S2: a digest refusal must never mark a fully judged
    run as aborted). A temporary file and atomic replacement keep partial output
    private and prevent a pre-existing destination symlink from redirecting the
    write.
    """
    default_dir = Path(__file__).resolve().parents[2] / "logs" / DEFAULT_DIGEST_SUBDIR
    logs_dir = Path(os.environ.get("SECOND_BRAIN_DIGEST_DIR", str(default_dir))).expanduser()
    if not logs_dir.is_absolute() or logs_dir.is_symlink():
        raise ValueError("Digest directory must be absolute and must not be a symlink")
    logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = logs_dir.stat().st_mode & 0o777
    if mode & 0o077:
        if logs_dir == default_dir:
            os.chmod(logs_dir, 0o700)  # our own directory: make it private
        else:
            logger.warning("Digest directory %s is not private (mode %o); digest file will be 0600", logs_dir, mode)
    safe_id = "".join(c for c in str(run_id) if c.isalnum() or c in "-_")[:80] or "unknown"
    safe_date = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
    file_path = logs_dir / f"dream-cycle-digest-{safe_date}-{safe_id}.md"
    fd, temporary = tempfile.mkstemp(prefix=".digest-", dir=logs_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, file_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(file_path)


def generate_digest(run_id: str, accepted: list[CandidateInsight], rejected: list[CandidateInsight], *,
                    generated: int | None = None, feedback_injected: bool | None = None,
                    cost_usd: float = 0.0, aborted: bool = False, storage_failed: int = 0) -> str:
    """Load optional presentation detail and write the pure renderer's output.

    The running pipeline supplies feedback explicitly because its completion row
    has not been written yet. Historical callers may read the completed row.
    """
    verdicts, slices = {}, []
    try:
        verdicts = dream_cycle_db.get_evaluator_verdicts_for_run(run_id)
        slices = dream_cycle_db.get_slices_for_run(run_id)
    except (psycopg2.Error, OSError, ValueError, KeyError):
        logger.warning("Digest detail unavailable", exc_info=True)
    if feedback_injected is None:
        feedback_injected = dream_cycle_db.was_feedback_injected(run_id)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = render_digest(run_id, accepted, rejected, date=date, generated=generated,
        feedback_injected=feedback_injected, verdicts_by_title=verdicts, slice_rows=slices,
        cost_usd=cost_usd, aborted=aborted, storage_failed=storage_failed)
    return write_digest(run_id, text, date=date)
