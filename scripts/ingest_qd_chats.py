#!/usr/bin/env python3
"""Ingest Quick Desktop chat sessions into the Second Brain.

Reads from ~/.quickwork/sessions/sessions.db, formats conversations
as markdown, and ingests through the standard pipeline.

Usage: .venv/bin/python scripts/ingest_qd_chats.py [--dry-run] [--limit N] [--min-messages 2]
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import ingest_content
from src.db import get_processed_source_urls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from src.qd_profile import qd_path

QD_SESSIONS_DB = qd_path("sessions", "sessions.db")
SOURCE_TYPE = "quick_desktop_chat"


def open_db():
    conn = sqlite3.connect(f"file:{QD_SESSIONS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def format_session_as_markdown(session, messages):
    """Format a QD session + messages as markdown with metadata header."""
    created = datetime.fromtimestamp(session["created_at"], tz=timezone.utc)
    title = session["title"] or "Untitled"

    lines = [
        f"# {title}",
        "",
        f"Source-Type: {SOURCE_TYPE}",
        f"Source-ID: {session['id']}",
        f"Date: {created.strftime('%Y-%m-%d')}",
        f"Agent-Mode: {session['agent_mode'] or 'standard'}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = msg["role"].capitalize()
        content = msg["content"]
        # Skip very long tool outputs (keep first 2000 chars)
        if msg["role"] == "tool" or (msg["tool_names"] and msg["role"] == "assistant"):
            if len(content) > 2000:
                content = content[:2000] + "\n\n[... truncated tool output ...]"
        lines.append(f"**{role}:**\n")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-messages", type=int, default=2,
                        help="Skip sessions with fewer messages")
    args = parser.parse_args()

    if not os.path.exists(QD_SESSIONS_DB):
        log.error(f"QD sessions DB not found: {QD_SESSIONS_DB}")
        sys.exit(1)

    already = get_processed_source_urls(SOURCE_TYPE)
    log.info(f"Already ingested: {len(already)} QD chats")

    conn = open_db()

    sessions = conn.execute("""
        SELECT id, title, created_at, updated_at, message_count, agent_mode
        FROM sessions
        WHERE message_count >= ? AND deleted_at IS NULL
        ORDER BY created_at
    """, (args.min_messages,)).fetchall()

    log.info(f"Found {len(sessions)} QD sessions with >= {args.min_messages} messages")

    if args.limit:
        sessions = sessions[:args.limit]

    processed = 0
    skipped = 0
    failed = 0
    start = time.time()

    for i, session in enumerate(sessions):
        source_url = f"qd-chat://{session['id']}"

        if source_url in already:
            skipped += 1
            continue

        messages = conn.execute("""
            SELECT role, content, timestamp, tool_names
            FROM session_messages
            WHERE session_id = ?
            ORDER BY timestamp
        """, (session["id"],)).fetchall()

        if not messages:
            skipped += 1
            continue

        md = format_session_as_markdown(session, messages)

        if args.dry_run:
            processed += 1
            continue

        try:
            result = ingest_content(md, SOURCE_TYPE, source_url)
            if result:
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            log.error(f"Failed {session['id']}: {e}")

        if (i + 1) % 50 == 0:
            elapsed = time.time() - start
            rate = (processed + skipped + failed) / max(elapsed, 1)
            remaining = (len(sessions) - i - 1) / max(rate, 0.01)
            log.info(
                f"Progress: {i+1}/{len(sessions)} | "
                f"processed={processed} skipped={skipped} failed={failed} | "
                f"ETA {remaining/60:.0f}m"
            )

    elapsed = time.time() - start
    log.info(f"Done in {elapsed:.0f}s: processed={processed} skipped={skipped} failed={failed}")
    conn.close()


if __name__ == "__main__":
    main()
