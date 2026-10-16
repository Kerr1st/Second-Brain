"""Optional skim input for discovery (Step 3.5).

R1 policy, preserved in R3: the skim is **non-fatal**. Any failure other than
``SpendCapExceeded`` logs one WARNING and the Explorer gets the plain arrivals
manifest. The 74ba02d fail-stop (``SkimFailureThreshold``) is deliberately not
ported — see R3 release notes.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime
from src.backends.base import BACKEND_CAPABILITIES
from src.backends.cost import SpendCapExceeded
from src.backends.resolver import SKIM_ROLE
from src.prompts.skim import PROMPT_VERSION as SKIM_PROMPT_VERSION
from src.dream_cycle import ledger
from src.dream_cycle.budget import *
from src.dream_cycle.budget import _skim_cap_usd
from src.skim import store as skim_store
from src.skim.cluster import cluster_window
from src.skim.runner import SkimResult, skim_window
logger = logging.getLogger(__name__)


def _load_skim_records(self, skim: SkimResult | None, run_id: str, run_type: str,
                       started_at: datetime) -> list[dict]:
    """The window's skim records for the Explorer inventory and the weighted
    guaranteed slice (docs/SKIM-STAGE.md, Phase 3).

    Fetched only when Step 3.5 ran and something is stored for the window
    (``skimmed + reused > 0``). The records are then clustered by their
    existing embeddings (``[c<k>]`` tags; no model calls). Never raises:
    any failure logs a WARNING and returns ``[]``, which makes the Explorer
    fall back to the plain manifest.
    """
    if skim is None or (skim.skimmed + skim.reused) <= 0:
        return []
    try:
        window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=self.db)
        backlog_ids = list(getattr(skim, "backlog_ids", None) or [])
        if backlog_ids:  # earlier-window docs whose skim failed before, skimmed by this run
            records = skim_store.fetch_window_records(window_start, skim.model, skim.prompt_version,
                                                      extra_ids=backlog_ids)
        else:
            records = skim_store.fetch_window_records(window_start, skim.model, skim.prompt_version)
    except Exception:
        logger.warning("Skim records unavailable for the window (non-fatal); the Explorer gets the manifest",
                       exc_info=True)
        return []
    if not isinstance(records, list) or not records:
        logger.warning("Skim ran (%d skimmed, %d reused) but no records came back for the window",
                       skim.skimmed, skim.reused)
        return []
    try:
        cluster_window(records, model=skim.model, prompt_version=skim.prompt_version)
    except Exception:
        logger.warning("Skim clustering failed (non-fatal); inventory without cluster tags", exc_info=True)
    logger.info("Skim records loaded for the window: %d (of %d arrivals)", len(records), skim.docs_total)
    return records


def _skim_arrivals(self, run_id: str, run_type: str, started_at: datetime) -> SkimResult | None:
    """Step 3.5 — skim every arrival in the run's window (docs/SKIM-STAGE.md).

    Resolves the ``skim`` role through the active profile and runs
    :func:`~src.skim.runner.skim_window` over ``ledger.arrival_window_start``,
    metering every call through the run's SpendTracker under the skim's own
    sub-cap (:func:`_skim_cap_usd`). Returns the counts, or ``None`` — with one
    WARNING — when the role is not configured, resolves to a non-Bedrock /
    agentic backend, or anything at all fails: the run then continues exactly
    as before (the Explorer gets the plain arrivals manifest). Only
    :class:`SpendCapExceeded` propagates, like in every other stage.

    Operational off-switch: ``SECOND_BRAIN_SKIM_DISABLED=1`` (also ``true``/``yes``)
    skips the stage with one WARNING and no model calls.
    """
    if os.environ.get(SKIM_DISABLED_ENV, "").strip().lower() in {"1", "true", "yes"}:
        logger.warning("Skim skipped: %s is set; the Explorer gets the arrivals manifest", SKIM_DISABLED_ENV)
        return None
    try:
        spec = self.resolver.spec_for(SKIM_ROLE)
    except KeyError:
        logger.warning("Skim skipped: role %r is not configured in the active backend profile", SKIM_ROLE)
        return None
    try:
        backend = getattr(spec, "backend", None)
        caps = BACKEND_CAPABILITIES.get(backend)
        if caps is None or caps.supports_mcp:
            logger.warning("Skim skipped: role %r must run on the direct Bedrock backend, not %r",
                           SKIM_ROLE, backend)
            return None
        invoker = self._invoker_for(SKIM_ROLE)
        window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=self.db)
        cap = _skim_cap_usd(self._spend.cap_usd)
        result = skim_window(
            window_start,
            invoker=invoker,
            model=spec.model,
            prompt_version=SKIM_PROMPT_VERSION,
            tracker=self._spend,
            cap_usd=cap,
            run_id=run_id,
            effort=spec.effort or "low",
        )
    except SpendCapExceeded:
        raise
    except Exception:
        logger.warning("Skim failed (non-fatal); the Explorer gets the arrivals manifest as before",
                       exc_info=True)
        return None
    logger.info(
        "Skim (%s): %d arrival(s) since %s — %d skimmed, %d reused, %d failed, %d invalid, %d coerced, "
        "%d truncated; quotes %d/%d verbatim; $%.4f of $%.2f sub-cap in %.0fs%s",
        spec.model, result.docs_total, window_start.isoformat(), result.skimmed, result.reused,
        result.failed, result.invalid, result.coerced, result.truncated, result.quotes_verbatim,
        result.quotes_emitted, result.cost_usd, cap, result.duration_s,
        " (stopped at sub-cap)" if result.aborted_on_cap else "",
    )
    return result
