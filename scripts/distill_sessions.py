#!/usr/bin/env python3
"""Distill chat sessions into durable decision/insight memories (Refactor P1).

Each chat session's full text lives in its parent memory (parent_id IS NULL).
For each undistilled session we run ONE metered LLM pass through the policy-checked distill role to extract
crisp, self-contained decisions/insights and store them as retrievable
type=decision|insight memories. Raw chat chunks are left untouched.

Scope: kiro_cli_chat + quick_desktop_chat (the large IDE backfill is deferred
until P2b dedup halves it). Idempotent via source_url distill://{session}#{n}.

Usage: .venv/bin/python scripts/distill_sessions.py [--dry-run] [--limit N]
                                                    [--source-type T (repeatable)]
"""

import argparse
import hashlib
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_connection, create_memory, get_processed_source_urls, create_relationship
from src.embeddings import generate_embedding
from src.search import hybrid_search, rerank
from psycopg2 import Error as DatabaseError
from botocore.exceptions import BotoCoreError, ClientError
from src.backends.base import ModelRefusalError
from src.backends.cost import SpendCapExceeded, SpendTracker, cap_from_env, invoke_metered
from src.backends.resolver import DISTILL_ROLE, resolve_stage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SOURCE_TYPE = "distilled_chat"
DISTILL_TYPES = {"decision", "insight"}
MAX_CHARS = 100000
DEFAULT_SOURCES = ("kiro_cli_chat", "quick_desktop_chat")

# The active profile supplies the extraction model. Any optional override is
# validated by resolve_stage before an adapter can be constructed.
DISTILL_MODEL = os.environ.get("DISTILL_MODEL") or None
DISTILL_EFFORT = os.environ.get("DISTILL_EFFORT", "medium")
DISTILL_TIMEOUT = int(os.environ.get("DISTILL_TIMEOUT", "600"))
# Nightly batch bound: sessions are fetched newest-first, so the backlog drains
# from the most relevant end without one run holding the machine for hours.
DEFAULT_LIMIT = int(os.environ.get("DISTILL_LIMIT", "40"))

# Item cap raised 5->8 (2026-06-04): 65% of distilled sessions were hitting the old
# cap of 5 exactly, i.e. it was binding and truncating durable items. 8 gives headroom
# while keeping a ceiling against low-value padding (quality bar enforced by the prompt).
SYSTEM_PROMPT = (
    "You are a knowledge distiller. You are given the transcript of ONE completed "
    "AI-assistant chat session, delimited by triple quotes. Do NOT continue or reply "
    "to the conversation. Analyze it and extract the durable knowledge worth "
    "remembering. Output ONLY a JSON array (use [] if nothing qualifies) — no prose, "
    'no code fence. Each element: {"type":"decision"|"insight","title":"<=80 chars",'
    '"content":"WHAT: ... WHY: ..."}. A decision = a choice that was made plus its '
    "rationale. An insight = a non-obvious learning or principle. At most 8 items. "
    "Skip routine chatter, tool output, and trivia. Each item must be self-contained "
    "(understandable without the transcript)."
)


def build_user_message(content):
    return f'Transcript to distill (do not reply to it):\n"""\n{content[:MAX_CHARS]}\n"""'


def valid_items(raw):
    """Keep only well-formed distill items: decision/insight with content."""
    return [it for it in (raw or [])
            if isinstance(it, dict) and it.get("type") in DISTILL_TYPES
            and isinstance(it.get("content"), str) and it["content"].strip()
            and (it.get("title") is None or isinstance(it.get("title"), str))]


def fetch_sessions(source_types, limit, already_sessions, refusal_scope=None):
    """Return [(session_id, source_type, content)] for undistilled session parents."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_url, source_type, content, metadata FROM memories "
                "WHERE source_type = ANY(%s) AND parent_id IS NULL AND content IS NOT NULL "
                "ORDER BY created_at DESC",
                (list(source_types),),
            )
            rows = cur.fetchall()
    out = []
    for source_url, st, content, metadata in rows:
        if refusal_scope and refusal_matches(metadata, refusal_scope, content):
            continue
        if not source_url or f"distill://{source_url}" in already_sessions:
            continue
        out.append((source_url, st, content))
        if limit and len(out) >= limit:
            break
    return out


def refusal_matches(metadata, scope, content):
    """Only the explicitly refused input/provider combination is quarantined."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return False
    refusal = metadata.get("distill_refusal", {}) if isinstance(metadata, dict) else {}
    return (isinstance(refusal, dict) and refusal.get("do_not_send") is True
            and all(refusal.get(k) == v for k, v in scope.items())
            and refusal.get("input_sha256") == hashlib.sha256(content.encode()).hexdigest())


