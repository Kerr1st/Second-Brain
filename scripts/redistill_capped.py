#!/usr/bin/env python3
"""One-off backfill: recover durable items the OLD 5-item distiller cap dropped.

Re-distills each session that has exactly OLD_CAP items, ROBUSTLY:
  - The LLM is given the already-captured items and asked ONLY for genuinely-additional
    ones (not rephrasings), so it does not re-extract the same decisions.
  - Each candidate is embedding-deduped (cosine) against the session's stored items AND
    against items accepted earlier in the same batch; near-duplicates are dropped.
  - Single-instance lockfile guard + correct idempotency (sessions that gain items leave
    the count==OLD_CAP set) prevent concurrent / double processing.

Non-destructive: existing items untouched; net-new stored at continuing #indices, tagged
metadata.redistilled=true. Contradiction detection is skipped (daily distiller + dream-cycle
handle that).

Usage: .venv/bin/python scripts/redistill_capped.py [--dry-run] [--limit N] [--cap N] [--sim-threshold F]
"""

import argparse
import logging
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_connection, create_memory
from src.embeddings import generate_embedding
from src.agent_invoker import AgentInvoker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SOURCE_TYPE = "distilled_chat"
DISTILL_TYPES = {"decision", "insight"}
OLD_CAP = 5
MAX_CONSEC_FAIL = 5
SIM_THRESHOLD = 0.88   # cosine; a candidate this similar to an existing/accepted item is dropped
MAX_CHARS = 100000
LOCKFILE = "/tmp/redistill_capped.lock"

REDISTILL_SYSTEM_PROMPT = (
    "You are given the transcript of ONE completed AI-assistant chat session (triple-quoted) "
    "and a numbered list of decisions/insights ALREADY captured from it. Do NOT reply to or "
    "continue the conversation. Extract ONLY durable decisions/insights that are genuinely "
    "ADDITIONAL and NOT already represented in the captured list — do not rephrase, restate, "
    "or duplicate any captured item. Output ONLY a JSON array (use [] if there is nothing "
    'genuinely new): each element {"type":"decision"|"insight","title":"<=80 chars",'
    '"content":"WHAT: ... WHY: ..."}. At most 3 items. Each must be self-contained and clearly '
    "distinct from every already-captured item."
)


def _valid(raw):
    return [it for it in (raw or [])
            if isinstance(it, dict) and it.get("type") in DISTILL_TYPES and it.get("content")]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def find_capped_sessions(cap=OLD_CAP, limit=0):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata->>'distilled_from' s, count(*) c FROM memories "
                "WHERE source_type=%s AND metadata->>'distilled_from' IS NOT NULL "
                "GROUP BY 1 HAVING count(*)=%s ORDER BY 1", (SOURCE_TYPE, cap))
            sessions = [r[0] for r in cur.fetchall()]
    return sessions[:limit] if limit else sessions


def session_state(session_url):
    """Return (existing items [{title,content}], max #index, origin_source_type)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_url, title, content, metadata->>'origin_source_type' "
                "FROM memories WHERE source_type=%s AND metadata->>'distilled_from'=%s "
                "ORDER BY source_url", (SOURCE_TYPE, session_url))
            rows = cur.fetchall()
    items, max_idx, origin = [], -1, None
    for su, title, content, ost in rows:
        items.append({"title": title, "content": content})
        origin = origin or ost
        try:
            max_idx = max(max_idx, int(su.rsplit("#", 1)[1]))
        except (ValueError, IndexError):
            pass
    return items, max_idx, origin


def fetch_transcript(session_url):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM memories WHERE source_url=%s AND parent_id IS NULL LIMIT 1",
                        (session_url,))
            r = cur.fetchone()
    return r[0] if r else None


def max_sim_existing(emb_str, session_url):
    """Max cosine similarity of a candidate embedding vs the session's STORED items."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(1 - (embedding <=> %s::vector)) FROM memories "
                "WHERE source_type=%s AND metadata->>'distilled_from'=%s AND embedding IS NOT NULL",
                (emb_str, SOURCE_TYPE, session_url))
            r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


def build_user_message(transcript, existing):
    captured = "\n".join(f"{i+1}. [{it.get('title') or ''}] {(it.get('content') or '')[:200]}"
                         for i, it in enumerate(existing)) or "(none)"
    return (f"Already-captured items from this session (do NOT duplicate or rephrase these):\n"
            f"{captured}\n\nTranscript (do not reply to it):\n\"\"\"\n{transcript[:MAX_CHARS]}\n\"\"\"")


