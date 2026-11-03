"""Attention ledger — measure what the Explorer actually looked at.

Every run persists its slices (memory ids included) and a small set of metrics
that answer, per slice and per run:

* arrival_share      — fraction of the slice's memories created since the
                        previous completed run of the same type (the "arrivals
                        window"). 0.0 means the slice ignored everything new.
* source_mix         — counts by memories.source_type.
* source_types       — number of distinct source types (breadth).
* concentration      — share of the dominant source type (1.0 = one channel).
* episodic_share     — fraction whose type is 'source' (raw capture) as opposed
                        to semantic types (insight, synthesis, decision, ...).
                        The panel rejects for evidence independence; slices built
                        from interdependent syntheses score low here.
* distinct_parents   — number of distinct parent documents / conversations the
                        memories come from (another independence proxy).
* revisit_share      — fraction already present in slices of the previous N runs.
* resolved / requested — how many of the Explorer's ids exist.

The computation is pure (:func:`slice_metrics`, :func:`run_metrics`) so it is
testable without a database; :func:`record_run_slices` does the I/O.

Baseline (2026-09-14..17, three scheduled runs, backfilled from candidate
citations): arrival_share 0.00, source_types 1, concentration 1.00,
episodic_share 0.00. See docs/ARCHITECTURE.md.
"""
from __future__ import annotations
from src.db import get_memory_provenance
from src.dream_cycle.storage import canonical_uuid

import logging
import psycopg2
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

EPISODIC_TYPES = {"source"}
DEFAULT_ARRIVAL_WINDOW = timedelta(days=7)   # when there is no previous run to anchor on
# The window is anchored on the previous run of the same type, but never shorter
# than a day (a re-run at 18:00 after the 05:00 run must still see last night's
# captures) and never longer than two weeks (a long gap should not pull the whole
# backlog into one slice).
MIN_ARRIVAL_WINDOW = timedelta(hours=24)
MAX_ARRIVAL_WINDOW = timedelta(days=14)
REVISIT_RUNS = 3


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def canonical_ids(memory_ids: Iterable) -> list[str]:
    """Explorer ids in canonical form (lower-case, hyphenated UUID), deduplicated
    in first-seen order. Upper-case, braced, URN or unhyphenated UUIDs resolve in
    the database but would miss a provenance dict keyed by canonical ids; strings
    that are not UUIDs are kept verbatim so they still fail to resolve."""
    out: list[str] = []
    seen: set[str] = set()
    for m in memory_ids or []:
        key = canonical_uuid(m) or str(m)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def slice_metrics(memory_ids: Iterable[str], provenance: dict[str, dict],
                  window_start: datetime, previous_ids: set[str] | None = None) -> dict:
    """Metrics for one slice.

    Args:
        memory_ids: ids as the Explorer emitted them (may include unknown ids).
        provenance: id -> {type, source_type, created_at, parent_id, project} for
            the ids that exist (see ``db.get_memory_provenance``).
        window_start: start of the arrivals window.
        previous_ids: memory ids seen in slices of recent runs.
    """
    # Defensive: canonicalise before the lookup (provenance is keyed by canonical
    # ids; the Explorer path also canonicalises at parse time, issue I4 2026-10-14).
    ids = canonical_ids(memory_ids)
    rows = [provenance[i] for i in ids if i in provenance]
    n = len(rows)
    previous_ids = previous_ids or set()
    window_start = _aware(window_start)

    def share(count: int) -> float:
        return round(count / n, 3) if n else 0.0

    source_mix = Counter((r.get("source_type") or "unknown") for r in rows)
    arrivals = sum(1 for r in rows if _aware(r.get("created_at")) and _aware(r["created_at"]) >= window_start)
    episodic = sum(1 for r in rows if (r.get("type") or "") in EPISODIC_TYPES)
    parents = {str(r.get("parent_id") or r.get("id")) for r in rows}
    revisits = sum(1 for i in ids if i in previous_ids)
    projects = Counter((r.get("project") or "") for r in rows if r.get("project"))
    top_source = source_mix.most_common(1)[0] if source_mix else ("", 0)

    return {
        "requested": len(ids),
        "resolved": n,
        "arrival_share": share(arrivals),
        "source_mix": dict(source_mix),
        "source_types": len(source_mix),
        "concentration": share(top_source[1]),
        "dominant_source": top_source[0],
        "episodic_share": share(episodic),
        "distinct_parents": len(parents),
        "revisit_share": round(revisits / len(ids), 3) if ids else 0.0,
        "top_project": projects.most_common(1)[0][0] if projects else None,
    }