def mark_refused(source_url, source_type, content, spec, *, operation="distill", input_text=None):
    """Persist a provider-reported refusal, without copying its text or transcript.

    A changed source body or provider/model may be reconsidered. The source body
    comparison prevents quarantining a newer revision written during the call.
    """
    marker = {"do_not_send": True, "role": DISTILL_ROLE, "backend": spec.backend,
              "model": spec.model, "operation": operation,
              "input_sha256": hashlib.sha256((content if input_text is None else input_text).encode()).hexdigest()}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
                "WHERE source_url=%s AND source_type=%s AND parent_id IS NULL AND content=%s",
                (json.dumps({"distill_refusal": marker}), source_url, source_type, content))
        conn.commit()


CONTRADICTION_SYSTEM_PROMPT = (
    "You compare two DECISION records from one person's knowledge base. Decide whether the "
    "NEW decision directly reverses or contradicts the PRIOR one — i.e., they cannot both be "
    "current truth on the same question. Be conservative: answer true ONLY for a genuine "
    "reversal or contradiction on the SAME topic, NOT for refinement, elaboration, a related "
    "but different topic, or mere similarity. Output ONLY JSON (no prose, no code fence): "
    '{"contradicts": true|false, "reason": "<=20 words"}.'
)


def _find_prior_decision(new_id, new_session, query_text, query_embedding):
    """Most similar prior ACTIVE decision that is not the new memory or a same-session sibling."""
    results = hybrid_search(query_text, query_embedding, limit=5, type="decision", status="active")
    results = rerank(results, query_text)
    for r in results:
        if str(r["id"]) == str(new_id):
            continue
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        if new_session and meta.get("distilled_from") == new_session:
            continue  # skip siblings distilled from the same session
        return r
    return None


def detect_and_link_contradiction(invoker, new_id, title, content, embedding, new_session, *, spec, tracker):
    """Conservatively flag when a NEW decision contradicts a PRIOR active decision.

    Creates a `contradicts` relationship (new -> prior) with the LLM's reason as a note.
    Deliberately never changes status — actual supersession stays a reviewed action
    (dream-cycle panel or the user). Returns the prior decision id when linked, else None.
    """
    cand = _find_prior_decision(new_id, new_session, f"{title}\n{content}", embedding)
    if cand is None:
        return None
    user = (
        f"NEW decision:\nTitle: {title}\n{content[:1500]}\n\n"
        f"PRIOR decision:\nTitle: {cand.get('title') or ''}\n{(cand.get('content') or '')[:1500]}"
    )
    res = invoke_metered(invoker, spec, tracker, CONTRADICTION_SYSTEM_PROMPT, user,
                         tools=False,
                         timeout=DISTILL_TIMEOUT, effort="low", stage="distill_contradiction")
    out = res.get("output") if isinstance(res, dict) else None
    if isinstance(out, dict) and out.get("contradicts") is True:
        reason = str(out.get("reason") or "").strip()[:300] or "distiller-detected contradiction"
        create_relationship(str(new_id), str(cand["id"]), "contradicts", reason)
        log.info("Contradiction linked: %s -> %s (%s)", new_id, cand["id"], reason)
        return str(cand["id"])
    return None


_EXPECTED_FAILURES = (RuntimeError, OSError, ValueError, DatabaseError, BotoCoreError, ClientError)


