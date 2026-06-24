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
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

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

# --- Feedback (delivery preferences; migration 010) ---
KINDS = {"insight", "contradiction", "resurface", "digest", "question"}
VALID_SIGNALS = {"useful", "less", "mute"}
# Soft ranking effect for the non-mute signals (mute is a hard filter, weight unused).
_SIGNAL_WEIGHT = {"useful": 1.0, "less": -0.6, "mute": 0.0}


def _classify_target(target: str) -> tuple[str, str]:
    """Classify a feedback target as ('kind'|'item'|'topic', normalized_key).

    A known kind keyword → kind; an 8+ hex string (uuid / uuid-prefix / 'src:tgt')
    → item; anything else → topic/project.
    """
    t = target.strip()
    if t.lower() in KINDS:
        return ("kind", t.lower())
    core = t.replace("-", "").replace(":", "")
    if len(core) >= 8 and re.fullmatch(r"[0-9a-fA-F]+", core):
        return ("item", t.lower())
    return ("topic", t)


def record_feedback(target: str, signal: str) -> dict:
    """Upsert a delivery-preference signal (latest per target wins)."""
    if signal not in VALID_SIGNALS:
        raise ValueError(f"signal must be one of {sorted(VALID_SIGNALS)}")
    ttype, tkey = _classify_target(target)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO express_feedback (target_type, target_key, signal, weight)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (target_type, target_key)
                DO UPDATE SET signal = EXCLUDED.signal, weight = EXCLUDED.weight, updated_at = now()
                """,
                (ttype, tkey, signal, _SIGNAL_WEIGHT[signal]),
            )
        conn.commit()
    return {"target_type": ttype, "target_key": tkey, "signal": signal}


def remove_feedback(target: str) -> int:
    """Remove any stored signal for a target (the --unmute / reset path). Returns rows deleted."""
    ttype, tkey = _classify_target(target)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM express_feedback WHERE target_type = %s AND target_key = %s",
                (ttype, tkey),
            )
            n = cur.rowcount
        conn.commit()
    return n


def list_feedback() -> list[dict]:
    """All stored preferences, newest first (for `brief --prefs`)."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT target_type, target_key, signal, weight, updated_at "
                "FROM express_feedback ORDER BY updated_at DESC"
            )
            return [dict(r) for r in cur.fetchall()]


def load_prefs() -> dict:
    """Compile stored feedback into muted sets + soft weights for fast application."""
    prefs = {
        "muted": {"item": set(), "kind": set(), "topic": set()},
        "weight": {"item": {}, "kind": {}, "topic": {}},
    }
    for r in list_feedback():
        tt, tk = r["target_type"], r["target_key"]
        if r["signal"] == "mute":
            prefs["muted"][tt].add(tk)
        else:
            prefs["weight"][tt][tk] = r["weight"]
    return prefs


def _apply_prefs(items: list[dict], prefs: dict) -> list[dict]:
    """Drop hard-muted items (by kind / item-id-prefix / topic); annotate the rest
    with a soft `pref_weight` (sum of matching kind + topic + item weights)."""
    muted, weight = prefs["muted"], prefs["weight"]
    kept = []
    for it in items:
        kind = it["kind"]
        topics = it.get("meta", {}).get("topics", []) or []
        if kind in muted["kind"]:
            continue
        if any(it["id"].lower().startswith(p) for p in muted["item"]):
            continue
        if muted["topic"] and (set(topics) & muted["topic"]):
            continue
        w = weight["kind"].get(kind, 0.0)
        w += sum(weight["topic"].get(t, 0.0) for t in topics)
        w += sum(v for k, v in weight["item"].items() if it["id"].lower().startswith(k))
        it["meta"]["pref_weight"] = w
        kept.append(it)
    return kept


def _pref_hint(prefs: dict) -> str:
    """Short natural-language preference summary for the LLM editor prompt."""
    more = [k for k, v in {**prefs["weight"]["kind"], **prefs["weight"]["topic"]}.items() if v > 0]
    less = [k for k, v in {**prefs["weight"]["kind"], **prefs["weight"]["topic"]}.items() if v < 0]
    parts = []
    if more:
        parts.append("favor (rank higher): " + ", ".join(sorted(more)))
    if less:
        parts.append("de-emphasize (rank lower): " + ", ".join(sorted(less)))
    return "User preferences — " + "; ".join(parts) + "." if parts else ""


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
                    "topics": sorted(projects),
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
        SELECT id, type, title, content, created_at, access_count, project,
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
                "meta": {"depth": round(r["depth"], 2), "access_count": r["access_count"],
                         "topics": [r["project"]] if r["project"] else []},
            }
        )
    return items


