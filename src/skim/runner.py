"""The skim runner: read every arrival in a window once, store one record each.

:func:`skim_window` is what the dream cycle's Step 3.5 calls
(``DreamCycleOrchestrator._skim_arrivals``). Contract, from
docs/PLAN-skim-stage.md §2:

* **Selection** — parent-level, non-superseded arrivals with
  ``created_at >= window_start``, excluding the dream cycle's own outputs
  (:func:`src.skim.store.list_arrivals`).
* **Idempotent** — documents that already have a row for
  ``(model, prompt_version)`` are skipped (``reused``), so a re-run or a second
  run type over an overlapping window pays nothing twice.
* **Isolated** — every document is skimmed in its own ``try``; a model error,
  an invalid reply or a storage failure counts against that document and the
  loop goes on. Only :class:`~src.backends.cost.SpendCapExceeded` (the run cap)
  propagates, after the call that tripped it has been stored.
* **Bounded** — documents longer than ``max_doc_chars`` are cut and flagged
  ``truncated``; the skim's own spend is summed and the loop stops cleanly
  (``aborted_on_cap``) once it reaches ``cap_usd``. Every call is also metered
  through ``tracker`` so it counts toward the run cap.
* **Faithful** — before a record is stored its quotes are checked against the
  text the model saw (:func:`src.skim.faithfulness.stored_form`): a quote that
  fails both paths is stored as ``null`` (the item's ``text`` stays) and counted
  in ``quotes_unverified`` on the record, in :class:`SkimResult` and in the run
  stats.
* **Quiet** — logging carries counts and ids only, never document text, titles,
  summaries or quotes.
"""

from __future__ import annotations

import logging
import time
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from src.backends.cost import SpendCapExceeded, SpendTracker, UsageUnavailable, invoke_metered
from src.backends.direct_api import EmptyModelResponse
from src.backends.resolver import RoleBackend, SKIM_ROLE
from src.prompts.skim import SKIM_SYSTEM_PROMPT, build_skim_request
from src.skim import store as default_store
from src.skim.faithfulness import stored_form
from src.skim.schema import SKIM_SCHEMA, SkimValidationError, validate_record

logger = logging.getLogger(__name__)

DEFAULT_MAX_DOC_CHARS = 120_000
DEFAULT_TIMEOUT_S = 120

#: Backlog (triage I1): how far before ``window_start`` to look for arrivals an
#: earlier pass had in scope but failed to store, and how many to retry per run.
DEFAULT_BACKLOG_LOOKBACK_DAYS = 14
DEFAULT_BACKLOG_MAX_DOCS = 20
#: A skim row counts as proof that an earlier pass had a window covering later
#: arrivals only if it was written within this long of its own arrival. Normal
#: windows are <= 3 days (Fri -> Mon); backlog rows for old docs are older than this.
BACKLOG_COVER_MAX_GAP = timedelta(days=7)
#: Tier for the single retry of an empty reply (the stage itself asks for Flex).
RETRY_SERVICE_TIER = "default"


