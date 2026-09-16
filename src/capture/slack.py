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

    python -m src.capture.slack [--days-back N] [--dry-run] [--max-conversations N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from src.capture.mcp_client import McpClient, McpToolError

logger = logging.getLogger(__name__)

SOURCE_TYPE = "slack"
SLACK_CMD = os.environ.get("SLACK_MCP", os.path.expanduser("~/.aim/mcp-servers/slack-mcp"))

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
    return f"slack://{conversation_id}/{day.isoformat()}"


def _hhmm(ts: str | None) -> str:
    """ISO timestamp → local HH:MM."""
    if not ts:
        return "--:--"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
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
    return day_source_url(conv["id"], day), "\n".join(header) + "\n\n---\n\n" + body


def completed_days(days_back: int, today: date | None = None) -> list[date]:
    """The ``days_back`` most recent *completed* local days, oldest first."""
    today = today or datetime.now().astimezone().date()
    return [today - timedelta(days=i) for i in range(days_back, 0, -1)]


def day_bounds_utc(day: date) -> tuple[str, str]:
    """Local-midnight bounds of ``day`` as UTC ISO strings for since/until."""
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.astimezone(timezone.utc).strftime(fmt), end.astimezone(timezone.utc).strftime(fmt)


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


async def collect(call: Call, days: list[date], already: set[str],
                  max_conversations: int = MAX_CONVERSATIONS) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
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
            except McpToolError as e:
                logger.warning("get_messages failed for %s on %s: %s", conv.get("name"), day, e)
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
    async with McpClient(SLACK_CMD, []) as client:
        items = await collect(client.call_json, days, already, max_conversations)
    if dry_run:
        for url, _ in items:
            logger.info("[DRY] %s", url)
        return {"new": len(items)}
    stats = ingest_batch(items, SOURCE_TYPE) if items else {"processed": 0, "skipped": 0, "failed": 0}
    logger.info("%s: %s", SOURCE_TYPE, stats)
    return stats


def capture(days_back: int = 2, max_conversations: int = MAX_CONVERSATIONS, dry_run: bool = False) -> dict:
    return asyncio.run(_run_async(days_back, max_conversations, dry_run))


def main():
    ap = argparse.ArgumentParser(description="Capture Slack conversations into the Second Brain.")
    ap.add_argument("--days-back", type=int, default=2, help="completed local days to capture (default 2)")
    ap.add_argument("--max-conversations", type=int, default=MAX_CONVERSATIONS)
    ap.add_argument("--dry-run", action="store_true", help="fetch + list, do not ingest")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    stats = capture(days_back=args.days_back, max_conversations=args.max_conversations, dry_run=args.dry_run)
    logger.info("captures_this_cycle: slack=%d", stats.get("processed", stats.get("new", 0)))


if __name__ == "__main__":
    main()
