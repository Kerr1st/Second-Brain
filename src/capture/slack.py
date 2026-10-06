"""Capture Slack conversations into the Second Brain.

Pulls through the local ``slack-mcp`` server (Midway hardware-bound auth, works
headlessly under launchd). Unit of capture is one *conversation-day*: every
message (with thread replies) posted in a channel or DM during one local
calendar day becomes a single source document, ``slack://<conversation id>/
<YYYY-MM-DD>``. Only *completed* days are captured, so a day is never half-
captured and then skipped by dedup; ``--days-back`` (default 2) lets a missed
night recover.

slack-mcp defends against prompt injection by prefixing replies with a text
preamble and wrapping each message text in ``<slack-user-content>`` tags with
whitespace datamarked as U+E000. Those are transport artefacts, not content —
:func:`clean_text` undoes them before rendering.

A stale Midway cookie killed this job silently on 2026-09-20 and 09-21 (two
days of Slack lost), so every run starts with :func:`preflight` — one cheap
``list_my_channels`` probe. An auth failure exits 3 with a one-line "run
mwinit" message and a notification, *without* attempting the capture; a dead
server exits 4; only a real capture exits 0.

    python -m src.capture.slack [--days-back N] [--dry-run] [--max-conversations N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from src.capture.mcp_client import (
    CaptureFailure, McpClient, McpRateLimited, McpToolError, auth_error_in_payload, classify_error,
    report_failure,
)

from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import run_channel
from src.capture.render import render_header_lines
from src.capture.window import completed_days, day_bounds_utc, local_zone

logger = logging.getLogger(__name__)

SOURCE_TYPE = "slack"
SLACK_CMD = os.environ.get("SLACK_MCP", os.path.expanduser("~/.aim/mcp-servers/slack-mcp"))

# Per-tool-call ceiling. A channel-day page (200 messages + up to 50 threads) is
# the heaviest call; 120s is generous for it and still bounded.
SLACK_CALL_TIMEOUT = float(os.environ.get("SLACK_CALL_TIMEOUT", "120"))
# slack-mcp throttles get_messages at 120 calls per 60 s and answers
# RATE_LIMIT_EXCEEDED with "Retry after N s". A 200-conversation, 2-day run makes
# up to ~400 such calls, so throttling is normal, not a fault: honour the
# Retry-After (bounded per call and per run) and count whatever is still refused
# as a failed conversation-day instead of silently dropping it (2026-10-05: 72
# throttled replies, 36 days dropped, job reported failed=0).
SLACK_RATE_LIMIT_RETRIES = int(os.environ.get("SLACK_RATE_LIMIT_RETRIES", "3"))
SLACK_RATE_LIMIT_MAX_WAIT = float(os.environ.get("SLACK_RATE_LIMIT_MAX_WAIT", "90"))
SLACK_RATE_LIMIT_WAIT_BUDGET = float(os.environ.get("SLACK_RATE_LIMIT_WAIT_BUDGET", "900"))
# The probe must be quick — it exists to fail fast, not to wait out a sick server.
PROBE_TOOL = "list_my_channels"
PROBE_TIMEOUT = float(os.environ.get("SLACK_PROBE_TIMEOUT", "60"))
AUTH_FIX = "run `mwinit` to refresh the Midway cookie, then re-run the job"

MAX_CONVERSATIONS = int(os.environ.get("SLACK_MAX_CONVERSATIONS", "200"))
MAX_MESSAGES_PER_DAY = 1000   # get_messages upper bound per call
MAX_THREADS = 50              # get_messages upper bound
MAX_DOC_CHARS = 60000
DATAMARK = "\ue000"

_TAG_RE = re.compile(r"</?slack-user-content[^>]*>")
_USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_CHANNEL_REF_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")

Call = Callable[[str, dict], Awaitable[Any]]


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def clean_text(text: str | None, users: dict[str, str] | None = None) -> str:
    """Undo slack-mcp transport wrapping and Slack markup into plain text."""
    if not text:
        return ""
    s = _TAG_RE.sub("", text).replace(DATAMARK, " ")
    s = _LINK_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})", s)
    s = _CHANNEL_REF_RE.sub(lambda m: f"#{m.group(2) or m.group(1)}", s)
    if users:
        s = _USER_MENTION_RE.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), s)
    return s.strip()


def display_name(user: Any) -> str:
    if isinstance(user, dict):
        return user.get("realName") or user.get("displayName") or user.get("name") or user.get("id") or "unknown"
    return str(user or "unknown")


def day_source_url(conversation_id: str, day: date) -> str:
    if not conversation_id or not conversation_id.strip():
        raise ValueError("Slack conversation identity is required")
    return f"slack://{conversation_id}/{day.isoformat()}"


def _hhmm(ts: str | None) -> str:
    """ISO timestamp → local HH:MM."""
    if not ts:
        return "--:--"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(local_zone()).strftime("%H:%M")
    except ValueError:
        return ts[11:16] if len(ts) >= 16 else ts


def _collect_users(messages: list[dict]) -> dict[str, str]:
    users: dict[str, str] = {}
    for m in messages:
        u = m.get("user")
        if isinstance(u, dict) and u.get("id"):
            users[u["id"]] = display_name(u)
        for r in m.get("thread") or []:
            ru = r.get("user")
            if isinstance(ru, dict) and ru.get("id"):
                users[ru["id"]] = display_name(ru)
    return users


def render_day(conv: dict, day: date, messages: list[dict]) -> tuple[str, str] | None:
    """Return (source_url, markdown) for one conversation-day, or None if empty."""
    users = _collect_users(messages)
    lines: list[str] = []
    n_messages = n_replies = 0
    for m in sorted(messages, key=lambda x: x.get("timestamp") or ""):
        text = clean_text(m.get("text"), users)
        if not text and not m.get("thread"):
            continue
        n_messages += 1
        lines.append(f"**{_hhmm(m.get('timestamp'))} {display_name(m.get('user'))}**: {text or '(attachment)'}")
        for r in m.get("thread") or []:
            rt = clean_text(r.get("text"), users)
            if rt:
                n_replies += 1
                lines.append(f"    ↳ {_hhmm(r.get('timestamp'))} {display_name(r.get('user'))}: {rt}")
    if not lines:
        return None
    body = "\n".join(lines)
    if len(body) > MAX_DOC_CHARS:
        body = body[:MAX_DOC_CHARS].rstrip() + "\n\n[truncated]"

    kind = conv.get("kind") or "channel"
    name = conv.get("name") or conv["id"]
    label = f"DM with {name}" if kind == "dm" else (f"Group DM {name}" if kind == "group_dm" else f"#{name}")
    header = [
        f"# Slack: {label} — {day.isoformat()}",
        "",
        f"Source: {day_source_url(conv['id'], day)}",
        "Type: slack-conversation",
        f"Date: {day.isoformat()}",
        f"Conversation: {label}",
        f"Kind: {kind}",
        f"Messages: {n_messages}",
        f"Thread replies: {n_replies}",
    ]
    if conv.get("purpose"):
        header.append(f"Purpose: {conv['purpose']}")
    if conv.get("topic"):
        header.append(f"Topic: {conv['topic']}")
    return day_source_url(conv["id"], day), render_header_lines(header, body)



# --------------------------------------------------------------------------- #
# Pre-flight auth probe                                                        #
# --------------------------------------------------------------------------- #
async def preflight(call: Call) -> tuple[str, str]:
    """Probe ``list_my_channels`` once; return ``(kind, detail)``.

    ``kind`` is ``"ok"``, ``"auth"``, ``"transport"`` or ``"error"``. This is the
    same call the capture makes first, so it fails in exactly the same way the
    real run would — only here the failure is classified instead of becoming a
    01:00 stack trace. Never raises.
    """
    try:
        data = await asyncio.wait_for(call(PROBE_TOOL, {}), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return "transport", f"{PROBE_TOOL}: no reply within {PROBE_TIMEOUT:.0f}s (server hung)"
    except Exception as e:  # noqa: BLE001 - classification is the whole point
        return classify_error(e), f"{PROBE_TOOL}: {e}"
    auth = auth_error_in_payload(data)
    if auth:
        return "auth", f"{PROBE_TOOL}: {auth}"
    if not isinstance(data, dict) or "sections" not in data:
        return "error", f"{PROBE_TOOL}: unexpected reply {type(data).__name__}"
    return "ok", ""


# --------------------------------------------------------------------------- #
# Collection (async, injectable `call` for tests)                              #
# --------------------------------------------------------------------------- #
async def list_conversations(call: Call, max_conversations: int = MAX_CONVERSATIONS) -> list[dict]:
    """Sidebar channels plus open DMs/group DMs, as {id, name, kind, topic}.

    The sidebar listing also contains DMs (as generic entries), so the two lists
    are merged by id and the DM listing wins on kind/recency.
    """
    by_id: dict[str, dict] = {}
    chans = await call("list_my_channels", {})
    for section in chans.get("sections") or []:
        for c in section.get("channels") or []:
            if c.get("id"):
                by_id[c["id"]] = {"id": c["id"], "name": c.get("name") or c["id"], "kind": "channel",
                                  "topic": c.get("topic") or ""}
    dms = await call("list_dms", {"limit": 500})
    for d in dms.get("dms") or []:
        cid = d.get("channelId") or d.get("id")
        if not cid:
            continue
        entry = by_id.setdefault(cid, {"id": cid, "topic": ""})
        entry.update({"name": d.get("name") or entry.get("name") or cid,
                      "kind": "group_dm" if d.get("isGroup") else "dm",
                      "last_activity": d.get("lastActivity") or ""})
    # Deterministic and bounded: channels first, then DMs.
    ordered = [c for c in by_id.values() if c["kind"] == "channel"] + \
              [c for c in by_id.values() if c["kind"] != "channel"]
    return ordered[:max_conversations]


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_window(ts: str | None, since: str, until: str) -> bool:
    dt = _parse_ts(ts)
    lo, hi = _parse_ts(since), _parse_ts(until)
    return dt is not None and lo <= dt < hi


async def fetch_day(call: Call, conv: dict, day: date) -> tuple[dict, list[dict]]:
    """All messages (with thread replies) in ``conv`` on ``day``; returns (channel meta, messages)."""
    since, until = day_bounds_utc(day)
    messages: list[dict] = []
    meta: dict = {}
    cursor = None
    while len(messages) < MAX_MESSAGES_PER_DAY:
        params = {"channel": conv["id"], "limit": min(200, MAX_MESSAGES_PER_DAY - len(messages)),
                  "since": since, "until": until, "includeThreadReplies": True, "maxThreadsToFetch": MAX_THREADS}
        if cursor:
            params["cursor"] = cursor
        page = await call("get_messages", params)
        meta = page.get("channel") or meta
        batch = page.get("messages") or []
        # Defensive: the server should honour since/until, but never trust it blindly.
        messages.extend(m for m in batch if _in_window(m.get("timestamp"), since, until))
        cursor = page.get("nextCursor")
        if not batch or not page.get("hasMore") or not cursor:
            break
    return meta, messages


def _dm_inactive_for(conv: dict, days: list[date]) -> bool:
    """Skip DMs whose last activity predates the window (saves one call each)."""
    la = conv.get("last_activity") or ""
    if not la or conv.get("kind") == "channel":
        return False
    since, _ = day_bounds_utc(days[0])
    return la < since


def _one_line_err(exc: BaseException, limit: int = 160) -> str:
    return " ".join(str(exc).split())[:limit]


async def collect(call: Call, days: list[date], already: set[str],
                  max_conversations: int = MAX_CONVERSATIONS) -> list[tuple[str, str]]:
    items = CaptureBatch()
    items.rate_limited = 0
    convs = await list_conversations(call, max_conversations)
    logger.info("Slack: %d conversations, days %s", len(convs), [d.isoformat() for d in days])
    for conv in convs:
        if _dm_inactive_for(conv, days):
            continue
        for day in days:
            url = day_source_url(conv["id"], day)
            if url in already:
                continue
            try:
                meta, messages = await fetch_day(call, conv, day)
            except McpRateLimited as e:
                # The client already slept through its Retry-After budget; what
                # is still refused is a lost conversation-day, i.e. a failure.
                logger.warning("Slack conversation-day still rate limited after backoff (%s); counted as failed",
                               _one_line_err(e))
                items.failed += 1
                items.rate_limited += 1
                continue
            except McpToolError as e:
                logger.warning("Slack conversation-day fetch failed: %s", type(e).__name__)
                items.failed += 1
                continue
            if not messages:
                continue
            enriched = {**conv, "purpose": (meta or {}).get("purpose") or "",
                        "topic": (meta or {}).get("topic") or conv.get("topic") or ""}
            rendered = render_day(enriched, day, messages)
            if rendered:
                items.append(rendered)
                already.add(url)
    return items


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
async def _run_async(days_back: int, max_conversations: int, dry_run: bool) -> dict:
    from src.db import get_processed_source_urls
    from src.ingest import ingest_batch

    days = completed_days(days_back)
    already = set(get_processed_source_urls(SOURCE_TYPE))
    try:
        async with McpClient(SLACK_CMD, [], call_timeout=SLACK_CALL_TIMEOUT,
                             rate_limit_retries=SLACK_RATE_LIMIT_RETRIES,
                             rate_limit_max_wait=SLACK_RATE_LIMIT_MAX_WAIT,
                             rate_limit_wait_budget=SLACK_RATE_LIMIT_WAIT_BUDGET) as client:
            kind, detail = await preflight(client.call_json)
            if kind != "ok":
                # Do not attempt the capture: with a stale cookie every call fails.
                raise CaptureFailure(kind, detail, AUTH_FIX if kind == "auth" else None)
            items = await collect(client.call_json, days, already, max_conversations)
            throttle = {"hits": getattr(client, "rate_limit_hits", 0),
                        "waited_s": round(getattr(client, "rate_limit_waited", 0.0), 1)}
    except CaptureFailure:
        raise
    except Exception as e:  # noqa: BLE001 - classify everything (KeyError, ExceptionGroup, ...) then exit with the right code
        kind = classify_error(e)
        raise CaptureFailure(kind, str(e), AUTH_FIX if kind == "auth" else None) from e
    stats = run_channel(SOURCE_TYPE, items, dry_run=dry_run, ingest=ingest_batch)
    stats["rate_limited"] = getattr(items, "rate_limited", 0)
    stats["throttle"] = throttle
    if stats["rate_limited"]:
        logger.error("Slack: %d conversation-day(s) lost to rate limiting after %.0fs of Retry-After backoff "
                     "(%d throttled replies); run is partial", stats["rate_limited"], throttle["waited_s"],
                     throttle["hits"])
    logger.info("%s: %s", SOURCE_TYPE, stats)
    return stats


def capture(days_back: int = 2, max_conversations: int = MAX_CONVERSATIONS, dry_run: bool = False) -> dict:
    return asyncio.run(_run_async(days_back, max_conversations, dry_run))


def main() -> int:
    """Return the process exit code: 0 OK, 3 auth, 4 transport, 1 anything else."""
    ap = argparse.ArgumentParser(description="Capture Slack conversations into the Second Brain.")
    ap.add_argument("--days-back", type=int, default=2, help="completed local days to capture (default 2)")
    ap.add_argument("--max-conversations", type=int, default=MAX_CONVERSATIONS)
    ap.add_argument("--dry-run", action="store_true", help="fetch + list, do not ingest")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        stats = capture(days_back=args.days_back, max_conversations=args.max_conversations, dry_run=args.dry_run)
    except CaptureFailure as failure:
        return report_failure(failure, SOURCE_TYPE)
    logger.info("captures_this_cycle: slack=%d", stats.get("processed", 0))
    return exit_code(stats)


if __name__ == "__main__":
    sys.exit(main())
