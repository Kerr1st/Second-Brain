#!/usr/bin/env python3
"""One-off backfill: re-distill sessions that hit the OLD 5-item cap, recovering the
durable items the cap truncated (the distiller item cap was raised 5 -> 8).

Non-destructive + dedup-aware: existing items are left untouched; only NET-NEW items
(whose content is not a near-duplicate of an already-stored item for that session) are
created, at continuing indices (#5, #6, ...), bounded to +(cap - OLD_CAP) per session.
Idempotent: once a session gains items it no longer has exactly OLD_CAP items, so it is
not re-selected. Contradiction detection is intentionally skipped here (the daily distiller
and dream-cycle handle that) to keep the backfill tractable.

Usage: .venv/bin/python scripts/redistill_capped.py [--dry-run] [--limit N] [--cap N]
"""

import argparse
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_connection, create_memory
from src.embeddings import generate_embedding
from src.agent_invoker import AgentInvoker
from scripts.distill_sessions import SYSTEM_PROMPT, build_user_message, valid_items, SOURCE_TYPE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

OLD_CAP = 5
MAX_CONSEC_FAIL = 5  # abort if this many sessions fail in a row (likely expired SSO token)


def _norm(text):
    """Same normalization the retrieval dedup uses, for content-equality checks."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:300]


def find_capped_sessions(cap=OLD_CAP, limit=0):
    """Original session source_urls whose distilled item-count == cap (hit the old cap)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata->>'distilled_from' AS sess, count(*) AS cnt "
                "FROM memories WHERE source_type=%s AND metadata->>'distilled_from' IS NOT NULL "
                "GROUP BY 1 HAVING count(*) = %s ORDER BY 1",
                (SOURCE_TYPE, cap),
            )
            sessions = [r[0] for r in cur.fetchall()]
    return sessions[:limit] if limit else sessions


def existing_items_for(session_url):
    """Return (set of normalized contents, max #index, origin_source_type) for a session."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_url, content, metadata->>'origin_source_type' "
                "FROM memories WHERE source_type=%s AND metadata->>'distilled_from'=%s",
                (SOURCE_TYPE, session_url),
            )
            rows = cur.fetchall()
    contents, max_idx, origin = set(), -1, None
    for source_url, content, ost in rows:
        contents.add(_norm(content))
        origin = origin or ost
        try:
            max_idx = max(max_idx, int(source_url.rsplit("#", 1)[1]))
        except (ValueError, IndexError):
            pass
    return contents, max_idx, origin


def fetch_transcript(session_url):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content, source_type FROM memories "
                "WHERE source_url=%s AND parent_id IS NULL LIMIT 1",
                (session_url,),
            )
            row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def redistill(dry_run=False, limit=0, cap=8):
    invoker = AgentInvoker()
    sessions = find_capped_sessions(limit=limit)
    max_new = max(0, cap - OLD_CAP)
    log.info("Re-distilling %d capped sessions (cap=%d, max_new/session=%d, dry_run=%s)",
             len(sessions), cap, max_new, dry_run)

    stats = {"sessions": 0, "new_items": 0, "no_new": 0, "no_transcript": 0, "failed": 0}
    consec_fail = 0
    for sess in sessions:
        transcript, orig_st = fetch_transcript(sess)
        if not transcript:
            stats["no_transcript"] += 1
            continue
        existing_contents, max_idx, origin = existing_items_for(sess)
        origin = origin or orig_st or "unknown"
        try:
            res = invoker.invoke(SYSTEM_PROMPT, build_user_message(transcript))
            items = valid_items(res["output"] if isinstance(res["output"], list) else [])
            consec_fail = 0
        except Exception as e:
            consec_fail += 1
            stats["failed"] += 1
            log.error("redistill failed %s: %s", sess, e)
            if consec_fail >= MAX_CONSEC_FAIL:
                log.error("Circuit breaker: %d consecutive failures (likely expired SSO token); "
                          "aborting. Re-run with a fresh token to resume the remainder.", consec_fail)
                break
            continue
        stats["sessions"] += 1

        # Net-new only: drop items whose content already exists for this session.
        new_items = [it for it in items if _norm(it["content"]) not in existing_contents][:max_new]
        if not new_items:
            stats["no_new"] += 1
            continue

        idx = max_idx
        for it in new_items:
            idx += 1
            url = f"distill://{sess}#{idx}"
            body = it["content"]
            if dry_run:
                stats["new_items"] += 1
                log.info("[DRY] %s | %s: %s", origin, it["type"], (it.get("title") or "")[:70])
                continue
            try:
                create_memory(
                    type=it["type"], title=(it.get("title") or body[:80])[:200],
                    content=body, embedding=generate_embedding(body),
                    tags=["distilled", origin, "redistill"], source_url=url, source_type=SOURCE_TYPE,
                    confidence=0.8, mem_class="semantic",
                    metadata={"distilled_from": sess, "origin_source_type": origin, "redistilled": True},
                )
                stats["new_items"] += 1
            except Exception as e:
                stats["failed"] += 1
                log.error("create failed %s: %s", url, e)

    log.info("Done: sessions=%d new_items=%d no_new=%d no_transcript=%d failed=%d",
             stats["sessions"], stats["new_items"], stats["no_new"],
             stats["no_transcript"], stats["failed"])
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cap", type=int, default=8)
    args = ap.parse_args()
    redistill(dry_run=args.dry_run, limit=args.limit, cap=args.cap)


if __name__ == "__main__":
    main()
