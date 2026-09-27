"""Optional skim input for discovery, with explicit fail-stop boundaries."""
from __future__ import annotations
import psycopg2
import logging
import os
from datetime import datetime
from src.backends.base import BACKEND_CAPABILITIES
from src.backends.cost import SpendCapExceeded
from src.backends.resolver import SKIM_ROLE
from src.prompts.skim import PROMPT_VERSION as SKIM_PROMPT_VERSION
from src.dream_cycle import ledger
from src.dream_cycle.budget import *
logger = logging.getLogger(__name__)
from src.skim import SkimResult, skim_window, fetch_window_records, cluster_window
from src.skim.runner import SkimFailureThreshold
from src.dream_cycle.budget import _skim_cap_usd

def _load_skim_records(self, skim: SkimResult | None, run_id: str, run_type: str,
                       started_at: datetime) -> list[dict]:
    """The window's skim records for the Explorer inventory and the weighted
    guaranteed slice (docs/SKIM-STAGE.md, Phase 3).

    Fetched only when Step 3.5 ran and something is stored for the window
    (``skimmed + reused > 0``). The records are then clustered by their
    existing embeddings (``[c<k>]`` tags; no model calls). Expected database,
    I/O and malformed-data failures fall back with a warning. Programming
    errors propagate so a broken stage cannot masquerade as missing data.
    """
    if skim is None or (skim.skimmed + skim.reused) <= 0:
        return []
    try:
        window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=self.db)
        records = fetch_window_records(window_start, skim.model, skim.prompt_version)
    except (psycopg2.Error, OSError, ValueError, KeyError):
        logger.warning("Skim records unavailable for the window (non-fatal); the Explorer gets the manifest",
                       exc_info=True)
        return []
    if not isinstance(records, list) or not records:
        logger.warning("Skim ran (%d skimmed, %d reused) but no records came back for the window",
                       skim.skimmed, skim.reused)
        return []
    try:
        cluster_window(records, model=skim.model, prompt_version=skim.prompt_version)
    except (psycopg2.Error, OSError, ValueError, KeyError):
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
    agentic backend, or an expected selection error occurs. The Explorer then
    gets the plain manifest. Spend/accounting gaps, repeated document failures
    and programming errors propagate with any available partial counts.

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
    except (SpendCapExceeded, SkimFailureThreshold) as exc:
        if getattr(exc, "skim_result", None) is not None:
            self._run_stats.update(exc.skim_result.to_stats())
        raise
    except (psycopg2.Error, OSError, ValueError, KeyError):
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
