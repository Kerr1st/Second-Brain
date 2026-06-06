"""Express — the delivery layer (Partnership rung).

The system already produces synthesis (dream-cycle insights, contradictions,
the weekly digest) but never surfaces it. Express closes that loop:

  compose_briefing()  — gather candidate items from the 5 synthesis sources (pure read)
  edit_briefing()     — LLM editor pass: rank, write one headline each, pick the lead
                        (falls back to a deterministic ordering if the LLM is unavailable)
  render_markdown()   — format a briefing for the terminal (P1 `brief`)

P2 (proactive Gmail push) builds on the same compose/edit and adds should_push()
+ render_email() + send_email(). See docs/EXPRESS-PLAN.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from src.db import get_connection

logger = logging.getLogger(__name__)

# --- Locked defaults (see EXPRESS-PLAN.md "Locked decisions") ---
INSIGHT_WINDOW_DAYS = 14        # rolling window for the on-demand brief
CONTRADICTION_LIMIT = 3
RESURFACE_LIMIT = 3
RESURFACE_MIN_AGE_DAYS = 30     # Bjork desirable difficulty: old (high storage)...
RESURFACE_SUPPRESS_DAYS = 14    # ...not accessed recently (low retrieval); no repeats within ~2wk
QUESTION_LIMIT = 2
MAX_ITEMS = 5                   # lead + up to 4

# Kind priority for the deterministic fallback (lower = more important).
_KIND_PRIORITY = {"contradiction": 0, "insight": 1, "resurface": 2, "digest": 3, "question": 4}


def _truncate(text: str | None, n: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _gather_insights(cur, window_days: int) -> list[dict]:
    """Recent accepted dream-cycle insights (identified by the `dream-cycle` tag)."""
    cur.execute(
        """
        SELECT id, type, title, content, created_at, metadata, project
        FROM memories
        WHERE tags @> ARRAY['dream-cycle'] AND status = 'active'
          AND created_at > now() - (%s || ' days')::interval
        ORDER BY created_at DESC
        LIMIT 8
        """,
        (window_days,),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    # Resolve source-memory projects/titles in one batched query → cross-project signal.
    all_src: set[str] = set()
    for r in rows:
        for sid in (r["metadata"] or {}).get("source_memories", []) or []:
            all_src.add(sid)
    src_info: dict[str, dict] = {}
    if all_src:
        cur.execute(
            "SELECT id, title, project FROM memories WHERE id = ANY(%s::uuid[])",
            (list(all_src),),
        )
        for s in cur.fetchall():
            src_info[str(s["id"])] = {"title": s["title"], "project": s["project"]}

    items = []
    for r in rows:
        meta = r["metadata"] or {}
        src_ids = meta.get("source_memories", []) or []
        projects = {
            src_info[s]["project"]
            for s in src_ids
            if s in src_info and src_info[s]["project"]
        }
        source_titles = [src_info[s]["title"] for s in src_ids if s in src_info]
        cross_project = (
            meta.get("strategy") == "cross_project_collision" or len(projects) >= 2
        )
        items.append(
            {
                "kind": "insight",
                "id": str(r["id"]),
                "title": r["title"],
                "detail": _truncate(r["content"]),
                "created_at": r["created_at"],
                "meta": {
                    "strategy": meta.get("strategy"),
                    "confidence": meta.get("confidence"),
                    "cross_project": cross_project,
                    "projects": sorted(projects),
                    "source_titles": source_titles[:5],
                },
            }
        )
    return items


def _gather_contradictions(cur, limit: int) -> list[dict]:
    """Active `contradicts` edges — the 'you're reversing a past decision' signal.

    Deduped to one entry per source memory (a memory may contradict several others;
    showing it once, newest-first, keeps the briefing varied).
    """
    cur.execute(
        """
        SELECT r.source_id, r.target_id, r.note, r.created_at,
               a.title AS a_title, b.title AS b_title
        FROM memory_relationships r
        JOIN memories a ON a.id = r.source_id
        JOIN memories b ON b.id = r.target_id
        WHERE r.relation_type = 'contradicts' AND r.expired_at IS NULL
        ORDER BY r.created_at DESC
        LIMIT %s
        """,
        (limit * 4,),
    )
    items = []
    seen_sources: set = set()
    for r in cur.fetchall():
        if r["source_id"] in seen_sources:
            continue
        seen_sources.add(r["source_id"])
        note = (r["note"] or "").strip()
        detail = note or "These two memories make opposing claims."
        detail += f"\n\n• «{r['a_title']}»\n• «{r['b_title']}»"
        items.append(
            {
                "kind": "contradiction",
                "id": f"{r['source_id']}:{r['target_id']}",
                "title": r["a_title"],
                "detail": detail,
                "created_at": r["created_at"],
                "meta": {"a_title": r["a_title"], "b_title": r["b_title"], "note": note},
            }
        )
        if len(items) >= limit:
            break
    return items


def _gather_resurfaced(cur, limit: int, suppress_days: int) -> list[dict]:
    """High-value, long-unaccessed memories (desirable difficulty; Bjork 1992).

    Value signal is `access_count` (proven prior retrieval = high storage strength)
    on distilled types; depth_score is only populated on dream-cycle memories so it
    is used only as a tiebreaker. "Forgotten" = old + not accessed recently.
    """
    cur.execute(
        """
        SELECT id, type, title, content, created_at, access_count,
               COALESCE((metadata->>'depth_score')::float, 0) AS depth
        FROM memories
        WHERE status = 'active'
          AND type IN ('insight', 'decision', 'synthesis')
          AND NOT (tags @> ARRAY['dream-cycle'])
          AND created_at < now() - (%s || ' days')::interval
          AND (last_accessed_at IS NULL OR last_accessed_at < now() - (%s || ' days')::interval)
          AND (
                metadata->>'express_last_surfaced' IS NULL
                OR (metadata->>'express_last_surfaced')::timestamptz < now() - (%s || ' days')::interval
              )
        ORDER BY access_count DESC, depth DESC, created_at ASC
        LIMIT %s
        """,
        (RESURFACE_MIN_AGE_DAYS, RESURFACE_MIN_AGE_DAYS, suppress_days, limit),
    )
    items = []
    for r in cur.fetchall():
        items.append(
            {
                "kind": "resurface",
                "id": str(r["id"]),
                "title": r["title"],
                "detail": _truncate(r["content"]),
                "created_at": r["created_at"],
                "meta": {"depth": round(r["depth"], 2), "access_count": r["access_count"]},
            }
        )
    return items


def _gather_digest(cur) -> list[dict]:
    """The latest weekly digest ('what you're working on / learning')."""
    cur.execute(
        """
        SELECT id, title, content, created_at
        FROM memories
        WHERE source_type = 'weekly_digest'
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    r = cur.fetchone()
    if not r:
        return []
    return [
        {
            "kind": "digest",
            "id": str(r["id"]),
            "title": r["title"],
            "detail": _truncate(r["content"]),
            "created_at": r["created_at"],
            "meta": {},
        }
    ]


def _gather_questions(cur, limit: int) -> list[dict]:
    """Active open questions (currently none in the store; future-proofed)."""
    cur.execute(
        """
        SELECT id, title, content, created_at
        FROM memories
        WHERE type = 'question' AND status = 'active'
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            "kind": "question",
            "id": str(r["id"]),
            "title": r["title"],
            "detail": _truncate(r["content"]),
            "created_at": r["created_at"],
            "meta": {},
        }
        for r in cur.fetchall()
    ]


