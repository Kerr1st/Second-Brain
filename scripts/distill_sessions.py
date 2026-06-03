#!/usr/bin/env python3
"""Distill chat sessions into durable decision/insight memories (Refactor P1).

Each chat session's full text lives in its parent memory (parent_id IS NULL).
For each undistilled session we run ONE LLM pass via AgentInvoker to extract
crisp, self-contained decisions/insights and store them as retrievable
type=decision|insight memories. Raw chat chunks are left untouched.

Scope: kiro_cli_chat + quick_desktop_chat (the large IDE backfill is deferred
until P2b dedup halves it). Idempotent via source_url distill://{session}#{n}.

Usage: .venv/bin/python scripts/distill_sessions.py [--dry-run] [--limit N]
                                                    [--source-type T (repeatable)]
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_connection, create_memory, get_processed_source_urls
from src.embeddings import generate_embedding
from src.agent_invoker import AgentInvoker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SOURCE_TYPE = "distilled_chat"
DISTILL_TYPES = {"decision", "insight"}
MAX_CHARS = 100000
DEFAULT_SOURCES = ("kiro_cli_chat", "quick_desktop_chat")

SYSTEM_PROMPT = (
    "You are a knowledge distiller. You are given the transcript of ONE completed "
    "AI-assistant chat session, delimited by triple quotes. Do NOT continue or reply "
    "to the conversation. Analyze it and extract the durable knowledge worth "
    "remembering. Output ONLY a JSON array (use [] if nothing qualifies) — no prose, "
    'no code fence. Each element: {"type":"decision"|"insight","title":"<=80 chars",'
    '"content":"WHAT: ... WHY: ..."}. A decision = a choice that was made plus its '
    "rationale. An insight = a non-obvious learning or principle. At most 5 items. "
    "Skip routine chatter, tool output, and trivia. Each item must be self-contained "
    "(understandable without the transcript)."
)


def build_user_message(content):
    return f'Transcript to distill (do not reply to it):\n"""\n{content[:MAX_CHARS]}\n"""'


def valid_items(raw):
    """Keep only well-formed distill items: decision/insight with content."""
    return [it for it in (raw or [])
            if isinstance(it, dict) and it.get("type") in DISTILL_TYPES and it.get("content")]


def fetch_sessions(source_types, limit, already_sessions):
    """Return [(session_id, source_type, content)] for undistilled session parents."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_url, source_type, content FROM memories "
                "WHERE source_type = ANY(%s) AND parent_id IS NULL AND content IS NOT NULL "
                "ORDER BY created_at DESC",
                (list(source_types),),
            )
            rows = cur.fetchall()
    out = []
    for source_url, st, content in rows:
        if f"distill://{source_url}" in already_sessions:
            continue
        out.append((source_url, st, content))
        if limit and len(out) >= limit:
            break
    return out


def distill(dry_run=False, limit=0, source_types=DEFAULT_SOURCES):
    invoker = AgentInvoker()
    already = get_processed_source_urls(SOURCE_TYPE)
    already_sessions = {u.rsplit("#", 1)[0] for u in already}
    sessions = fetch_sessions(source_types, limit, already_sessions)
    log.info("Distilling %d sessions (sources=%s, dry_run=%s)", len(sessions), source_types, dry_run)

    stats = {"sessions": 0, "memories": 0, "empty": 0, "failed": 0}
    for source_url, st, content in sessions:
        try:
            res = invoker.invoke(SYSTEM_PROMPT, build_user_message(content))
            items = res["output"] if isinstance(res["output"], list) else []
        except Exception as e:
            stats["failed"] += 1
            log.error("Distill failed %s: %s", source_url, e)
            continue

        items = valid_items(items)
        stats["sessions"] += 1
        if not items:
            stats["empty"] += 1
            continue

        for i, it in enumerate(items):
            url = f"distill://{source_url}#{i}"
            if url in already:
                continue
            body = it["content"]
            if dry_run:
                stats["memories"] += 1
                log.info("[DRY] %s | %s: %s", st, it["type"], (it.get("title") or "")[:70])
                continue
            try:
                create_memory(
                    type=it["type"], title=(it.get("title") or body[:80])[:200],
                    content=body, embedding=generate_embedding(body),
                    tags=["distilled", st], source_url=url, source_type=SOURCE_TYPE,
                    confidence=0.8, mem_class="semantic",
                    metadata={"distilled_from": source_url, "origin_source_type": st},
                )
                stats["memories"] += 1
            except Exception as e:
                stats["failed"] += 1
                log.error("create failed %s: %s", url, e)

    log.info("Done: sessions=%d memories=%d empty=%d failed=%d",
             stats["sessions"], stats["memories"], stats["empty"], stats["failed"])
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source-type", action="append", dest="source_types")
    args = ap.parse_args()
    distill(dry_run=args.dry_run, limit=args.limit,
            source_types=tuple(args.source_types) if args.source_types else DEFAULT_SOURCES)


if __name__ == "__main__":
    main()