def redistill(dry_run=False, limit=0, cap=8, sim_threshold=SIM_THRESHOLD):
    invoker = AgentInvoker()
    sessions = find_capped_sessions(limit=limit)
    max_new = max(0, cap - OLD_CAP)
    log.info("Re-distilling %d capped sessions (cap=%d max_new=%d sim_threshold=%.2f dry_run=%s)",
             len(sessions), cap, max_new, sim_threshold, dry_run)
    stats = {"sessions": 0, "new_items": 0, "dropped_dup": 0, "no_new": 0, "no_transcript": 0, "failed": 0}
    consec_fail = 0
    for sess in sessions:
        transcript = fetch_transcript(sess)
        if not transcript:
            stats["no_transcript"] += 1
            continue
        existing, max_idx, origin = session_state(sess)
        origin = origin or "unknown"
        try:
            res = invoker.invoke(REDISTILL_SYSTEM_PROMPT, build_user_message(transcript, existing))
            cands = _valid(res["output"] if isinstance(res["output"], list) else [])
            consec_fail = 0
        except Exception as e:
            consec_fail += 1
            stats["failed"] += 1
            log.error("redistill failed %s: %s", sess, e)
            if consec_fail >= MAX_CONSEC_FAIL:
                log.error("Circuit breaker: %d consecutive failures (likely expired SSO token); aborting.",
                          consec_fail)
                break
            continue
        stats["sessions"] += 1

        accepted_embs, idx, added = [], max_idx, 0
        for it in cands:
            if added >= max_new:
                break
            body = it["content"]
            emb = generate_embedding(body)            # list[float]
            emb_str = str(emb)
            if max_sim_existing(emb_str, sess) > sim_threshold:        # dup of a stored item
                stats["dropped_dup"] += 1
                continue
            if any(_cosine(emb, e) > sim_threshold for e in accepted_embs):  # dup within this batch
                stats["dropped_dup"] += 1
                continue
            idx += 1
            if dry_run:
                stats["new_items"] += 1; added += 1; accepted_embs.append(emb)
                log.info("[DRY] %s | %s: %s", origin, it["type"], (it.get("title") or "")[:70])
                continue
            try:
                create_memory(
                    type=it["type"], title=(it.get("title") or body[:80])[:200],
                    content=body, embedding=emb, tags=["distilled", origin, "redistill"],
                    source_url=f"distill://{sess}#{idx}", source_type=SOURCE_TYPE,
                    confidence=0.8, mem_class="semantic",
                    metadata={"distilled_from": sess, "origin_source_type": origin, "redistilled": True})
                stats["new_items"] += 1; added += 1; accepted_embs.append(emb)
            except Exception as e:
                stats["failed"] += 1
                log.error("create failed %s#%d: %s", sess, idx, e)
        if added == 0:
            stats["no_new"] += 1

    log.info("Done: sessions=%d new_items=%d dropped_dup=%d no_new=%d no_transcript=%d failed=%d",
             stats["sessions"], stats["new_items"], stats["dropped_dup"], stats["no_new"],
             stats["no_transcript"], stats["failed"])
    return stats


def _acquire_lock():
    """Single-instance guard: refuse to start if another live redistill holds the lock."""
    if os.path.exists(LOCKFILE):
        try:
            pid = int(open(LOCKFILE).read().strip())
            os.kill(pid, 0)  # raises unless pid is alive
            log.error("Another redistill is running (pid %d); exiting. Remove %s if stale.", pid, LOCKFILE)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock — overwrite
    with open(LOCKFILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cap", type=int, default=8)
    ap.add_argument("--sim-threshold", type=float, default=SIM_THRESHOLD)
    args = ap.parse_args()
    if not args.dry_run and not _acquire_lock():
        sys.exit(1)
    try:
        redistill(dry_run=args.dry_run, limit=args.limit, cap=args.cap, sim_threshold=args.sim_threshold)
    finally:
        if not args.dry_run and os.path.exists(LOCKFILE):
            try:
                os.remove(LOCKFILE)
            except OSError:
                pass


if __name__ == "__main__":
    main()
