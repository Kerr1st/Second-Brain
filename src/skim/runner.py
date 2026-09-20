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
* **Quiet** — logging carries counts and ids only, never document text, titles,
  summaries or quotes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from src.backends.cost import SpendCapExceeded, estimate_cost_usd
from src.prompts.skim import SKIM_SYSTEM_PROMPT, build_skim_request
from src.skim import store as default_store
from src.skim.faithfulness import check_record
from src.skim.schema import SKIM_SCHEMA, SkimValidationError, validate_record

logger = logging.getLogger(__name__)

DEFAULT_MAX_DOC_CHARS = 120_000
DEFAULT_TIMEOUT_S = 120


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
    cost_usd: float = 0.0      # this pass's metered spend
    aborted_on_cap: bool = False
    duration_s: float = 0.0
    model: str = ""
    prompt_version: str = ""
    window_start: datetime | None = None
    coercion_classes: dict[str, int] = field(default_factory=dict)

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
            "skim_cost_usd": round(self.cost_usd, 6),
            "skim_aborted_on_cap": self.aborted_on_cap,
            "skim_duration_s": round(self.duration_s, 1),
            "skim_model": self.model,
            "skim_prompt_version": self.prompt_version,
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat() if self.window_start else None
        return d


def _record_fn(tracker: Any) -> Callable[[str, dict | None], float]:
    """``tracker`` may be a SpendTracker (``.record(model, usage)``) or a bare
    ``record_spend(model, usage) -> cost`` callable."""
    if hasattr(tracker, "record"):
        return tracker.record
    if callable(tracker):
        return tracker
    raise TypeError("tracker must be a SpendTracker or a callable (model, usage) -> cost")


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
) -> SkimResult:
    """Skim every not-yet-skimmed arrival since ``window_start``; see module docstring.

    Args:
        invoker: A tool-less :class:`~src.backends.base.Invoker` (Bedrock) whose
            ``invoke`` accepts ``schema=``.
        model: Model id, for pricing and the idempotency key.
        prompt_version: :data:`src.prompts.skim.PROMPT_VERSION` (idempotency key).
        tracker: The run's ``SpendTracker`` or a ``record_spend(model, usage)``
            callable; every call's usage goes through it (run cap).
        cap_usd: The skim's own sub-cap; the loop stops when its spend reaches it.
        run_id: Attribution for the stored rows and the metrics line.
        fetch_docs: Optional ``window_start -> [doc]`` override (tests); docs
            carry ``id, title, source_type, created_at, content``.
        store: Module/object with ``list_arrivals, missing_ids, fetch_documents,
            upsert_skim`` (default :mod:`src.skim.store`).
        max_doc_chars: Documents longer than this are cut and flagged.
    """
    store = store if store is not None else default_store
    record_spend = _record_fn(tracker)
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
        try:
            reply = invoker.invoke(SKIM_SYSTEM_PROMPT, request, tools=False, effort=effort, timeout=timeout,
                                   stage="skim", run_id=run_id, schema=SKIM_SCHEMA)
        except SpendCapExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — one document must never stop the rest
            result.failed += 1
            logger.warning("Skim call failed for memory %s (%d/%d): %s", doc_id[:8], n, len(docs),
                           type(exc).__name__)
            continue

        usage = reply.get("usage") if isinstance(reply, dict) else None
        usage = usage if isinstance(usage, dict) else None
        cost = 0.0
        if usage is not None:
            try:
                cost = float(record_spend(model, usage) or 0.0)
            except SpendCapExceeded as exc:
                # The call was made and paid for: keep its record, then propagate.
                cap_exc = exc
                cost = estimate_cost_usd(model, usage)
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

        faith = check_record(record, content)
        try:
            store.upsert_skim(
                doc_id, model=model, prompt_version=prompt_version, skim=record.to_dict(), run_id=run_id,
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
        if record.coercions:
            result.coerced += 1
            for c in record.coercions:
                coercion_classes[c] = coercion_classes.get(c, 0) + 1
        if cap_exc is not None:
            raise cap_exc

    result.coercion_classes = coercion_classes
    result.duration_s = time.monotonic() - t0
    logger.info("Skim done: %d skimmed, %d reused, %d failed, %d invalid, %d coerced, %d truncated; "
                "quotes %d/%d verbatim; $%.4f in %.1fs%s",
                result.skimmed, result.reused, result.failed, result.invalid, result.coerced, result.truncated,
                result.quotes_verbatim, result.quotes_emitted, result.cost_usd, result.duration_s,
                " (stopped at sub-cap)" if result.aborted_on_cap else "")
    return result