def run_metrics(slices: list[dict]) -> dict:
    """Aggregate per-slice metrics into run-level metrics (memory-weighted)."""
    metrics = [s["metrics"] for s in slices if s.get("metrics")]
    total = sum(m["resolved"] for m in metrics)
    if not total:
        return {"slices": len(slices), "memories": 0, "arrival_share": 0.0, "source_types": 0,
                "concentration": 0.0, "episodic_share": 0.0, "revisit_share": 0.0,
                "arrival_anchored_slices": 0, "source_mix": {}}

    def weighted(key: str) -> float:
        return round(sum(m[key] * m["resolved"] for m in metrics) / total, 3)

    mix: Counter = Counter()
    for m in metrics:
        mix.update(m["source_mix"])
    top = mix.most_common(1)[0][1] if mix else 0
    return {
        "slices": len(slices),
        "memories": total,
        "arrival_share": weighted("arrival_share"),
        "source_types": len(mix),
        "source_mix": dict(mix),
        "concentration": round(top / total, 3),
        "episodic_share": weighted("episodic_share"),
        "revisit_share": weighted("revisit_share"),
        # A slice "anchored" in the arrivals window: most of its evidence is new and
        # it spans more than one channel. This is the quantity PR3 budgets on.
        "arrival_anchored_slices": sum(
            1 for m in metrics if m["arrival_share"] >= 0.6 and m["source_types"] >= 2),
    }


def format_attention_section(run: dict, slices: list[dict]) -> list[str]:
    """Markdown lines for the digest's Attention section."""
    lines = ["## Attention (what the Explorer examined)", ""]
    if not run.get("memories"):
        lines += ["No slice memories resolved.", ""]
        return lines
    mix = ", ".join(f"{k} {v}" for k, v in sorted(run["source_mix"].items(), key=lambda kv: -kv[1]))
    lines += [
        f"- Slices: {run['slices']} ({run['arrival_anchored_slices']} anchored in the arrivals window)",
        f"- Memories examined: {run['memories']}",
        f"- Arrival share (created since previous run): {run['arrival_share']:.0%}",
        f"- Source types: {run['source_types']} — {mix}",
        f"- Concentration (dominant source): {run['concentration']:.0%}",
        f"- Episodic share (raw captures vs syntheses): {run['episodic_share']:.0%}",
        f"- Revisit share (seen in last {REVISIT_RUNS} runs): {run['revisit_share']:.0%}",
        "",
    ]
    for s in slices:
        m = s.get("metrics") or {}
        if not m:
            continue
        origin = f" [{s['origin']}]" if s.get("origin") and s["origin"] != "explorer" else ""
        lines.append(
            f"- **{s.get('name') or '(unnamed)'}**{origin} — {s.get('strategy') or '?'}; "
            f"{m['resolved']}/{m['requested']} memories, arrivals {m['arrival_share']:.0%}, "
            f"{m['source_types']} source type(s) ({m['dominant_source']} {m['concentration']:.0%}), "
            f"episodic {m['episodic_share']:.0%}, parents {m['distinct_parents']}, revisit {m['revisit_share']:.0%}"
        )
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# I/O                                                                          #
# --------------------------------------------------------------------------- #
def _dcdb(dcdb=None):
    if dcdb is not None:
        return dcdb
    import src.dream_cycle_db as dream_cycle_db
    return dream_cycle_db