def compose_briefing(
    window_days: int = INSIGHT_WINDOW_DAYS,
    suppress_days: int = RESURFACE_SUPPRESS_DAYS,
) -> dict:
    """Gather candidate items from the five synthesis sources. Pure read.

    Returns:
        {"generated_at": iso, "items": [item, ...], "counts": {kind: n}}
        where each item is {kind, id, title, detail, created_at, meta}.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            items: list[dict] = []
            items += _gather_insights(cur, window_days)
            items += _gather_contradictions(cur, CONTRADICTION_LIMIT)
            items += _gather_resurfaced(cur, RESURFACE_LIMIT, suppress_days)
            items += _gather_digest(cur)
            items += _gather_questions(cur, QUESTION_LIMIT)

    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "counts": counts,
    }


# --- Editor pass ---------------------------------------------------------------

_EDITOR_SYSTEM = (
    "You are the editor of a personal-knowledge briefing — the daily voice of the "
    "user's 'second brain'. You are given candidate items the system synthesized "
    "(insights it derived, contradictions it detected between past decisions, "
    "high-value things the user has forgotten, and activity digests). Your job: "
    "decide what is worth the user's attention, in what order.\n\n"
    "Rules:\n"
    "- Rank by importance. A CROSS-PROJECT synthesis (an idea connecting separate "
    "projects) or a CONTRADICTION (the user reversing/conflicting with a past "
    "decision) is the most valuable — lead with the single strongest such item.\n"
    "- Keep at most 5 items. Drop the weak ones; fewer is better than padding.\n"
    "- For each kept item write ONE punchy headline (<= 90 chars) that captures the "
    "idea so the user instantly knows if they want the detail. No clickbait; be concrete.\n"
    "- Output ONLY a JSON object (no prose, no code fence): "
    '{"lead": <idx>, "items": [{"idx": <int>, "headline": "<text>"}, ...]} '
    "ordered best-first, where idx refers to the candidate index you were given. "
    "lead must equal items[0].idx."
)


def _editor_user_message(items: list[dict]) -> str:
    lines = ["Candidate items (idx | kind | title | signal | detail):"]
    for i, it in enumerate(items):
        m = it["meta"]
        signal = ""
        if it["kind"] == "insight":
            signal = "CROSS-PROJECT" if m.get("cross_project") else "single-project"
            if m.get("projects"):
                signal += f" {m['projects']}"
        elif it["kind"] == "contradiction":
            signal = "CONTRADICTION"
        elif it["kind"] == "resurface":
            signal = f"forgotten (depth={m.get('depth')}, accessed {m.get('access_count')}x)"
        lines.append(
            f"\n[{i}] {it['kind']} | {it['title']} | {signal}\n{_truncate(it['detail'], 400)}"
        )
    return "\n".join(lines)


def _deterministic_edit(items: list[dict]) -> dict:
    """Fallback ordering when the LLM editor is unavailable.

    Contradictions and cross-project insights float to the top; headline = title.
    """
    def sort_key(it):
        kp = _KIND_PRIORITY.get(it["kind"], 9)
        if it["kind"] == "insight" and it["meta"].get("cross_project"):
            kp = -1  # cross-project insight outranks everything
        return (kp, -(it["created_at"].timestamp() if it.get("created_at") else 0))

    ordered = sorted(items, key=sort_key)[:MAX_ITEMS]
    for it in ordered:
        it["headline"] = it["title"]
    return {"items": ordered, "lead_idx": 0 if ordered else None, "editor": "deterministic"}


def edit_briefing(briefing: dict, invoker=None) -> dict:
    """Rank items, attach one headline each, pick the lead.

    Uses an LLM editor pass via AgentInvoker; on any failure (no kiro-cli, parse
    error, timeout) falls back to a deterministic ordering so `brief` always works.

    Returns the briefing dict augmented with an ordered "ranked" list (each item
    gains a "headline") and "lead_idx"/"editor" markers.
    """
    items = briefing.get("items", [])
    if not items:
        briefing["ranked"] = []
        briefing["lead_idx"] = None
        briefing["editor"] = "empty"
        return briefing

    ranked: dict | None = None
    try:
        if invoker is None:
            from src.agent_invoker import AgentInvoker

            invoker = AgentInvoker()
        res = invoker.invoke(_EDITOR_SYSTEM, _editor_user_message(items), timeout=180)
        out = res["output"] if isinstance(res.get("output"), dict) else {}
        chosen = out.get("items") or []
        ordered = []
        for entry in chosen:
            idx = entry.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(items):
                it = dict(items[idx])
                it["headline"] = (entry.get("headline") or it["title"]).strip()[:120]
                ordered.append(it)
        if ordered:
            ranked = {"items": ordered[:MAX_ITEMS], "lead_idx": 0, "editor": "llm"}
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash the brief
        logger.warning("Editor LLM pass failed (%s); using deterministic fallback", exc)

    if ranked is None:
        ranked = _deterministic_edit(items)

    briefing["ranked"] = ranked["items"]
    briefing["lead_idx"] = ranked["lead_idx"]
    briefing["editor"] = ranked["editor"]
    return briefing


# --- Rendering -----------------------------------------------------------------

_KIND_LABEL = {
    "insight": "💡 Insight",
    "contradiction": "⚠️  Contradiction",
    "resurface": "🔁 Worth revisiting",
    "digest": "📊 Recent activity",
    "question": "❓ Open question",
}


def render_markdown(briefing: dict) -> str:
    """Render a composed+edited briefing as scannable Markdown (headlines, then detail)."""
    ranked = briefing.get("ranked", [])
    if not ranked:
        return "# Your briefing\n\n_Nothing worth surfacing right now — the brain is quiet._\n"

    lines = ["# Your briefing", ""]
    # Scan layer: the headlines.
    for i, it in enumerate(ranked):
        marker = "★" if i == 0 else "•"
        lines.append(f"{marker} **{it['headline']}**  _( {_KIND_LABEL.get(it['kind'], it['kind'])} )_")
    lines.append("")
    lines.append("---")
    # Detail layer.
    for i, it in enumerate(ranked):
        lines.append("")
        lines.append(f"### {'★ ' if i == 0 else ''}{it['headline']}")
        meta_bits = []
        if it["kind"] == "insight" and it["meta"].get("projects"):
            meta_bits.append("across " + ", ".join(it["meta"]["projects"]))
        if it["kind"] == "resurface":
            meta_bits.append(f"last touched a while ago · depth {it['meta'].get('depth')}")
        if meta_bits:
            lines.append(f"_{' · '.join(meta_bits)}_")
        lines.append("")
        lines.append(it["detail"])
        if it["kind"] == "insight" and it["meta"].get("source_titles"):
            lines.append("")
            lines.append("Drawn from: " + "; ".join(f"«{t}»" for t in it["meta"]["source_titles"]))
    lines.append("")
    return "\n".join(lines)
