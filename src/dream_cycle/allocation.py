"""Attention allocation — make sure a run looks at the user's life, not one cluster.

Five pieces, all deterministic and unit-testable:

* :func:`build_arrivals_manifest` — the "what arrived since the previous run"
  block injected into the Explorer's user message: counts per channel and the
  newest documents in each, with ids the Explorer can pull directly.

* :func:`build_arrivals_inventory` — replaces the manifest when skim records
  exist for the window (docs/PLAN-skim-stage.md, Phase 3): the same header,
  then one line per document (``id | channel | S<salience> N<novelty> | flags |
  summary``) so the Explorer has read the whole day before choosing slices.
  Ordered by salience then novelty, channel-interleaved within a tier,
  capped by characters.

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
  Explorer ignores its instructions. With skim records
  (:func:`select_arrivals`) half of it goes to the highest-salience /
  highest-novelty documents first, the other half to the same stratified
  method over the rest — the important things guaranteed, serendipity
  preserved.

* :func:`order_slices` — the judging order for Step 6/7 (Phase 4): the
  guaranteed arrivals slice first, then the Explorer's slices by arrival
  share, so that under the spend cap the most recent material is judged
  before anything else is cut.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
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
INVENTORY_MAX_CHARS = 24_000
INVENTORY_SUMMARY_CHARS = 140

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
    """Render db.get_arrivals_summary() output for the Explorer's user message.

    An entry without a ``memories`` count (the inventory's fallback builds
    entries from document counts alone) is rendered without the memory figures.
    """
    if not arrivals:
        return (f"## What arrived since the previous run ({window_start:%Y-%m-%d %H:%M} UTC)\n"
                "Nothing new was captured in this window.")
    docs = sum(a["documents"] for a in arrivals)
    mems = sum(a["memories"] for a in arrivals) if all(a.get("memories") is not None for a in arrivals) else None
    lines = [
        f"## What arrived since the previous run ({window_start:%Y-%m-%d %H:%M} UTC)",
        (f"{docs} documents / {mems} memories" if mems is not None else f"{docs} documents")
        + f" across {len(arrivals)} channels. "
        "Ids below can be passed straight to memory_read; use memory_list(source_type=..., "
        "since_days=...) to see everything in a channel.",
        "",
    ]
    for a in arrivals:
        unit = CHANNEL_UNITS.get(a["source_type"], "documents")
        mem = f" ({a['memories']} memories)" if a.get("memories") is not None else ""
        lines.append(f"- {a['source_type']}: {a['documents']} {unit}{mem}")
        for s in a.get("samples", [])[:MANIFEST_SAMPLES_PER_SOURCE]:
            title = " ".join(str(s.get("title") or "").split())[:90]
            lines.append(f"    - {s.get('created_at', '')[:10]} {title} [{s.get('id')}]")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 12].rstrip() + "\n(truncated)"
    return text


# --------------------------------------------------------------------------- #
# Inventory (skim records)                                                     #
# --------------------------------------------------------------------------- #
INVENTORY_LEGEND = (
    "One line per document: id | channel | S<salience 0-3> N<novelty 0-3> | flags | summary. "
    "Flags: D<n> decisions, Q<n> open questions, C contradiction hint, P<n> people named, "
    "! a quote could not be verified against the text; [c<k>] documents whose embeddings cluster "
    "together. Every id can be passed straight to memory_read."
)


@dataclass(frozen=True)
class InventoryRender:
    """Result of :func:`render_arrivals_inventory`: the text and what it covers."""

    text: str
    listed: int      # document lines included
    total: int       # skim records available for the window
    fallback: bool = False   # no records → manifest-style output


def _skim_of(record: dict) -> dict:
    skim = record.get("skim")
    return skim if isinstance(skim, dict) else {}


def _score(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _record_flags(skim: dict) -> str:
    """Compact flags: ``D<n> Q<n> C P<n> !`` — only the parts that apply, ``-`` when none."""
    parts: list[str] = []
    decisions = skim.get("decisions") or []
    questions = skim.get("open_questions") or []
    people = skim.get("people") or []
    if decisions:
        parts.append(f"D{len(decisions)}")
    if questions:
        parts.append(f"Q{len(questions)}")
    if skim.get("contradiction_hint"):
        parts.append("C")
    if people:
        parts.append(f"P{len(people)}")
    unverified = _score(skim.get("quotes_unverified"))
    if not unverified:
        unverified = sum(1 for item in [*decisions, *questions] if isinstance(item, dict) and item.get("quote") is None)
    if unverified:
        parts.append("!")
    return " ".join(parts) if parts else "-"


def _trim_summary(summary: str, limit: int = INVENTORY_SUMMARY_CHARS) -> str:
    text = " ".join(str(summary or "").split())
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind(" ")
    if cut > limit // 2:
        head = head[:cut]
    return head.rstrip(" ,;:") + "…"


def _channel_of(record: dict) -> str:
    return record.get("source_type") or "generated"


def rank_skim_records(records: list[dict]) -> list[dict]:
    """The inventory order: salience desc, novelty desc; within one (S, N) tier
    the channels are interleaved round-robin (each channel newest first, channels
    by size then name) so a large channel cannot monopolise the top of a tier
    when scores tie. Deterministic; ``records`` is not modified."""
    tiers: dict[tuple[int, int], dict[str, list[dict]]] = {}
    for r in records:
        skim = _skim_of(r)
        key = (_score(skim.get("salience")), _score(skim.get("novelty")))
        tiers.setdefault(key, {}).setdefault(_channel_of(r), []).append(r)
    out: list[dict] = []
    for key in sorted(tiers, reverse=True):
        by_channel = tiers[key]
        queues = [sorted(by_channel[ch], key=lambda r: (str(r.get("created_at") or ""), str(r.get("id"))), reverse=True)
                  for ch in sorted(by_channel, key=lambda c: (-len(by_channel[c]), c))]
        while queues:
            queues = [q for q in queues if q]
            for q in queues:
                out.append(q.pop(0))
    return out


def inventory_line(record: dict) -> str:
    """``<id> | <channel> | S<s> N<n> | <flags> | <summary>`` (+ `` [c<k>]`` when clustered)."""
    skim = _skim_of(record)
    line = (f"{record.get('id')} | {_channel_of(record)} | S{_score(skim.get('salience'))} "
            f"N{_score(skim.get('novelty'))} | {_record_flags(skim)} | {_trim_summary(skim.get('summary', ''))}")
    cluster = record.get("cluster_id")
    if cluster:
        line += f" [{cluster}]"
    return line


def render_arrivals_inventory(window_start: datetime, records: list[dict], *,
                              counts_by_channel: dict[str, int],
                              max_chars: int = INVENTORY_MAX_CHARS) -> InventoryRender:
    """Build the inventory and report how many documents it lists.

    ``records`` are ``store.fetch_window_records`` rows (``id, title, source_type,
    created_at, skim, cluster_id``); ``counts_by_channel`` are the window's
    arrival counts per channel (all arrivals, skimmed or not). With no records
    the output is :func:`build_arrivals_manifest` built from the counts.
    """
    if not records:
        arrivals = [{"source_type": ch, "documents": n, "samples": []}
                    for ch, n in sorted(counts_by_channel.items(), key=lambda kv: (-kv[1], kv[0])) if n]
        text = build_arrivals_manifest(window_start, arrivals, max_chars=max_chars)
        return InventoryRender(text=text, listed=0, total=0, fallback=True)

    counts = {ch: n for ch, n in counts_by_channel.items() if n}
    if not counts:
        counts = dict(Counter(_channel_of(r) for r in records))
    docs = sum(counts.values())
    header = [
        f"## What arrived since the previous run ({window_start:%Y-%m-%d %H:%M} UTC)",
        f"{docs} documents across {len(counts)} channels; {len(records)} of them were read by the skim stage "
        "and are listed below, most salient first. "
        "Use memory_list(source_type=..., since_days=...) to see everything in a channel.",
        "",
    ]
    for ch, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        header.append(f"- {ch}: {n} {CHANNEL_UNITS.get(ch, 'documents')}")
    header += ["", INVENTORY_LEGEND, ""]

    ranked = rank_skim_records(records)
    lines = list(header)
    length = sum(len(l) + 1 for l in lines)
    footer_reserve = len(_inventory_footer(len(ranked))) + 1
    listed = 0
    for r in ranked:
        line = inventory_line(r)
        if length + len(line) + 1 > max_chars - footer_reserve:
            break
        lines.append(line)
        length += len(line) + 1
        listed += 1
    if listed < len(ranked):
        lines.append(_inventory_footer(len(ranked) - listed))
    return InventoryRender(text="\n".join(lines), listed=listed, total=len(ranked))


def _inventory_footer(k: int) -> str:
    return f"… {k} more documents not listed (all ids are retrievable by tools)"


def build_arrivals_inventory(window_start: datetime, records: list[dict], *,
                             counts_by_channel: dict[str, int],
                             max_chars: int = INVENTORY_MAX_CHARS) -> str:
    """The Explorer's arrivals block when skim records exist — see
    :func:`render_arrivals_inventory`; returns the text only."""
    return render_arrivals_inventory(window_start, records, counts_by_channel=counts_by_channel,
                                     max_chars=max_chars).text


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
def allocate_stratified(counts: dict[str, int], size: int, per_source_cap: int = ARRIVALS_PER_SOURCE_CAP,
                        caps: dict[str, int] | None = None) -> dict[str, int]:
    """Split ``size`` slots across channels ∝ sqrt(count), capped per channel.

    sqrt keeps a 192-document feed from crowding out 7 browsing days; the cap
    keeps any one channel from dominating; leftovers go to the largest channels.
    ``caps`` overrides the cap for individual channels (the skim-weighted slice
    passes the room left after its salience picks); a channel with no room is
    skipped.
    """
    def cap_of(k: str) -> int:
        return caps.get(k, per_source_cap) if caps else per_source_cap

    channels = {k: v for k, v in counts.items() if v > 0 and cap_of(k) > 0}
    if not channels or size <= 0:
        return {}
    weights = {k: math.sqrt(v) for k, v in channels.items()}
    total_w = sum(weights.values())
    alloc = {k: min(cap_of(k), channels[k], int(size * w / total_w)) for k, w in weights.items()}
    # Every channel with something gets at least one slot while slots remain.
    for k in sorted(channels, key=lambda c: -channels[c]):
        if sum(alloc.values()) >= size:
            break
        if alloc[k] == 0:
            alloc[k] = 1
    # Distribute leftovers to the largest channels under their caps.
    for k in sorted(channels, key=lambda c: -channels[c]):
        while sum(alloc.values()) < size and alloc[k] < min(cap_of(k), channels[k]):
            alloc[k] += 1
        if sum(alloc.values()) >= size:
            break
    return {k: v for k, v in alloc.items() if v > 0}


def _channel_label(row: dict) -> str:
    return row.get("source_type") or f"generated:{row.get('type')}"


def _stratified_rows(candidates: list[dict], size: int, taken: Counter | None = None,
                     semantic_taken: int = 0) -> list[dict]:
    """The original guaranteed-slice selection: sqrt-stratified across channels,
    episodic first, at most MAX_SEMANTIC_IN_ARRIVALS generated memories.

    ``taken`` / ``semantic_taken`` describe rows already chosen by another step
    (the skim-weighted half) so the per-channel and semantic caps hold for the
    slice as a whole; without them this is exactly the historical selection.
    """
    by_channel: dict[str, list[dict]] = {}
    for r in candidates:
        by_channel.setdefault(_channel_label(r), []).append(r)
    episodic = {k: v for k, v in by_channel.items() if not k.startswith("generated:")}
    semantic = {k: v for k, v in by_channel.items() if k.startswith("generated:")}
    caps = {k: ARRIVALS_PER_SOURCE_CAP - taken.get(k, 0) for k in by_channel} if taken else None

    chosen: list[dict] = []
    for k, n in allocate_stratified({k: len(v) for k, v in episodic.items()}, size, caps=caps).items():
        chosen += episodic[k][:n]
    if len(chosen) < size and semantic:
        room = min(MAX_SEMANTIC_IN_ARRIVALS - semantic_taken, size - len(chosen))
        for k, n in allocate_stratified({k: len(v) for k, v in semantic.items()}, room, caps=caps).items():
            chosen += semantic[k][:n]
    return chosen


def _fill_capped(chosen: list[dict], pool: list[dict], size: int) -> int:
    """Append rows from ``pool`` (already ranked) to ``chosen`` until ``size``,
    skipping ids already chosen, channels at ARRIVALS_PER_SOURCE_CAP and generated
    memories once MAX_SEMANTIC_IN_ARRIVALS is reached. Returns how many were added."""
    have = {str(r["id"]) for r in chosen}
    per_channel = Counter(_channel_label(r) for r in chosen)
    semantic = sum(1 for r in chosen if _channel_label(r).startswith("generated:"))
    added = 0
    for r in pool:
        if len(chosen) >= size:
            break
        rid = str(r["id"])
        if rid in have:
            continue
        label = _channel_label(r)
        if per_channel[label] >= ARRIVALS_PER_SOURCE_CAP:
            continue
        if label.startswith("generated:"):
            if semantic >= MAX_SEMANTIC_IN_ARRIVALS:
                continue
            semantic += 1
        chosen.append(r)
        have.add(rid)
        per_channel[label] += 1
        added += 1
    return added


@dataclass(frozen=True)
class ArrivalsSelection:
    """What :func:`select_arrivals` picked for the guaranteed slice and how."""

    rows: list[dict] = field(default_factory=list)
    skim_weighted: bool = False   # skim records were available and used
    skim_picked: int = 0          # rows chosen for salience / novelty (not by stratification)

    @property
    def metrics(self) -> dict:
        """Extra keys for the slice's ledger metrics (empty when not weighted)."""
        return {"skim_weighted": True, "skim_picked": self.skim_picked} if self.skim_weighted else {}


