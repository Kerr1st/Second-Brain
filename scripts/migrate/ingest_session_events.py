#!/usr/bin/env python3
"""Ingest Quick Desktop session_events (message_user) into Second Brain.

Reads ~/.quickwork/sessions/sessions.db, extracts message_user events,
filters short messages, and creates one memory per meaningful user prompt.

Usage: .venv/bin/python scripts/migrate/ingest_session_events.py [--dry-run]

Idempotent — uses source_url 'qd-session-event://{event_id}' for deduplication.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import create_memory, get_processed_source_urls
from src.embeddings import generate_embedding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SESSIONS_DB_PATH = os.path.expanduser("~/.quickwork/sessions/sessions.db")
SOURCE_TYPE = "quick_desktop_session_event"
MIN_CONTENT_LENGTH = 20


def parse_event_content(data_str):
    """Extract content text from the JSON data column."""
    if not data_str:
        return ""
    try:
        data = json.loads(data_str)
        return data.get("content", "")
    except (json.JSONDecodeError, TypeError):
        return ""


def source_url_for_event(event_id):
    """Generate dedup source_url for a session event."""
    return f"qd-session-event://{event_id}"


def is_meaningful_message(content):
    """Filter out short/trivial messages."""
    return len(content) >= MIN_CONTENT_LENGTH


def format_memory_content(content, session_title, timestamp):
    """Format user message as memory content with context."""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"[Session: {session_title}] [{dt}]\n\n{content}"


def ingest_session_events(dry_run=False):
    """Ingest message_user events from sessions.db. Returns stats dict."""
    if not os.path.exists(SESSIONS_DB_PATH):
        log.error(f"Sessions DB not found: {SESSIONS_DB_PATH}")
        return {"processed": 0, "skipped": 0, "filtered": 0, "failed": 0}

    conn = sqlite3.connect(SESSIONS_DB_PATH)
    conn.row_factory = sqlite3.Row

    # Load session titles
    sessions = {}
    for row in conn.execute("SELECT id, title FROM sessions"):
        sessions[row["id"]] = row["title"]

    # Load message_user events
    events = conn.execute(
        "SELECT id, session_id, timestamp, data FROM session_events WHERE type = 'message_user'"
    ).fetchall()
    conn.close()

    log.info(f"Found {len(events)} message_user events across {len(sessions)} sessions")

    already = get_processed_source_urls(SOURCE_TYPE) if not dry_run else set()
    stats = {"processed": 0, "skipped": 0, "filtered": 0, "failed": 0}

    for event in events:
        event_id = event["id"]
        url = source_url_for_event(event_id)

        if url in already:
            stats["skipped"] += 1
            continue

        content = parse_event_content(event["data"])
        if not is_meaningful_message(content):
            stats["filtered"] += 1
            continue

        if dry_run:
            stats["processed"] += 1
            continue

        try:
            session_title = sessions.get(event["session_id"], "Unknown Session")
            md = format_memory_content(content, session_title, event["timestamp"])
            embedding = generate_embedding(md)

            create_memory(
                type="source",
                title=content[:100],
                content=md,
                embedding=embedding,
                tags=["qd-session", "user-prompt"],
                source_url=url,
                source_type=SOURCE_TYPE,
                metadata={
                    "session_id": event["session_id"],
                    "session_title": session_title,
                    "event_timestamp": event["timestamp"],
                },
            )
            stats["processed"] += 1
        except Exception as e:
            stats["failed"] += 1
            log.error(f"Failed {event_id}: {e}")

    log.info(f"Done: processed={stats['processed']} skipped={stats['skipped']} filtered={stats['filtered']} failed={stats['failed']}")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ingest_session_events(dry_run=args.dry_run)
    if args.dry_run:
        log.info("DRY RUN — no changes written")


if __name__ == "__main__":
    main()