def distill(dry_run=False, limit=DEFAULT_LIMIT, source_types=DEFAULT_SOURCES,
            detect_contradictions=True, *, resolver=None, tracker=None):
    if limit < 0:
        raise ValueError("limit must be non-negative")
    already = get_processed_source_urls(SOURCE_TYPE)
    already_sessions = {u.rsplit("#", 1)[0] for u in already if u}
    stats = {"sessions": 0, "memories": 0, "empty": 0, "failed": 0,
             "contradictions": 0, "refused": 0, "aborted": False,
             "dry_run": dry_run, "planned_sessions": 0, "cost_usd": 0.0}
    if dry_run:
        stats["planned_sessions"] = len(fetch_sessions(source_types, limit, already_sessions))
        log.info("Dry run: %d potentially eligible sessions; no models, embeddings or writes. "
                 "Provider-scoped refusal filtering happens in execution.", stats["planned_sessions"])
        return stats

    invoker, spec = resolve_stage(DISTILL_ROLE, resolver=resolver, model_override=DISTILL_MODEL)
    tracker = tracker if tracker is not None else SpendTracker(
        cap_usd=cap_from_env("SECOND_BRAIN_DISTILL_CAP_USD", 1.0))
    scope = {"role": DISTILL_ROLE, "backend": spec.backend, "model": spec.model, "operation": "distill"}
    sessions = fetch_sessions(source_types, limit, already_sessions, refusal_scope=scope)
    log.info("Distilling %d sessions (model=%s, stop threshold=$%.2f)",
             len(sessions), spec.model, tracker.cap_usd)

    for source_url, st, content in sessions:
        try:
            res = invoke_metered(invoker, spec, tracker, SYSTEM_PROMPT, build_user_message(content),
                                 tools=False, timeout=DISTILL_TIMEOUT, effort=DISTILL_EFFORT, stage="distill")
            if not isinstance(res.get("output"), list):
                raise ValueError("Distill response must be an array")
            items = valid_items(res["output"])
            if res["output"] and not items:
                raise ValueError("Distill response contained no valid items")
        except SpendCapExceeded as exc:
            stats["failed"] += 1
            stats["aborted"] = True
            log.error("Distillation stopped (%s)", type(exc).__name__)
            break
        except ModelRefusalError:
            stats["failed"] += 1
            stats["refused"] += 1
            mark_refused(source_url, st, content, spec)
            log.warning("Explicit refusal recorded for the exact session/provider input")
            continue
        except _EXPECTED_FAILURES as exc:
            stats["failed"] += 1
            log.error("Distillation failed (%s)", type(exc).__name__)
            continue

        stats["sessions"] += 1
        if not items:
            stats["empty"] += 1
            continue
        for i, it in enumerate(items):
            url = f"distill://{source_url}#{i}"
            if url in already:
                continue
            body = it["content"]
            try:
                emb = generate_embedding(body)
                title = (it.get("title") or body[:80])[:200]
                new_id = create_memory(
                    type=it["type"], title=title, content=body, embedding=emb,
                    tags=["distilled", st], source_url=url, source_type=SOURCE_TYPE,
                    confidence=0.8, mem_class="semantic",
                    metadata={"distilled_from": source_url, "origin_source_type": st},
                )
                stats["memories"] += 1
                already.add(url)
            except _EXPECTED_FAILURES as exc:
                stats["failed"] += 1
                log.error("Distilled-memory write failed (%s)", type(exc).__name__)
                continue
            if detect_contradictions and it["type"] == "decision":
                try:
                    if detect_and_link_contradiction(invoker, new_id, title, body, emb, source_url,
                                                     spec=spec, tracker=tracker):
                        stats["contradictions"] += 1
                except SpendCapExceeded as exc:
                    stats["failed"] += 1
                    stats["aborted"] = True
                    log.error("Contradiction check stopped (%s)", type(exc).__name__)
                    break
                except _EXPECTED_FAILURES as exc:
                    stats["failed"] += 1
                    log.error("Contradiction check failed (%s)", type(exc).__name__)
        if stats["aborted"]:
            break
    stats["cost_usd"] = tracker.total_usd
    stats["spend_unknown"] = bool(tracker.blocked_reason)
    log.info("Done: %s", stats)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Count potentially eligible sessions; no model calls or writes.")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Max sessions per run, newest first (default {DEFAULT_LIMIT}; 0 = all)")
    ap.add_argument("--source-type", action="append", dest="source_types")
    ap.add_argument("--skip-contradictions", action="store_true",
                    help="Disable distill-time contradiction detection (e.g. for large backfills)")
    args = ap.parse_args()
    try:
        stats = distill(dry_run=args.dry_run, limit=args.limit,
                       source_types=tuple(args.source_types) if args.source_types else DEFAULT_SOURCES,
                       detect_contradictions=not args.skip_contradictions)
    except _EXPECTED_FAILURES as exc:
        log.error("Distillation could not complete (%s)", type(exc).__name__)
        return 1
    return 3 if stats["aborted"] else 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