def select_arrivals(candidates: list[dict], size: int = ARRIVALS_SLICE_SIZE,
                    skim_records: list[dict] | None = None) -> ArrivalsSelection:
    """Choose the guaranteed slice's rows from ``candidates`` (parent-level
    memories in the window, newest first; ``id, title, type, source_type,
    created_at``).

    Without ``skim_records`` this is exactly the stratified selection the slice
    has always used. With records (``store.fetch_window_records`` rows), the
    highest-salience, highest-novelty documents (:func:`rank_skim_records`
    order) fill ``size // 2`` slots first, then the stratified method fills the
    rest from the documents not yet chosen — both halves under
    ARRIVALS_PER_SOURCE_CAP per channel and MAX_SEMANTIC_IN_ARRIVALS generated
    memories for the slice as a whole. Salience goes first so a channel's
    newest documents can never crowd out its important ones; a partial skim
    (few records) simply leaves more slots to the stratified half, so the
    slice never shrinks because the skim was incomplete.
    """
    if not candidates:
        return ArrivalsSelection()
    if not skim_records:
        return ArrivalsSelection(rows=_stratified_rows(candidates, size))

    by_id = {str(r["id"]): r for r in candidates}
    ranked = [by_id[str(rec["id"])] for rec in rank_skim_records(skim_records) if str(rec["id"]) in by_id]
    chosen: list[dict] = []
    picked = _fill_capped(chosen, ranked, size // 2)
    have = {str(r["id"]) for r in chosen}
    rest = [r for r in candidates if str(r["id"]) not in have]
    chosen += _stratified_rows(
        rest, size - len(chosen),
        taken=Counter(_channel_label(r) for r in chosen),
        semantic_taken=sum(1 for r in chosen if _channel_label(r).startswith("generated:")),
    )
    return ArrivalsSelection(rows=chosen, skim_weighted=True, skim_picked=picked)


def slice_from_rows(window_start: datetime, rows: list[dict]) -> MemorySlice | None:
    """Wrap chosen rows as the guaranteed arrivals slice."""
    if not rows:
        return None
    channels = sorted({_channel_label(r) for r in rows})
    return MemorySlice(
        name=f"Arrivals since {window_start:%Y-%m-%d}: {', '.join(channels)}",
        strategy=ARRIVALS_STRATEGY,
        memory_ids=[str(r["id"]) for r in rows],
        memory_titles=[str(r.get("title") or "")[:200] for r in rows],
        hypothesis=("What happened in the user's life since the previous run, across channels. "
                    "Look for echoes between channels (a Slack thread and an email about the same account; "
                    "a meeting and the reading before it), commitments made, questions left open, and "
                    "things the user should notice — not abstract principles."),
    )


def compose_arrivals_slice(window_start: datetime, candidates: list[dict],
                           size: int = ARRIVALS_SLICE_SIZE,
                           skim_records: list[dict] | None = None) -> MemorySlice | None:
    """Build the guaranteed slice from parent-level memories created in the window.

    ``candidates``: rows with id, title, type, source_type, created_at (newest
    first). Episodic memories (type 'source') are preferred; at most
    MAX_SEMANTIC_IN_ARRIVALS generated/semantic memories are included so the
    slice satisfies the evidence-independence composition rule. See
    :func:`select_arrivals` for the skim-weighted variant.
    """
    return slice_from_rows(window_start, select_arrivals(candidates, size, skim_records).rows)


def build_arrivals_selection(window_start: datetime, size: int = ARRIVALS_SLICE_SIZE, dcdb=None,
                             skim_records: list[dict] | None = None) -> tuple[MemorySlice | None, ArrivalsSelection]:
    """DB-backed selection (reads via dream_cycle_db): the slice and how it was picked."""
    if dcdb is None:
        import src.dream_cycle_db as dcdb
    rows = dcdb.get_recent_parent_memories(window_start)
    selection = select_arrivals(rows if isinstance(rows, list) else [], size, skim_records)
    return slice_from_rows(window_start, selection.rows), selection


def build_arrivals_slice(window_start: datetime, size: int = ARRIVALS_SLICE_SIZE, dcdb=None,
                         skim_records: list[dict] | None = None) -> MemorySlice | None:
    """DB-backed wrapper around :func:`compose_arrivals_slice` (reads via dream_cycle_db)."""
    return build_arrivals_selection(window_start, size, dcdb, skim_records)[0]
