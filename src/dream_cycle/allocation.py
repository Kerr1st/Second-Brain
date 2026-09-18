"""Attention allocation — make sure a run looks at the user's life, not one cluster.

Three pieces, all deterministic and unit-testable:

* :func:`build_arrivals_manifest` — the "what arrived since the previous run"
  block injected into the Explorer's user message: counts per channel and the
  newest documents in each, with ids the Explorer can pull directly.

* :func:`enforce_budget` — validates the Explorer's slices against the breadth
  rules in the prompt and decides whether the orchestrator must add a slice of
  its own. Rules (see prompts/explorer.py "Slice budget"):
    - a slice with no resolvable memories is dropped;
    - at least ``required_anchored(n)`` slices must be *arrival-anchored*
      (>= ANCHOR_ARRIVAL_SHARE of memories created since the previous run AND
      >= ANCHOR_MIN_SOURCES channels); if not, one guaranteed arrivals slice is
      added;
    - no single channel may supply every slice: when three or more slices share
      a dominant channel at >= CONCENTRATION_LIMIT and none is anchored, only
      the first two are kept.
  Nothing here judges topics. Work on the Second Brain itself is part of the
  user's day and competes on equal footing; it simply cannot be the only thing.

* :func:`build_arrivals_slice` — the guaranteed slice: a stratified sample of
  parent-level memories created since the previous run, spread across channels
  (sqrt-proportional so small channels are represented, capped per channel),
  episodic first. Exists so every run examines new material even when the
  Explorer ignores its instructions.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime

from src.models import MemorySlice

logger = logging.getLogger(__name__)

ANCHOR_ARRIVAL_SHARE = 0.6
ANCHOR_MIN_SOURCES = 2
CONCENTRATION_LIMIT = 0.8
ARRIVALS_STRATEGY = "arrivals_synthesis"
ARRIVALS_ORIGIN = "guaranteed_arrivals"
ARRIVALS_SLICE_SIZE = 14
ARRIVALS_PER_SOURCE_CAP = 4
MAX_SEMANTIC_IN_ARRIVALS = 3
MANIFEST_SAMPLES_PER_SOURCE = 4
MANIFEST_MAX_CHARS = 7000

# Channel → what one document is, for the manifest and the prompt.
CHANNEL_UNITS = {
    "slack": "conversation-days", "outlook_email": "emails", "outlook_calendar": "calendar events",
    "browser_history": "browsing days", "article": "articles / web pages", "youtube": "video transcripts",
    "kiro_cli_chat": "Kiro CLI sessions", "kiro_ide_chat": "Kiro IDE sessions",
    "distilled_chat": "distilled session insights", "weekly_digest": "weekly digests",
}


def required_anchored(n_slices: int) -> int:
    """At least half the slices, minimum one (when there is at least one slice)."""
    return math.ceil(n_slices / 2) if n_slices else 0


def is_anchored(metrics: dict) -> bool:
    return (metrics.get("arrival_share", 0.0) >= ANCHOR_ARRIVAL_SHARE
            and metrics.get("source_types", 0) >= ANCHOR_MIN_SOURCES)


# --------------------------------------------------------------------------- #
# Manifest                                                                     #
# --------------------------------------------------------------------------- #
def build_arrivals_manifest(window_start: datetime, arrivals: list[dict],
                            max_chars: int = MANIFEST_MAX_CHARS) -> str:
    """Render db.get_arrivals_summary() output for the Explorer's user message."""
    if not arrivals:
        return (f"## What arrived since the previous run ({window_start:%Y-%m-%d %H:%M} UTC)\n"
                "Nothing new was captured in this window.")
    docs = sum(a["documents"] for a in arrivals)
    mems = sum(a["memories"] for a in arrivals)
    lines = [
        f"## What arrived since the previous run ({window_start:%Y-%m-%d %H:%M} UTC)",
        f"{docs} documents / {mems} memories across {len(arrivals)} channels. "
        "Ids below can be passed straight to memory_read; use memory_list(source_type=..., "
        "since_days=...) to see everything in a channel.",
        "",
    ]
    for a in arrivals:
        unit = CHANNEL_UNITS.get(a["source_type"], "documents")
        lines.append(f"- {a['source_type']}: {a['documents']} {unit} ({a['memories']} memories)")
        for s in a.get("samples", [])[:MANIFEST_SAMPLES_PER_SOURCE]:
            title = " ".join(str(s.get("title") or "").split())[:90]
            lines.append(f"    - {s.get('created_at', '')[:10]} {title} [{s.get('id')}]")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 12].rstrip() + "\n(truncated)"
    return text