def _gather_digest(cur) -> list[dict]:
    """The latest weekly digest ('what you're working on / learning')."""
    cur.execute(
        """
        SELECT id, title, content, created_at, project
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
            "meta": {"topics": [r["project"]] if r["project"] else []},
        }
    ]


def _gather_questions(cur, limit: int) -> list[dict]:
    """Active open questions (currently none in the store; future-proofed)."""
    cur.execute(
        """
        SELECT id, title, content, created_at, project
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
            "meta": {"topics": [r["project"]] if r["project"] else []},
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

    # Apply delivery preferences: drop hard-muted items, annotate soft weights.
    prefs = load_prefs()
    items = _apply_prefs(items, prefs)

    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "counts": counts,
        "pref_hint": _pref_hint(prefs),
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


def _editor_user_message(items: list[dict], pref_hint: str = "") -> str:
    lines = []
    if pref_hint:
        lines.append(pref_hint + "\n")
    lines.append("Candidate items (idx | kind | title | signal | detail):")
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
        # Soft preference: positive weight pulls earlier, negative pushes later.
        eff = kp - it["meta"].get("pref_weight", 0.0)
        return (eff, -(it["created_at"].timestamp() if it.get("created_at") else 0))

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
        res = invoker.invoke(_EDITOR_SYSTEM, _editor_user_message(items, briefing.get("pref_hint", "")), timeout=180)
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
        sid = it["id"].split(":")[0][:8]
        lines.append(f"### {'★ ' if i == 0 else ''}{it['headline']}  `#{sid}`")
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
    lines.append("---")
    lines.append("_Shape it: `brief --useful <#id>` · `brief --less <kind|topic>` · "
                 "`brief --mute <#id|kind|topic>` · `brief --unmute …` · `brief --prefs`_")
    return "\n".join(lines)


# --- P2: proactive push (high-bar Gmail) --------------------------------------

# Config via env / gitignored (see EXPRESS-PLAN.md "Locked decisions" #9).
ENV_TO = "EXPRESS_EMAIL_TO"
ENV_FROM = "EXPRESS_EMAIL_FROM"
ENV_PASSWORD = "GMAIL_APP_PASSWORD"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def email_configured() -> bool:
    """True iff all Gmail send credentials are present in the environment."""
    return all(os.environ.get(v) for v in (ENV_TO, ENV_FROM, ENV_PASSWORD))


def _distinct_projects(cur, source_ids) -> list[str]:
    """Distinct non-empty projects among the given source memory UUIDs."""
    ids = [s for s in (source_ids or []) if s]
    if not ids:
        return []
    cur.execute(
        "SELECT DISTINCT project FROM memories "
        "WHERE id = ANY(%s::uuid[]) AND project IS NOT NULL AND project <> ''",
        (ids,),
    )
    return [row["project"] for row in cur.fetchall()]


def should_push(last_pushed_run_id: str | None = None) -> dict:
    """Decide whether the latest dream-cycle run warrants an unsolicited email.

    The bar (locked): push only when the latest completed run produced a NEW
    cross-project synthesis OR a detected contradiction. Everything else stays
    in the on-demand `brief`.

    Args:
        last_pushed_run_id: the run id already emailed (suppresses re-push).

    Returns:
        {"push": bool, "run_id": str|None, "reason": str, "triggers": [...]}
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM dream_cycle_runs WHERE completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 1"
            )
            run = cur.fetchone()
            if not run:
                return {"push": False, "run_id": None,
                        "reason": "no completed dream-cycle run", "triggers": []}
            run_id = str(run["id"])
            if last_pushed_run_id and run_id == last_pushed_run_id:
                return {"push": False, "run_id": run_id,
                        "reason": "latest run already pushed", "triggers": []}

            cur.execute(
                "SELECT candidate_json FROM dream_cycle_candidates "
                "WHERE run_id = %s AND final_verdict = 'ACCEPTED'",
                (run_id,),
            )
            accepted = cur.fetchall()
            triggers = []
            for c in accepted:
                cj = c["candidate_json"] or {}
                title = cj.get("title") or "(insight)"
                rels = cj.get("relationships") or []
                if any((r or {}).get("relation_type") == "contradicts" for r in rels):
                    triggers.append({"kind": "contradiction", "title": title})
                    continue
                projects = _distinct_projects(cur, cj.get("source_memories"))
                if cj.get("strategy_that_found_it") == "cross_project_collision" or len(projects) >= 2:
                    triggers.append({"kind": "cross_project", "title": title, "projects": projects})

    push = bool(triggers)
    if push:
        reason = "; ".join(f"{t['kind']}: {t['title']}" for t in triggers)
    else:
        reason = f"{len(accepted)} accepted insight(s), none cross-project or contradiction"
    return {"push": push, "run_id": run_id, "reason": reason, "triggers": triggers}


def render_email(briefing: dict) -> dict:
    """Render an edited briefing as an email. Returns {subject, html, text}.

    Scannable headlines at the top, detail beneath (no fragile collapsible HTML).
    """
    ranked = briefing.get("ranked", [])
    if not ranked:
        return {
            "subject": "Your second brain — nothing urgent",
            "html": "<p>Nothing worth surfacing right now.</p>",
            "text": "Nothing worth surfacing right now.\n",
        }

    lead = ranked[0]["headline"]
    subject = f"🧠 {lead}" if len(lead) <= 110 else f"🧠 {lead[:107]}…"

    # Scan layer
    html = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:640px;margin:0 auto;color:#1a1a1a;line-height:1.5">',
        '<p style="color:#666;font-size:13px;margin:0 0 4px">Your second brain noticed:</p>',
        "<ul style='padding-left:18px'>",
    ]
    text = ["Your second brain noticed:\n"]
    for i, it in enumerate(ranked):
        label = _KIND_LABEL.get(it["kind"], it["kind"])
        mark = "★" if i == 0 else "•"
        html.append(
            f"<li style='margin:6px 0'><strong>{escape(it['headline'])}</strong>"
            f" <span style='color:#999;font-size:12px'>({escape(label)})</span></li>"
        )
        text.append(f"{mark} {it['headline']}  ({label})")
    html.append("</ul><hr style='border:none;border-top:1px solid #eee;margin:16px 0'>")
    text.append("\n" + "-" * 40)

    # Detail layer
    for i, it in enumerate(ranked):
        star = "★ " if i == 0 else ""
        html.append(f"<h3 style='margin:18px 0 6px'>{star}{escape(it['headline'])}</h3>")
        text.append(f"\n{star}{it['headline']}\n")
        if it["kind"] == "insight" and it["meta"].get("projects"):
            across = "across " + ", ".join(it["meta"]["projects"])
            html.append(f"<p style='color:#888;font-size:12px;margin:0 0 6px'>{escape(across)}</p>")
            text.append(f"({across})")
        detail = it["detail"]
        html.append(f"<p style='white-space:pre-wrap;margin:0'>{escape(detail)}</p>")
        text.append(detail)
        if it["kind"] == "insight" and it["meta"].get("source_titles"):
            src = "Drawn from: " + "; ".join(f"«{t}»" for t in it["meta"]["source_titles"])
            html.append(f"<p style='color:#888;font-size:12px;margin:6px 0 0'>{escape(src)}</p>")
            text.append(src)
    html.append("</div>")

    return {"subject": subject, "html": "\n".join(html), "text": "\n".join(text) + "\n"}


def send_email(subject: str, html: str, text: str, *, to: str | None = None,
               sender: str | None = None, password: str | None = None,
               smtp_factory=None) -> bool:
    """Send a multipart (plain + HTML) email via Gmail SMTP (STARTTLS).

    Credentials come from env (EXPRESS_EMAIL_TO/FROM, GMAIL_APP_PASSWORD) unless
    passed explicitly. `smtp_factory` allows tests to inject a fake transport.

    Raises:
        RuntimeError: if email is not configured (missing to/from/password).
    """
    to = to or os.environ.get(ENV_TO)
    sender = sender or os.environ.get(ENV_FROM)
    password = password or os.environ.get(ENV_PASSWORD)
    if not (to and sender and password):
        raise RuntimeError(
            f"Express email not configured: set {ENV_TO}, {ENV_FROM}, and {ENV_PASSWORD}."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if smtp_factory is not None:
        smtp = smtp_factory()
    else:  # pragma: no cover — real network path, exercised live not in tests
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    try:
        smtp.starttls()
        smtp.login(sender, password)
        smtp.sendmail(sender, [to], msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001
            pass
    logger.info("Express email sent to %s: %s", to, subject)
    return True
