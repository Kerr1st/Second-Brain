"""Agents Explorer responsibilities."""
from __future__ import annotations
import logging
import psycopg2
from datetime import datetime
from src.models import MemorySlice
from src.prompts import get_explorer_prompt
from src.dream_cycle import allocation, ledger
from src.dream_cycle.budget import *
logger = logging.getLogger(__name__)

def invoke_explorer(
    self, feedback: str, run_type: str, scope: dict | None = None,
    stats: dict | None = None,
) -> list[MemorySlice]:
    """Invoke Explorer agent and parse memory slices.

    Builds the Explorer prompt, invokes via AgentInvoker with MCP config,
    and parses the JSON output into MemorySlice objects.

    Args:
        feedback: Formatted feedback injection text (may be empty).
        run_type: One of scheduled, post_learn, session_start, user_triggered.
        scope: Optional scoping dict for non-scheduled runs.
        stats: Optional pre-fetched memory stats (avoids redundant DB call).

    Returns:
        List of MemorySlice objects (may be empty for circuit breaker).
    """
    if stats is None:
        stats = self.db.get_memory_stats()
    date_range = stats["date_range"]

    # Build human-readable date range string
    date_min = date_range.get("min")
    date_max = date_range.get("max")
    if date_min and date_max:
        date_range_str = f"{date_min:%B %Y} to {date_max:%B %Y}"
    else:
        date_range_str = "unknown"

    # Build Explorer prompt
    prompt = get_explorer_prompt(
        memory_count=stats["total_count"],
        date_range=date_range_str,
        feedback_injection=feedback,
        run_type=run_type,
        scope=scope,
        strategy_usage=self.db.get_strategy_usage(n_cycles=10),
    )

    # Arrivals block: what was captured since the previous run, by channel, with
    # ids. Without this the Explorer knew only "N new memories" and had no reason
    # to look at any of them. With skim records for the window (Step 3.5) the
    # block is the full inventory — one line per document — instead of the
    # 4-samples-per-channel manifest (docs/SKIM-STAGE.md, Phase 3).
    manifest = ""
    self._explorer_input = {}
    try:
        window_start = ledger.arrival_window_start(self._current_run_id, run_type,
                                                   self._current_started_at, dcdb=self.db)
        arrivals = self.db.get_arrivals_summary(window_start, allocation.MANIFEST_SAMPLES_PER_SOURCE)
        arrivals = arrivals if isinstance(arrivals, list) else []
        if self._skim_records:
            counts = {a["source_type"]: int(a["documents"]) for a in arrivals
                      if isinstance(a, dict) and a.get("source_type") and a.get("documents")}
            render = allocation.render_arrivals_inventory(window_start, self._skim_records,
                                                          counts_by_channel=counts)
            manifest = render.text
            self._explorer_input = {"explorer_input": "inventory", "inventory_listed": render.listed,
                                    "inventory_docs": render.total, "inventory_chars": len(manifest)}
            logger.info("Explorer arrivals input: inventory — %d of %d skimmed documents listed "
                        "(%d lines, %d chars)", render.listed, render.total, manifest.count("\n") + 1, len(manifest))
        else:
            manifest = allocation.build_arrivals_manifest(window_start, arrivals)
            self._explorer_input = {"explorer_input": "manifest"}
            logger.info("Explorer arrivals input: manifest — %d channels, %d sample ids per channel "
                        "(%d lines, %d chars); no skim records for this window",
                        len(arrivals), allocation.MANIFEST_SAMPLES_PER_SOURCE, manifest.count("\n") + 1,
                        len(manifest))
    except (psycopg2.Error, OSError, ValueError, KeyError):
        logger.warning("Arrivals manifest unavailable for this run", exc_info=True)

    user_message = (
        f"Run type: {run_type}. "
        f"Memory count: {stats['total_count']}. "
        f"Recent activity (7d): {stats['recent_activity']} new memories. "
        "Please assemble 0-5 memory slices using diverse strategies and meet the slice budget."
        + (f"\n\n{manifest}" if manifest else "")
    )

    logger.info("Invoking Explorer agent (run_type=%s)", run_type)
    result = self._invoke_metered("explorer",
        system_prompt=prompt,
        user_message=user_message,
        tools=True,
        timeout=EXPLORER_TIMEOUT_S,
        effort=self.resolver.spec_for("explorer").effort,
        stage="explorer",
        run_id=self._current_run_id,
    )

    self._capture("explorer", inputs={"system_prompt": prompt, "user_message": user_message}, output=result.get("output"))

    # Parse output into MemorySlice objects
    raw_slices = result["output"]
    if not isinstance(raw_slices, list):
        logger.warning("Explorer output is not a list, wrapping: %s", type(raw_slices))
        raw_slices = [raw_slices] if raw_slices else []

    slices = []
    for raw in raw_slices:
        try:
            if not isinstance(raw, dict):
                raise ValueError("Explorer slice must be an object")
            slice_obj = MemorySlice(
                name=raw.get("name", ""),
                strategy=raw.get("strategy", ""),
                memory_ids=raw.get("memory_ids", []),
                memory_titles=raw.get("memory_titles", []),
                hypothesis=raw.get("hypothesis", ""),
            )
            slices.append(slice_obj)
            logger.debug(
                "Parsed slice: name=%s, strategy=%s, memories=%d",
                slice_obj.name,
                slice_obj.strategy,
                len(slice_obj.memory_ids),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse Explorer slice: %s", type(exc).__name__)

    if not slices:
        logger.warning("Explorer produced no usable slices (raw_candidates=%d)", len(raw_slices))
    return slices


def _allocate_attention(self, run_id: str, run_type: str, started_at: datetime,
                        slices: list[MemorySlice]) -> list[MemorySlice]:
    """Enforce the slice budget, add the guaranteed arrivals slice, persist the ledger.

    Expected database, I/O and malformed-data failures retain the Explorer slices.
    Programming errors propagate.
    Scoped runs (post_learn, user_triggered) are exempt from the budget — their
    attention is directed by the scope — but are still recorded. The ledger rows
    of the returned slices are kept on ``self._slice_rows`` (same order) for the
    judging order; they are empty when the ledger could not be computed.
    """
    self._slice_rows = []
    try:
        window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=self.db)
        provenance = ledger.fetch_provenance(slices, dcdb=self.db)
        if provenance is None:
            logger.info("Attention: provenance lookup unavailable; slices used as-is, ledger skipped")
            return slices
        rows = ledger.compute_slice_rows(run_id, run_type, slices, started_at, dcdb=self.db,
                                         window_start=window_start, provenance=provenance)
        metrics = [r["metrics"] for r in rows]
        origins: dict[int, str] = {}
        extra_metrics: dict[int, dict] = {}

        if run_type in ("scheduled", "session_start"):
            kept, kept_metrics, needs_arrivals = allocation.enforce_budget(slices, metrics)
            if needs_arrivals:
                # With skim records half the slice goes to the most salient / novel
                # arrivals (docs/SKIM-STAGE.md, Phase 3); without them this is
                # the stratified sample it has always been.
                extra, selection = allocation.build_arrivals_selection(
                    window_start, dcdb=self.db, skim_records=self._skim_records or None)
                if extra is not None:
                    kept.append(extra)
                    origins[len(kept) - 1] = allocation.ARRIVALS_ORIGIN
                    extra_metrics[len(kept) - 1] = selection.metrics
                    logger.info("Added guaranteed arrivals slice with %d memories (%s): %s",
                                len(extra.memory_ids),
                                f"{selection.skim_picked} picked for salience/novelty from the skim, "
                                f"{len(extra.memory_ids) - selection.skim_picked} stratified"
                                if selection.skim_weighted else "stratified, no skim records",
                                extra.name)
                else:
                    logger.info("No arrivals to build a guaranteed slice from")
            slices = kept
            # Re-resolve: the arrivals slice brings ids the first lookup did not cover.
            provenance = ledger.fetch_provenance(slices, dcdb=self.db) or provenance
            rows = ledger.compute_slice_rows(run_id, run_type, slices, started_at, origins,
                                             dcdb=self.db, window_start=window_start,
                                             provenance=provenance)
            for i, m in extra_metrics.items():
                rows[i]["metrics"].update(m)

        self.db.record_slices(run_id, rows)
        ledger.log_run_metrics(ledger.run_metrics(rows))
        self._slice_rows = rows
    except (psycopg2.Error, OSError, ValueError, KeyError):
        logger.warning("Attention allocation failed (non-fatal); using Explorer slices as-is", exc_info=True)
        self._slice_rows = []
    return slices