# --------------------------------------------------------------------------- #
# Budget enforcement                                                           #
# --------------------------------------------------------------------------- #
def enforce_budget(slices: list[MemorySlice], metrics: list[dict]) -> tuple[list[MemorySlice], list[dict], bool]:
    """Apply the breadth rules.

    Returns (kept_slices, kept_metrics, needs_arrivals_slice) plus logs every
    decision. ``metrics[i]`` is ledger.slice_metrics for ``slices[i]``.
    """
    kept: list[tuple[MemorySlice, dict]] = []
    for s, m in zip(slices, metrics):
        if m.get("resolved", 0) == 0:
            logger.warning("Dropping slice %r: none of its %d memory ids resolve", s.name, m.get("requested", 0))
            continue
        kept.append((s, m))

    # Concentration: three or more unanchored slices from one dominant channel → keep two.
    by_source: Counter = Counter(
        m["dominant_source"] for _, m in kept
        if m.get("concentration", 0) >= CONCENTRATION_LIMIT and not is_anchored(m))
    for source, count in by_source.items():
        if count >= 3:
            seen = 0
            trimmed = []
            for s, m in kept:
                same = (m.get("dominant_source") == source and m.get("concentration", 0) >= CONCENTRATION_LIMIT
                        and not is_anchored(m))
                if same:
                    seen += 1
                    if seen > 2:
                        logger.warning("Dropping slice %r: %d slices already drawn almost entirely from %s",
                                       s.name, 2, source)
                        continue
                trimmed.append((s, m))
            kept = trimmed

    anchored = sum(1 for _, m in kept if is_anchored(m))
    need = required_anchored(len(kept)) if kept else 1
    needs_arrivals = anchored < need
    logger.info("Slice budget: %d slices kept (%d dropped), %d anchored in the arrivals window, %d required%s",
                len(kept), len(slices) - len(kept), anchored, need,
                " — adding a guaranteed arrivals slice" if needs_arrivals else "")
    return [s for s, _ in kept], [m for _, m in kept], needs_arrivals


# --------------------------------------------------------------------------- #
# Guaranteed arrivals slice                                                    #
# --------------------------------------------------------------------------- #
def allocate_stratified(counts: dict[str, int], size: int, per_source_cap: int = ARRIVALS_PER_SOURCE_CAP) -> dict[str, int]:
    """Split ``size`` slots across channels ∝ sqrt(count), capped per channel.

    sqrt keeps a 192-document feed from crowding out 7 browsing days; the cap
    keeps any one channel from dominating; leftovers go to the largest channels.
    """
    channels = {k: v for k, v in counts.items() if v > 0}
    if not channels or size <= 0:
        return {}
    weights = {k: math.sqrt(v) for k, v in channels.items()}
    total_w = sum(weights.values())
    alloc = {k: min(per_source_cap, channels[k], int(size * w / total_w)) for k, w in weights.items()}
    # Every channel with something gets at least one slot while slots remain.
    for k in sorted(channels, key=lambda c: -channels[c]):
        if sum(alloc.values()) >= size:
            break
        if alloc[k] == 0:
            alloc[k] = 1
    # Distribute leftovers to the largest channels under their caps.
    for k in sorted(channels, key=lambda c: -channels[c]):
        while sum(alloc.values()) < size and alloc[k] < min(per_source_cap, channels[k]):
            alloc[k] += 1
        if sum(alloc.values()) >= size:
            break
    return {k: v for k, v in alloc.items() if v > 0}


def compose_arrivals_slice(window_start: datetime, candidates: list[dict],
                           size: int = ARRIVALS_SLICE_SIZE) -> MemorySlice | None:
    """Build the guaranteed slice from parent-level memories created in the window.

    ``candidates``: rows with id, title, type, source_type, created_at (newest
    first). Episodic memories (type 'source') are preferred; at most
    MAX_SEMANTIC_IN_ARRIVALS generated/semantic memories are included so the
    slice satisfies the evidence-independence composition rule.
    """
    if not candidates:
        return None
    by_channel: dict[str, list[dict]] = {}
    for r in candidates:
        label = r.get("source_type") or f"generated:{r.get('type')}"
        by_channel.setdefault(label, []).append(r)
    episodic = {k: v for k, v in by_channel.items() if not k.startswith("generated:")}
    semantic = {k: v for k, v in by_channel.items() if k.startswith("generated:")}

    chosen: list[dict] = []
    for k, n in allocate_stratified({k: len(v) for k, v in episodic.items()}, size).items():
        chosen += episodic[k][:n]
    if len(chosen) < size and semantic:
        room = min(MAX_SEMANTIC_IN_ARRIVALS, size - len(chosen))
        for k, n in allocate_stratified({k: len(v) for k, v in semantic.items()}, room).items():
            chosen += semantic[k][:n]
    if not chosen:
        return None
    channels = sorted({(r.get("source_type") or f"generated:{r.get('type')}") for r in chosen})
    return MemorySlice(
        name=f"Arrivals since {window_start:%Y-%m-%d}: {', '.join(channels)}",
        strategy=ARRIVALS_STRATEGY,
        memory_ids=[str(r["id"]) for r in chosen],
        memory_titles=[str(r.get("title") or "")[:200] for r in chosen],
        hypothesis=("What happened in the user's life since the previous run, across channels. "
                    "Look for echoes between channels (a Slack thread and an email about the same account; "
                    "a meeting and the reading before it), commitments made, questions left open, and "
                    "things the user should notice — not abstract principles."),
    )


def build_arrivals_slice(window_start: datetime, size: int = ARRIVALS_SLICE_SIZE, dcdb=None) -> MemorySlice | None:
    """DB-backed wrapper around :func:`compose_arrivals_slice` (reads via dream_cycle_db)."""
    if dcdb is None:
        import src.dream_cycle_db as dcdb
    rows = dcdb.get_recent_parent_memories(window_start)
    return compose_arrivals_slice(window_start, rows if isinstance(rows, list) else [], size)
