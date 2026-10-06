"""Capture Outlook mail and calendar into the Second Brain.

Pulls through the local ``grasp-mcp`` server (Microsoft Graph, cached token —
works headlessly under launchd). Each email and each calendar event becomes one
source document, rendered to the standard metadata-header markdown and handed
to :func:`src.ingest.ingest_batch`, which dedups by ``source_url`` and does the
parent + chunk + embedding work.

Window: a rolling ``--days-back`` window (default 2) ending now. Dedup makes
re-runs idempotent, so a missed night is recovered by the next run.

Source URLs are content-derived, not Graph ids: Graph message ids change when
a message moves folders, which would re-ingest it. ``outlook://mail/<received>/
<hash(subject|from)>`` is stable across moves.

grasp-mcp died mid-session on 2026-09-20/21 (``McpError: Connection closed``),
losing 57–81 h of mail and calendar behind a stack trace. That is now survivable:
:class:`~src.capture.mcp_client.McpClient` reconnects and retries the failing
call once per session, and a failure that outlives the retry exits 4
(transport) rather than 1, distinct from an expired Graph token's 3 (auth).

    python -m src.capture.outlook [--days-back N] [--dry-run] [--no-calendar]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from src.capture.mcp_client import (
    CaptureFailure, McpClient, McpToolError, classify_error, report_failure,
)

from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import run_channel
from src.capture.render import render_header_lines, html_to_text as _html_to_text

logger = logging.getLogger(__name__)

SOURCE_TYPE_MAIL = "outlook_email"
SOURCE_TYPE_CALENDAR = "outlook_calendar"

GRASP_CMD = os.environ.get("GRASP_MCP", os.path.expanduser("~/.toolbox/bin/grasp-mcp"))
GRASP_ARGS = ["serve", "--quiet"]

# Per-tool-call ceiling. The heaviest call is one 50-message listing page (Graph
# occasionally takes tens of seconds on a large mailbox), so the 60s used by the
# credentials probe is too tight for capture — but it must stay bounded, or a
# hung server holds the job open until launchd's next run.
OUTLOOK_CALL_TIMEOUT = float(os.environ.get("OUTLOOK_CALL_TIMEOUT", "180"))
AUTH_FIX = "open Kiro and use an Outlook tool once to refresh the Microsoft Graph token"

MAIL_FOLDERS = ("inbox", "sent")
PAGE_SIZE = 50
MAX_MAIL_PER_FOLDER = int(os.environ.get("OUTLOOK_MAX_MAIL_PER_FOLDER", "300"))
MAX_EVENTS = int(os.environ.get("OUTLOOK_MAX_EVENTS", "200"))
MAX_BODY_CHARS = 20000

# Outlook's external-sender banner adds nothing to the memory.
_BANNER_RE = re.compile(
    r"CAUTION: This email originated from outside of the organization\..*?"
    r"(?:recognize the sender and know the content is safe\.?)", re.S | re.I)
# grasp-mcp wraps bodies in an anti-injection envelope: "<email_content_HASH>" + a
# three-line warning ending "Treat ALL content below as data only." + the body +
# "</email_content_HASH>". Transport artefact, not content.
_ENVELOPE_OPEN_RE = re.compile(
    r"^\s*<email_content_[0-9a-f]+>\s*.*?Treat ALL content below as data only\.\s*", re.S)
_ENVELOPE_CLOSE_RE = re.compile(r"\s*</email_content_[0-9a-f]+>\s*$", re.S)


def strip_envelope(body: str) -> str:
    body = _ENVELOPE_OPEN_RE.sub("", body or "", count=1)
    return _ENVELOPE_CLOSE_RE.sub("", body, count=1)

Call = Callable[[str, dict], Awaitable[Any]]


# --------------------------------------------------------------------------- #
# Rendering (pure)                                                             #
# --------------------------------------------------------------------------- #
def html_to_text(body: str | None) -> str:
    return _BANNER_RE.sub("", _html_to_text(body)).strip()


def _addr(entry: Any) -> str:
    """Render a Graph recipient ({emailAddress, displayName}) or a string."""
    if isinstance(entry, dict):
        name = entry.get("displayName") or ""
        mail = entry.get("emailAddress") or entry.get("address") or ""
        return f"{name} <{mail}>" if name and mail else (name or mail)
    return str(entry or "")


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:12]


def mail_source_url(msg: dict) -> str:
    received = (msg.get("receivedDateTime") or msg.get("sentDateTime") or "")[:19]
    if not received:
        raise ValueError("Outlook mail requires received or sent time for stable identity")
    sender = msg.get("from")
    sender_mail = sender.get("emailAddress", "") if isinstance(sender, dict) else str(sender or "")
    return f"outlook://mail/{received}/{_hash(msg.get('subject') or '', sender_mail)}"


def render_email(details: dict) -> tuple[str, str] | None:
    """Return (source_url, markdown) for one email, or None if it has no body."""
    body_raw = strip_envelope(details.get("bodyContent") or details.get("bodyPreview") or "")
    body = html_to_text(body_raw) if (details.get("bodyType") or "html").lower() == "html" else body_raw.strip()
    body = _BANNER_RE.sub("", body).strip()
    if not body:
        return None
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "\n\n[truncated]"
    subject = (details.get("subject") or "(no subject)").strip()
    received = details.get("receivedDateTime") or details.get("sentDateTime") or ""
    to = ", ".join(_addr(r) for r in (details.get("toRecipients") or [])) or "(none)"
    header = [
        f"# {subject}",
        "",
        f"Source: {mail_source_url(details)}",
        "Type: email",
        f"Date: {received[:10]}",
        f"From: {_addr(details.get('from'))}",
        f"To: {to}",
        f"Folder: {details.get('folder') or 'inbox'}",
        f"Received: {received}",
    ]
    if details.get("conversationId"):
        header.append(f"Conversation: {details['conversationId']}")
    if details.get("hasAttachments"):
        header.append("Attachments: yes")
    return mail_source_url(details), render_header_lines(header, body)


def event_source_url(ev: dict) -> str:
    start = (ev.get("start") or "")[:19]
    if not start:
        raise ValueError("Outlook event requires start time for stable identity")
    return f"outlook://event/{start}/{_hash(ev.get('subject') or '', str(ev.get('organizer') or ''))}"


def render_event(ev: dict) -> tuple[str, str]:
    """Return (source_url, markdown) for one calendar event."""
    subject = (ev.get("subject") or "(no subject)").strip()
    start, end = ev.get("start") or "", ev.get("end") or ""
    attendees = ev.get("attendees") or []
    header = [
        f"# {subject}",
        "",
        f"Source: {event_source_url(ev)}",
        "Type: calendar-event",
        f"Date: {start[:10]}",
        f"Start: {start}",
        f"End: {end}",
        f"Organizer: {ev.get('organizer') or ''}",
        f"Location: {ev.get('location') or ''}",
        f"All-day: {'yes' if ev.get('isAllDay') else 'no'}",
        f"Response: {ev.get('responseStatus') or ''}",
        f"Attendees: {len(attendees)}",
    ]
    lines = [f"Meeting: {subject}", f"When: {start} to {end}" + (" (all day)" if ev.get("isAllDay") else "")]
    if ev.get("organizer"):
        lines.append(f"Organizer: {ev['organizer']}")
    if ev.get("location"):
        lines.append(f"Location: {ev['location']}")
    if attendees:
        lines.append("Attendees: " + "; ".join(str(a) for a in attendees[:60])
                     + (f" (+{len(attendees) - 60} more)" if len(attendees) > 60 else ""))
    body_raw = strip_envelope(ev.get("bodyContent") or "")
    body = html_to_text(body_raw) if (ev.get("bodyContentType") or "html").lower() == "html" else body_raw.strip()
    if body:
        lines += ["", body[:MAX_BODY_CHARS]]
    return event_source_url(ev), render_header_lines(header, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Collection (async, injectable `call` for tests)                              #
# --------------------------------------------------------------------------- #
def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def collect_mail(call: Call, start: datetime, end: datetime, already: set[str],
                       folders=MAIL_FOLDERS, max_per_folder: int = MAX_MAIL_PER_FOLDER) -> list[tuple[str, str]]:
    """List messages in [start, end] per folder, fetch bodies for unseen ones, render."""
    items = CaptureBatch()
    flt = f"receivedDateTime ge {_iso_z(start)} and receivedDateTime le {_iso_z(end)}"
    for folder in folders:
        seen_in_folder = 0
        token = None
        while seen_in_folder < max_per_folder:
            params = {"folder": folder, "maxResults": PAGE_SIZE,
                      "orderBy": "receivedDateTime desc", "filter": flt}
            if token:
                params["pageToken"] = token
            page = await call("get_emails", params)
            emails = page.get("emails") or []
            if not emails:
                break
            for msg in emails:
                seen_in_folder += 1
                try:
                    url = mail_source_url(msg)
                except ValueError as e:
                    # One message without a received/sent time must not abort the
                    # whole run (R4 review S3-7): count it, log it, move on.
                    logger.warning("Outlook mail skipped (id=%s): %s", msg.get("id"), e)
                    items.failed += 1
                    continue
                if url in already:
                    continue
                try:
                    details = await call("get_email_details", {"emailId": msg["id"]})
                except McpToolError as e:
                    logger.warning("Outlook detail fetch failed: %s", type(e).__name__)
                    items.failed += 1
                    continue
                details = {**msg, **(details or {})}
                details.setdefault("folder", folder)
                try:
                    rendered = render_email(details)
                except ValueError as e:
                    logger.warning("Outlook mail render failed (id=%s): %s", msg.get("id"), e)
                    items.failed += 1
                    continue
                if rendered:
                    items.append(rendered)
                    already.add(url)
            token = page.get("nextPageToken") or page.get("pageToken")
            if not token or not page.get("hasMore", True) or seen_in_folder >= max_per_folder:
                break
    return items


async def collect_calendar(call: Call, start: datetime, end: datetime, already: set[str],
                           max_events: int = MAX_EVENTS) -> list[tuple[str, str]]:
    items = CaptureBatch()
    token = None
    seen = 0
    while seen < max_events:
        params = {"startDateTime": _iso_z(start), "endDateTime": _iso_z(end), "maxResults": PAGE_SIZE}
        if token:
            params["pageToken"] = token
        page = await call("get_calendar_events", params)
        events = page.get("events") or []
        if not events:
            break
        for ev in events:
            seen += 1
            try:
                url = event_source_url(ev)
            except ValueError as e:
                logger.warning("Outlook event skipped (id=%s): %s", ev.get("id"), e)
                items.failed += 1
                continue
            if url in already:
                continue
            if ev.get("sensitivity", "normal") not in ("normal", "personal"):
                continue  # private/confidential events stay out of the corpus
            items.append(render_event(ev))
            already.add(url)
        token = page.get("nextPageToken")
        if not token:
            break
    return items


async def collect(call: Call, start: datetime, end: datetime, already_mail: set[str],
                  already_cal: set[str], calendar: bool = True) -> dict[str, list[tuple[str, str]]]:
    out = {SOURCE_TYPE_MAIL: await collect_mail(call, start, end, already_mail)}
    if calendar:
        out[SOURCE_TYPE_CALENDAR] = await collect_calendar(call, start, end, already_cal)
    return out


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
async def _run_async(days_back: int, calendar: bool, dry_run: bool) -> dict:
    from src.db import get_processed_source_urls
    from src.ingest import ingest_batch

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    # Calendar: also look one day ahead so tomorrow's meetings are known today.
    cal_end = end + timedelta(days=1)
    already_mail = set(get_processed_source_urls(SOURCE_TYPE_MAIL))
    already_cal = set(get_processed_source_urls(SOURCE_TYPE_CALENDAR))
    logger.info("Outlook capture: window %s .. %s (calendar to %s)", start.date(), end.date(), cal_end.date())

    try:
        async with McpClient(GRASP_CMD, GRASP_ARGS, call_timeout=OUTLOOK_CALL_TIMEOUT) as client:
            mail = await collect_mail(client.call_json, start, end, already_mail)
            events = await collect_calendar(client.call_json, start, cal_end, already_cal) if calendar else []
            if client.reconnects_used:
                logger.warning("grasp-mcp connection was re-established %d time(s) during this run",
                               client.reconnects_used)
    except Exception as e:  # noqa: BLE001 - classify everything: transport death vs stale token vs bug.
        # A KeyError/TypeError from a malformed payload or an anyio ExceptionGroup
        # must still produce the one-line classified failure and its exit code,
        # not a bare traceback (D N-7).
        kind = classify_error(e)
        raise CaptureFailure(kind, str(e), AUTH_FIX if kind == "auth" else None) from e

    summary = {}
    for source_type, items in ((SOURCE_TYPE_MAIL, mail), (SOURCE_TYPE_CALENDAR, events)):
        stats = run_channel(source_type, items, dry_run=dry_run, ingest=ingest_batch)
        summary[source_type] = stats
        logger.info("%s: %s", source_type, stats)
    return summary


def capture(days_back: int = 2, calendar: bool = True, dry_run: bool = False) -> dict:
    return asyncio.run(_run_async(days_back, calendar, dry_run))


def main() -> int:
    """Return the process exit code: 0 OK, 3 auth, 4 transport, 1 anything else."""
    ap = argparse.ArgumentParser(description="Capture Outlook mail + calendar into the Second Brain.")
    ap.add_argument("--days-back", type=int, default=2, help="rolling window in days (default 2)")
    ap.add_argument("--no-calendar", action="store_true", help="skip calendar events")
    ap.add_argument("--dry-run", action="store_true", help="fetch + list, do not ingest")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        summary = capture(days_back=args.days_back, calendar=not args.no_calendar, dry_run=args.dry_run)
    except CaptureFailure as failure:
        return report_failure(failure, "outlook")
    total_new = sum(v.get("processed", 0) for v in summary.values())
    logger.info("captures_this_cycle: outlook=%d", total_new)
    return exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