def arrival_window_start(run_id: str, run_type: str, started_at: datetime | None = None, dcdb=None) -> datetime:
    """Start of the arrivals window: previous completed run of this type, clamped to
    [MIN_ARRIVAL_WINDOW, MAX_ARRIVAL_WINDOW] before ``started_at``; 7 days back when
    there is no previous run. Tolerates a mocked ``dcdb`` (non-datetime → fallback)."""
    anchor = _aware(started_at) if isinstance(started_at, datetime) else datetime.now(timezone.utc)
    prev = _dcdb(dcdb).get_previous_run_started_at(run_id, run_type)
    if not isinstance(prev, datetime):
        return anchor - DEFAULT_ARRIVAL_WINDOW
    prev = _aware(prev)
    return min(anchor - MIN_ARRIVAL_WINDOW, max(anchor - MAX_ARRIVAL_WINDOW, prev))


def fetch_provenance(slices, dcdb=None) -> dict[str, dict] | None:
    """id -> provenance row for every memory id in ``slices``.

    Returns None when the lookup did not produce a list (e.g. the DB layer is
    mocked or unavailable) so callers can tell "nothing resolved" from "could
    not look anything up" — the budget must not drop slices in the latter case.
    """
    dcdb = _dcdb(dcdb)
    all_ids = []
    for s in slices:
        all_ids += [str(m) for m in (_get(s, "memory_ids") or [])]
    prov = get_memory_provenance(all_ids)
    if not isinstance(prov, list):
        return None
    return {canonical_uuid(r["id"]) or str(r["id"]): r for r in prov}


def compute_slice_rows(run_id: str, run_type: str, slices, started_at: datetime | None = None,
                       origins: dict[int, str] | None = None, dcdb=None,
                       window_start: datetime | None = None,
                       provenance: dict[str, dict] | None = None) -> list[dict]:
    """Slice rows (with metrics) ready for ``record_slices``; pure apart from reads."""
    dcdb = _dcdb(dcdb)
    if window_start is None:
        window_start = arrival_window_start(run_id, run_type, started_at, dcdb)
    previous = dcdb.get_recent_slice_memory_ids(run_id, REVISIT_RUNS)
    previous_ids = set(previous) if isinstance(previous, (set, list, tuple)) else set()
    if provenance is None:
        provenance = fetch_provenance(slices, dcdb) or {}

    rows = []
    for i, s in enumerate(slices):
        ids = [str(m) for m in (_get(s, "memory_ids") or [])]
        rows.append({
            "name": _get(s, "name"), "strategy": _get(s, "strategy"),
            "hypothesis": _get(s, "hypothesis"), "memory_ids": ids,
            "memory_titles": _get(s, "memory_titles") or [],
            "origin": (origins or {}).get(i, "explorer"),
            "metrics": slice_metrics(ids, provenance, window_start, previous_ids),
        })
    return rows


def log_run_metrics(run: dict) -> None:
    if not run:
        return
    logger.info(
        "Attention: %d slices, %d memories, arrivals %.0f%%, %d source types "
        "(dominant %.0f%%), episodic %.0f%%, revisit %.0f%%, anchored slices %d",
        run["slices"], run["memories"], 100 * run["arrival_share"], run["source_types"],
        100 * run["concentration"], 100 * run["episodic_share"], 100 * run["revisit_share"],
        run["arrival_anchored_slices"],
    )


def record_run_slices(run_id: str, run_type: str, slices, started_at: datetime | None = None,
                      origins: dict[int, str] | None = None, dcdb=None) -> dict:
    """Compute metrics for the run's slices, persist them, and return run metrics.

    ``slices`` are MemorySlice objects (or dicts with the same fields). Expected
    storage/data errors return no metrics; programming errors remain visible.
    """
    dcdb = _dcdb(dcdb)
    try:
        rows = compute_slice_rows(run_id, run_type, slices, started_at, origins, dcdb)
        dcdb.record_slices(run_id, rows)
        run = run_metrics(rows)
        log_run_metrics(run)
        return run
    except (psycopg2.Error, OSError, ValueError, KeyError):
        logger.warning("Attention ledger failed (non-fatal)", exc_info=True)
        return {}


def _get(obj, field: str):
    return obj.get(field) if isinstance(obj, dict) else getattr(obj, field, None)
