#!/usr/bin/env python3
"""Ingest Quick Desktop chat sessions into the Second Brain.

Reads from ~/.quickwork/sessions/sessions.db, formats conversations
as markdown, and ingests through the standard pipeline.

Usage: .venv/bin/python -m src.capture.qd_chats [--dry-run] [--limit N] [--min-messages 2]
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

from src.capture.render import render_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from src.qd_profile import qd_path

QD_SESSIONS_DB = qd_path("sessions", "sessions.db")
SOURCE_TYPE = "quick_desktop_chat"


def open_db():
    conn = sqlite3.connect(f"file:{QD_SESSIONS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# F-094: Quick Desktop marks private and incognito conversations with these
# authoritative per-session flags. Such a session is never captured. A store
# whose schema predates a flag cannot hold a session carrying it.
PRIVACY_FLAGS = ("private", "incognito")


def eligible_sessions(conn, min_messages=2):
    """Sessions eligible for capture: enough messages, not deleted, not private/incognito.

    ``archived`` and non-``main`` ``session_type`` sessions are not excluded
    (user-archived conversations remain the user's own record; see F-094).
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    privacy = "".join(f" AND COALESCE({flag}, 0) = 0" for flag in PRIVACY_FLAGS if flag in columns)
    return conn.execute(f"""SELECT id,title,created_at,updated_at,message_count,agent_mode
        FROM sessions WHERE message_count >= ? AND deleted_at IS NULL{privacy}
        ORDER BY updated_at DESC""", (min_messages,)).fetchall()


def format_session_as_markdown(session, messages):
    """Format a QD session + messages as markdown with metadata header."""
    created = datetime.fromtimestamp(session["created_at"], tz=timezone.utc)
    title = session["title"] or "Untitled"

    lines = []

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

    return render_document(title, {"Source": f"qd-chat://{session['id']}", "Source_Type": SOURCE_TYPE,
        "Source_ID": session["id"], "Date": created.strftime("%Y-%m-%d"),
        "Agent_Mode": session["agent_mode"] or "standard"}, "\n".join(lines))



def capture(*, dry_run=False, limit=0, min_messages=2):
    from src.capture.framework import run_channel
    from src.capture.common import CaptureBatch
    if not os.path.isfile(QD_SESSIONS_DB):
        return {"processed": 0, "skipped": 0, "failed": 1}
    conn = open_db()
    items = CaptureBatch()
    try:
        sessions = eligible_sessions(conn, min_messages)
        for session in sessions[:limit] if limit else sessions:
            messages = conn.execute("""SELECT role,content,timestamp,tool_names FROM session_messages
                WHERE session_id = ? ORDER BY timestamp""", (session["id"],)).fetchall()
            if messages:
                items.append((f"qd-chat://{session['id']}", format_session_as_markdown(session, messages)))
    finally:
        conn.close()
    return run_channel(SOURCE_TYPE, items, dry_run=dry_run, refresh=True)


def main():
    from src.capture.common import exit_code
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-messages", type=int, default=2)
    args = parser.parse_args()
    result = capture(dry_run=args.dry_run, limit=args.limit, min_messages=args.min_messages)
    log.info("%s", result)
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