@dataclass
class SkimResult:
    """Counts of one :func:`skim_window` pass — safe to log and persist as run stats."""

    docs_total: int = 0        # arrivals in the window (after the filter)
    skimmed: int = 0           # records stored by this pass
    reused: int = 0            # already skimmed for (model, prompt_version) — skipped
    failed: int = 0            # model/transport/storage error for the document
    invalid: int = 0           # reply was not a valid skim record
    coerced: int = 0           # stored records that needed trimming
    truncated: int = 0         # documents cut to max_doc_chars before the call
    quotes_emitted: int = 0
    quotes_verbatim: int = 0
    quotes_unverified: int = 0  # stored as null: failed both faithfulness paths
    cost_usd: float = 0.0      # this pass's metered spend
    aborted_on_cap: bool = False
    duration_s: float = 0.0
    model: str = ""
    prompt_version: str = ""
    window_start: datetime | None = None
    coercion_classes: dict[str, int] = field(default_factory=dict)
    empty_responses: int = 0   # EmptyModelResponse raised (first attempts and retries)
    retries: int = 0           # retries made after an EmptyModelResponse
    retries_recovered: int = 0  # retries that returned a reply
    backlog: int = 0           # earlier-window arrivals re-selected (failed before, no row yet)
    backlog_ids: list[str] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.skimmed + self.failed + self.invalid

    def to_stats(self) -> dict[str, Any]:
        """The keys persisted on the run record (``dream_cycle_runs.stats``)."""
        return {
            "skim_docs": self.docs_total,
            "skim_skimmed": self.skimmed,
            "skim_reused": self.reused,
            "skim_failed": self.failed,
            "skim_invalid": self.invalid,
            "skim_coerced": self.coerced,
            "skim_truncated": self.truncated,
            "skim_quotes_emitted": self.quotes_emitted,
            "skim_quotes_verbatim": self.quotes_verbatim,
            "skim_quotes_unverified": self.quotes_unverified,
            "skim_cost_usd": round(self.cost_usd, 6),
            "skim_aborted_on_cap": self.aborted_on_cap,
            "skim_duration_s": round(self.duration_s, 1),
            "skim_model": self.model,
            "skim_prompt_version": self.prompt_version,
            "skim_coercion_classes": dict(self.coercion_classes),
            "skim_empty_responses": self.empty_responses,
            "skim_retries": self.retries,
            "skim_retries_recovered": self.retries_recovered,
            "skim_backlog": self.backlog,
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat() if self.window_start else None
        return d


def _first(output: Any) -> Any:
    if isinstance(output, list):
        return output[0] if output else None
    return output


def _select_documents(window_start: datetime, model: str, prompt_version: str, *,
                      fetch_docs: Callable[[datetime], list[dict]] | None, store) -> tuple[int, list[dict]]:
    """Arrivals in the window and the subset still to skim (with content).

    Content is loaded only for the documents that need it: ``list_arrivals``
    carries no text, ``missing_ids`` filters, ``fetch_documents`` loads.
    """
    if fetch_docs is not None:
        docs = fetch_docs(window_start) or []
        ids = [str(d["id"]) for d in docs]
        todo = set(store.missing_ids(ids, model, prompt_version))
        return len(ids), [d for d in docs if str(d["id"]) in todo]
    arrivals = store.list_arrivals(window_start)
    ids = [str(a["id"]) for a in arrivals]
    todo_ids = store.missing_ids(ids, model, prompt_version)
    return len(ids), (store.fetch_documents(todo_ids) if todo_ids else [])


def _select_backlog(window_start: datetime, model: str, prompt_version: str, *, store,
                    lookback_days: int, max_docs: int) -> list[dict]:
    """Earlier-window arrivals an earlier pass had in scope but stored nothing for.

    The ledger starts each window at the previous run's start, so a document
    whose skim call failed is never selected again by the window query (triage
    I1). This re-selects, newest first and at most ``max_docs``, arrivals created
    in ``[window_start - lookback_days, window_start)`` that still have no row for
    ``(model, prompt_version)`` **and** were demonstrably in scope of an earlier
    pass with that same key.

    "Failed before" vs "never in scope": no failure is recorded anywhere, so it
    is inferred from the rows that *do* exist. A pass's window is a contiguous
    interval ``[ws, t_run]``. If some arrival ``A`` with
    ``A.created_at <= D.created_at`` has a row (same key) skimmed at or after
    ``D.created_at``, the pass that wrote it had ``ws <= A.created_at <=
    D.created_at <= t_run`` — so ``D`` was in that pass's window and missing it
    means the call failed (error, invalid reply, sub-cap stop). Arrivals with no
    such covering row were never in scope of this key: before a prompt-version
    or model change, or while the skim was disabled. Those are left alone, so a
    version bump does not turn into a two-week re-skim.

    Idempotent: a re-selected document that is now stored has a row and drops
    out; one that keeps failing ages out of the lookback. Reads no content
    except for the (≤ ``max_docs``) documents returned.
    """
    if lookback_days <= 0 or max_docs <= 0 or not hasattr(store, "fetch_records"):
        return []
    lookback_start = window_start - timedelta(days=lookback_days)
    prior = []
    for a in store.list_arrivals(lookback_start) or []:
        created = a.get("created_at")
        if isinstance(created, datetime) and created < window_start:
            prior.append((str(a["id"]), created))
    if not prior:
        return []
    ids = [i for i, _ in prior]
    missing = set(store.missing_ids(ids, model, prompt_version))
    if not missing:
        return []
    rows = store.fetch_records([i for i in ids if i not in missing], model, prompt_version) or {}
    # Skimmed arrivals by arrival time, with the running latest skim time: for a
    # missing D, a covering pass exists iff some skimmed A with A.created_at <=
    # D.created_at was skimmed at or after D.created_at.
    # Only rows written within BACKLOG_COVER_MAX_GAP of their arrival count as
    # coverage: a row the backlog itself wrote days later for an old failed doc
    # proves nothing about the docs after it (they may have arrived while the skim
    # was disabled), so it must not seed further re-skims.
    skimmed = sorted(
        (created, rows[i].get("created_at"))
        for i, created in prior
        if i in rows and isinstance(rows[i].get("created_at"), datetime)
        and rows[i]["created_at"] - created <= BACKLOG_COVER_MAX_GAP
    )
    arrival_times = [c for c, _ in skimmed]
    latest_skim: list[datetime] = []
    for _, skimmed_at in skimmed:
        latest_skim.append(skimmed_at if not latest_skim else max(latest_skim[-1], skimmed_at))
    chosen: list[str] = []
    for doc_id, created in prior:  # newest first (list_arrivals order)
        if doc_id not in missing:
            continue
        k = bisect_right(arrival_times, created)
        if k and latest_skim[k - 1] >= created:
            chosen.append(doc_id)
            if len(chosen) >= max_docs:
                break
    return store.fetch_documents(chosen) if chosen else []


def skim_window(
    window_start: datetime,
    *,
    invoker,
    model: str,
    prompt_version: str,
    tracker,
    cap_usd: float,
    run_id: str | None,
    fetch_docs: Callable[[datetime], list[dict]] | None = None,
    store=None,
    max_doc_chars: int = DEFAULT_MAX_DOC_CHARS,
    effort: str = "low",
    timeout: int = DEFAULT_TIMEOUT_S,
    coerce: bool = True,
    backlog_lookback_days: int = DEFAULT_BACKLOG_LOOKBACK_DAYS,
    backlog_max_docs: int = DEFAULT_BACKLOG_MAX_DOCS,
) -> SkimResult:
    """Skim every not-yet-skimmed arrival since ``window_start``; see module docstring.

    Args:
        invoker: A tool-less :class:`~src.backends.base.Invoker` (Bedrock) whose
            ``invoke`` accepts ``schema=``.
        model: Model id, for pricing and the idempotency key.
        prompt_version: :data:`src.prompts.skim.PROMPT_VERSION` (idempotency key).
        tracker: The run's ``SpendTracker``; every call's usage goes through it (run cap).
        cap_usd: The skim's own sub-cap; the loop stops when its spend reaches it.
        run_id: Attribution for the stored rows and the metrics line.
        fetch_docs: Optional ``window_start -> [doc]`` override (tests); docs
            carry ``id, title, source_type, created_at, content``.
        store: Module/object with ``list_arrivals, missing_ids, fetch_documents,
            upsert_skim`` (default :mod:`src.skim.store`).
        max_doc_chars: Documents longer than this are cut and flagged.
        backlog_lookback_days / backlog_max_docs: Bounds of the failed-before
            backlog (:func:`_select_backlog`); ``backlog_max_docs=0`` disables it.

    An :class:`~src.backends.direct_api.EmptyModelResponse` (no text, no tool
    call) is retried once — on :data:`RETRY_SERVICE_TIER` when the invoker
    supports a per-call tier override — unless the tracker is blocked or the
    sub-cap is already reached; both attempts count toward the sub-cap.
    """
    store = store if store is not None else default_store
    if not isinstance(tracker, SpendTracker):
        raise TypeError("tracker must be a SpendTracker")
    spec = RoleBackend(SKIM_ROLE, "bedrock", model, effort)
    t0 = time.monotonic()
    result = SkimResult(model=model, prompt_version=prompt_version, window_start=window_start)
    coercion_classes: dict[str, int] = {}

    # Selection spends nothing; if it fails the caller (orchestrator) logs and moves on.
    result.docs_total, docs = _select_documents(window_start, model, prompt_version,
                                                fetch_docs=fetch_docs, store=store)
    result.reused = result.docs_total - len(docs)
    logger.info("Skim: %d arrival(s) since %s; %d already skimmed, %d to skim (model=%s prompt=%s cap $%.2f)",
                result.docs_total, window_start.isoformat() if hasattr(window_start, "isoformat") else window_start,
                result.reused, len(docs), model, prompt_version, cap_usd)

    # Backlog: earlier-window arrivals whose skim failed (see _select_backlog).
    # Never fatal; skipped for the ``fetch_docs`` override. Processed after the
    # window's own documents so the sub-cap favours the current window.
    if fetch_docs is None and backlog_max_docs > 0:
        try:
            window_ids = {str(d.get("id")) for d in docs}
            backlog = [d for d in _select_backlog(window_start, model, prompt_version, store=store,
                                                  lookback_days=backlog_lookback_days,
                                                  max_docs=backlog_max_docs)
                       if str(d.get("id")) not in window_ids]
        except Exception as exc:  # noqa: BLE001 — the backlog must never stop the window's skim
            logger.warning("Skim backlog selection failed (non-fatal): %s", type(exc).__name__)
            backlog = []
        if backlog:
            result.backlog = len(backlog)
            result.backlog_ids = [str(d.get("id")) for d in backlog]
            logger.info("Skim backlog: %d earlier-window arrival(s) with a failed skim re-selected "
                        "(lookback %d days, max %d): %s", len(backlog), backlog_lookback_days,
                        backlog_max_docs, ", ".join(i[:8] for i in result.backlog_ids))
            docs = list(docs) + backlog
    tier_override = (
        {"service_tier": RETRY_SERVICE_TIER}
        if getattr(invoker, "SUPPORTS_SERVICE_TIER_OVERRIDE", False) is True else {}
    )

    for n, doc in enumerate(docs, 1):
        if cap_usd is not None and result.cost_usd >= cap_usd:
            result.aborted_on_cap = True
            logger.warning("Skim stopped at its sub-cap: $%.4f >= $%.2f after %d/%d document(s)",
                           result.cost_usd, cap_usd, n - 1, len(docs))
            break
        doc_id = str(doc.get("id"))
        content = doc.get("content") or ""
        truncated = False
        if len(content) > max_doc_chars:
            content = content[:max_doc_chars]
            truncated = True
            result.truncated += 1
        request = build_skim_request(content, doc.get("source_type"), doc.get("title"), doc.get("created_at"))

        cap_exc: SpendCapExceeded | None = None
        before = tracker.total_usd
        call_opts: dict[str, Any] = {}
        retried = False
        reply = None
        while True:
            try:
                reply = invoke_metered(
                    invoker, spec, tracker, SKIM_SYSTEM_PROMPT, request,
                    tools=False, effort=effort, timeout=timeout,
                    stage="skim", run_id=run_id, schema=SKIM_SCHEMA, **call_opts,
                )
            except UsageUnavailable:
                raise
            except SpendCapExceeded as exc:
                # A completed response that crossed the cap can still be stored.
                # A failed/truncated response has no usable record to preserve.
                reply = getattr(exc, "invocation_result", None)
                if reply is None:
                    raise
                cap_exc = exc
            except Exception as exc:
                if isinstance(exc, EmptyModelResponse):
                    result.empty_responses += 1
                    spent = result.cost_usd + (tracker.total_usd - before)
                    if (not retried and not tracker.blocked_reason
                            and (cap_usd is None or spent < cap_usd)):
                        # One retry (triage I1), on the standard tier when the
                        # backend can override it per call. Both attempts are
                        # metered through ``tracker`` and charged to this
                        # document (and so to the skim sub-cap) below.
                        retried = True
                        result.retries += 1
                        call_opts = dict(tier_override)
                        logger.warning("Skim reply empty for memory %s (%d/%d, stopReason=%s); retrying once%s",
                                       doc_id[:8], n, len(docs), exc.stop_reason,
                                       f" on service tier {RETRY_SERVICE_TIER!r}" if call_opts else "")
                        continue
                result.failed += 1
                result.cost_usd += tracker.total_usd - before
                if tracker.blocked_reason:
                    raise UsageUnavailable(tracker.blocked_reason) from exc
                logger.warning("Skim call failed for memory %s (%d/%d): %s%s", doc_id[:8], n, len(docs),
                               type(exc).__name__,
                               f" (stopReason={exc.stop_reason})" if isinstance(exc, EmptyModelResponse) else "")
                reply = None
            break
        if reply is None:
            continue
        if retried:
            result.retries_recovered += 1

        usage = reply.get("usage")
        cost = tracker.total_usd - before
        result.cost_usd += cost

        try:
            record = validate_record(_first(reply.get("output") if isinstance(reply, dict) else None),
                                     coerce=coerce)
        except SkimValidationError as exc:
            result.invalid += 1
            logger.warning("Skim reply invalid for memory %s (%d/%d): %s", doc_id[:8], n, len(docs),
                           str(exc).split(", got")[0][:120])
            if cap_exc is not None:
                raise cap_exc
            continue

        # Stored form: quotes that fail both faithfulness paths become null (see
        # src.skim.faithfulness.stored_form); the check runs against the text the
        # model actually saw (after truncation).
        skim_data, faith = stored_form(record, content)
        try:
            store.upsert_skim(
                doc_id, model=model, prompt_version=prompt_version, skim=skim_data, run_id=run_id,
                input_tokens=usage.get("inputTokens", usage.get("input_tokens")) if usage else None,
                output_tokens=usage.get("outputTokens", usage.get("output_tokens")) if usage else None,
                cost_usd=cost, truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            logger.warning("Skim store failed for memory %s (%d/%d): %s", doc_id[:8], n, len(docs),
                           type(exc).__name__)
            if cap_exc is not None:
                raise cap_exc
            continue

        result.skimmed += 1
        result.quotes_emitted += faith["quotes_emitted"]
        result.quotes_verbatim += faith["quotes_verbatim"]
        result.quotes_unverified += faith["quotes_fabricated"]
        if record.coercions:
            result.coerced += 1
            for c in record.coercions:
                coercion_classes[c] = coercion_classes.get(c, 0) + 1
        if cap_exc is not None:
            raise cap_exc

    result.coercion_classes = coercion_classes
    result.duration_s = time.monotonic() - t0
    logger.info("Skim done: %d skimmed, %d reused, %d failed, %d invalid, %d coerced, %d truncated; "
                "quotes %d/%d verbatim (%d stored as unverified); %d empty repl(ies), %d retried, "
                "%d recovered; backlog %d; coercions %s; $%.4f in %.1fs%s",
                result.skimmed, result.reused, result.failed, result.invalid, result.coerced, result.truncated,
                result.quotes_verbatim, result.quotes_emitted, result.quotes_unverified,
                result.empty_responses, result.retries, result.retries_recovered, result.backlog,
                dict(sorted(coercion_classes.items())) or "none", result.cost_usd,
                result.duration_s, " (stopped at sub-cap)" if result.aborted_on_cap else "")
    return result
