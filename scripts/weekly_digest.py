#!/usr/bin/env python3
"""Weekly digest (Refactor P3): aggregate the last N days of captured memory and
distill ONE `synthesis` memory answering "what am I working on / learning".

This is the proactive half of the system's purpose — it answers questions that
point retrieval cannot (e.g. "what am I spending most of my time on"). Idempotent
via source_url digest://{ISO-year}-W{week}.

Usage: .venv/bin/python scripts/weekly_digest.py [--days N] [--dry-run]
"""

import argparse
import datetime
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_connection, create_memory, get_processed_source_urls
from src.embeddings import generate_embedding
from src.agent_invoker import AgentInvoker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SOURCE_TYPE = "weekly_digest"
SYSTEM_PROMPT = (
    "You are a personal-knowledge analyst. You are given aggregated statistics about "
    "the user's captured activity over a recent window (top topic tags, active "
    "projects, channel activity, and titles of decisions/insights they recorded). "
    "Do NOT reply conversationally. Write a concise digest answering: what is the user "
    "spending time on, what did they decide, and what are they learning. Output ONLY a "
    'JSON object (no prose, no code fence): {"title":"<=80 chars","summary":"3-6 '
    'sentences, concrete, naming the actual projects/themes/decisions, written to '
    "directly answer 'what am I working on and learning'\"}. Use only the provided "
    "data; do not invent."
)


def aggregate(days):
    """Pull the recent-activity signal from the memories table (no KG; that is P4)."""
    iv = "%s days" % int(days)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT t, count(*) FROM memories m, unnest(m.tags) t "
                        "WHERE m.created_at >= now() - %s::interval GROUP BY t "
                        "ORDER BY 2 DESC LIMIT 25", (iv,))
            tags = cur.fetchall()
            cur.execute("SELECT project, count(*) FROM memories WHERE created_at >= "
                        "now() - %s::interval AND project IS NOT NULL AND project <> '' "
                        "AND project !~ '^[0-9a-f-]{36}$' GROUP BY 1 ORDER BY 2 DESC "
                        "LIMIT 12", (iv,))
            projects = cur.fetchall()
            cur.execute("SELECT source_type, count(*) FROM memories WHERE created_at >= "
                        "now() - %s::interval GROUP BY 1 ORDER BY 2 DESC", (iv,))
            sources = cur.fetchall()
            cur.execute("SELECT title FROM memories WHERE created_at >= now() - %s::interval "
                        "AND source_type = 'distilled_chat' AND title IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 40", (iv,))
            decisions = [r[0] for r in cur.fetchall()]
    return tags, projects, sources, decisions


def build_user_message(days, tags, projects, sources, decisions):
    def fmt(rows):
        return ", ".join(f"{name} ({n})" for name, n in rows) or "(none)"
    parts = [
        f"Window: last {days} days.",
        f"Top topic tags: {fmt(tags)}",
        f"Active projects: {fmt(projects)}",
        f"Channel activity: {fmt(sources)}",
        "Recent decisions/insights recorded:",
    ]
    parts += [f"- {t}" for t in decisions] or ["- (none)"]
    return "\n".join(parts)


def run(days=7, dry_run=False):
    y, w, _ = datetime.date.today().isocalendar()
    url = f"digest://{y}-W{w:02d}"
    if url in get_processed_source_urls(SOURCE_TYPE):
        log.info("Digest %s already exists, skipping", url)
        return None
    tags, projects, sources, decisions = aggregate(days)
    if not sources:
        log.info("No activity in window, nothing to digest")
        return None

    res = AgentInvoker().invoke(SYSTEM_PROMPT, build_user_message(days, tags, projects, sources, decisions))
    out = res["output"] if isinstance(res["output"], dict) else {}
    summary = (out.get("summary") or "").strip()
    title = (out.get("title") or f"Weekly digest {url}").strip()[:200]
    if not summary:
        log.error("Digest LLM returned no summary; raw=%.200s", res.get("raw", ""))
        return None
    # Anchor to the question it answers so it is retrievable for meta-queries
    # like "what am I spending my time on" (the summary alone is object-level).
    body = (f"What I've been spending my time working on and learning "
            f"(last {days}-day digest):\n\n{summary}")

    if dry_run:
        log.info("[DRY] %s\nTITLE: %s\n%s", url, title, body)
        return body

    create_memory(
        type="synthesis", title=title, content=body,
        embedding=generate_embedding(body), tags=["digest", "weekly"],
        source_url=url, source_type=SOURCE_TYPE, confidence=0.85,
        mem_class="semantic", metadata={"days": days, "week": url},
    )
    log.info("Created digest memory %s", url)
    try:  # best-effort proactive ping (the "here's your week" surface)
        import subprocess
        subprocess.run(["osascript", "-e",
                        'display notification "%s" with title "Weekly digest"'
                        % title.replace('"', "'")[:180]], timeout=10, check=False)
    except Exception:
        pass
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
