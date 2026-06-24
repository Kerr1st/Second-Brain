#!/usr/bin/env python3
"""Backfill project tags on existing memories.

Re-derives project tags from original source data and updates the
`project` column on memories that currently have project = NULL.

Phases:
  A) IDE chat memories — re-parse .chat files from disk, fall back to
     Project: header in memory content.
  B) CLI chat memories — read metadata->>'source_id' (workspace path),
     fall back to CLI SQLite DB.
  C) Skip non-chat memories (youtube, manual, article) — leave NULL.

Usage:
  python scripts/backfill_projects.py [--dry-run]
"""

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db import get_connection
from src.parsers.ide_chat import extract_project_context, find_chat_files
from src.project import normalize_project_tag

# CLI SQLite DB path (macOS)
CLI_DB_PATH = os.path.expanduser(
    "~/Library/Application Support/kiro-cli/data.sqlite3"
)

BATCH_SIZE = 500

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(LOG_DIR, f"backfill_projects-{date_str}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_chat_file_index():
    """Build a dict mapping chat filename (no extension) → full path on disk."""
    index = {}
    for path in find_chat_files():
        name = os.path.basename(path).replace(".chat", "")
        index[name] = path
    return index


def _extract_project_from_chat_file(filepath):
    """Re-parse a .chat file and return the normalized project_hint, or None."""
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    meta = extract_project_context(data)
    return meta.get("project_hint")  # already normalized by the parser


def _extract_project_from_content(content):
    """Parse a 'Project:' header from memory content text. Returns normalized tag or None."""
    if not content:
        return None
    match = re.search(r"^Project:\s*(.+)$", content, re.MULTILINE)
    if match:
        return normalize_project_tag(match.group(1))
    return None


def _source_url_to_chat_name(source_url):
    """Convert a source_url like 'ide_<chatname>.md' to the chat filename."""
    if not source_url:
        return None
    name = source_url
    # Strip ide_ prefix and .md suffix
    if name.startswith("ide_"):
        name = name[4:]
    if name.endswith(".md"):
        name = name[:-3]
    return name if name else None


def _get_cli_conversation_ids():
    """Read conversation_id values from the CLI SQLite DB. Returns dict {conversation_id: True}."""
    if not os.path.exists(CLI_DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(CLI_DB_PATH)
        rows = conn.execute(
            "SELECT conversation_id FROM conversations_v2"
        ).fetchall()
        conn.close()
        return {row[0]: True for row in rows}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Phase A: IDE Chat Backfill
# ---------------------------------------------------------------------------

def backfill_ide_chats(conn, dry_run, log):
    """Backfill project tags for IDE chat memories."""
    stats = {"updated": 0, "excluded": 0, "left_null": 0, "errors": 0}

    # Build index of .chat files on disk
    chat_index = _build_chat_file_index()
    log.info(f"  Found {len(chat_index)} .chat files on disk")

    # Fetch all IDE chat parent memories
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, source_url, content, metadata
            FROM memories
            WHERE source_type = 'kiro_ide_chat'
              AND parent_id IS NULL
            ORDER BY created_at
        """)
        parents = cur.fetchall()

    log.info(f"  Found {len(parents)} IDE chat parent memories")

    batch_count = 0
    for mem_id, source_url, content, metadata_raw in parents:
        try:
            project = None

            # Strategy 1: Re-parse .chat file from disk
            chat_name = _source_url_to_chat_name(source_url)
            if chat_name and chat_name in chat_index:
                project = _extract_project_from_chat_file(chat_index[chat_name])

            # Strategy 2: Fall back to Project: header in content
            if project is None:
                project = _extract_project_from_content(content)

            if project is None:
                stats["left_null"] += 1
                continue

            # normalize_project_tag already applied by the extraction functions,
            # but apply once more for safety (idempotent)
            project = normalize_project_tag(project)
            if project is None:
                stats["excluded"] += 1
                continue

            if not dry_run:
                with conn.cursor() as cur:
                    # Update parent
                    cur.execute(
                        "UPDATE memories SET project = %s, updated_at = now() WHERE id = %s",
                        (project, mem_id),
                    )
                    # Update children
                    cur.execute(
                        "UPDATE memories SET project = %s, updated_at = now() WHERE parent_id = %s",
                        (project, mem_id),
                    )

            stats["updated"] += 1
            batch_count += 1

            if batch_count >= BATCH_SIZE and not dry_run:
                conn.commit()
                batch_count = 0

        except Exception as e:
            log.error(f"  Error processing IDE memory {mem_id}: {e}")
            stats["errors"] += 1

    # Final commit for remaining batch
    if not dry_run and batch_count > 0:
        conn.commit()

    return stats


# ---------------------------------------------------------------------------
# Phase B: CLI Chat Backfill
# ---------------------------------------------------------------------------

def backfill_cli_chats(conn, dry_run, log):
    """Backfill project tags for CLI chat memories."""
    stats = {"updated": 0, "excluded": 0, "left_null": 0, "errors": 0}

    # Fetch all CLI chat parent memories
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, metadata
            FROM memories
            WHERE source_type = 'kiro_cli_chat'
              AND parent_id IS NULL
            ORDER BY created_at
        """)
        parents = cur.fetchall()

    log.info(f"  Found {len(parents)} CLI chat parent memories")

    # Lazy-load CLI SQLite conversation IDs only if needed
    cli_conversations = None

    batch_count = 0
    for mem_id, metadata_raw in parents:
        try:
            workspace_path = None

            # Strategy 1: Read source_id from metadata JSONB
            if metadata_raw:
                meta = metadata_raw if isinstance(metadata_raw, dict) else json.loads(metadata_raw)
                workspace_path = meta.get("source_id")

            # Strategy 2: Fall back to CLI SQLite DB
            if not workspace_path:
                if cli_conversations is None:
                    cli_conversations = _get_cli_conversation_ids()
                    log.info(f"  Loaded {len(cli_conversations)} conversations from CLI SQLite DB")
                # We can't directly match without a conversation_id, so skip
                # (the source_id metadata is the primary matching mechanism)
                stats["left_null"] += 1
                continue

            project = normalize_project_tag(workspace_path)

            if project is None:
                stats["excluded"] += 1
                continue

            if not dry_run:
                with conn.cursor() as cur:
                    # Update parent
                    cur.execute(
                        "UPDATE memories SET project = %s, updated_at = now() WHERE id = %s",
                        (project, mem_id),
                    )
                    # Update children
                    cur.execute(
                        "UPDATE memories SET project = %s, updated_at = now() WHERE parent_id = %s",
                        (project, mem_id),
                    )

            stats["updated"] += 1
            batch_count += 1

            if batch_count >= BATCH_SIZE and not dry_run:
                conn.commit()
                batch_count = 0

        except Exception as e:
            log.error(f"  Error processing CLI memory {mem_id}: {e}")
            stats["errors"] += 1

    # Final commit for remaining batch
    if not dry_run and batch_count > 0:
        conn.commit()

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    log = setup_logging()

    log.info("=== Backfill Project Tags ===")
    log.info(f"Dry run: {dry_run}")

    with get_connection() as conn:
        # Phase A: IDE chats
        log.info("Phase A: IDE chat backfill")
        ide_stats = backfill_ide_chats(conn, dry_run, log)
        log.info(
            f"  IDE results: updated={ide_stats['updated']}, "
            f"excluded={ide_stats['excluded']}, "
            f"left_null={ide_stats['left_null']}, "
            f"errors={ide_stats['errors']}"
        )

        # Phase B: CLI chats
        log.info("Phase B: CLI chat backfill")
        cli_stats = backfill_cli_chats(conn, dry_run, log)
        log.info(
            f"  CLI results: updated={cli_stats['updated']}, "
            f"excluded={cli_stats['excluded']}, "
            f"left_null={cli_stats['left_null']}, "
            f"errors={cli_stats['errors']}"
        )

        # Phase C: Non-chat memories are simply not queried — they stay NULL
        log.info("Phase C: Non-chat memories skipped (left as NULL)")

        # Summary
        total_updated = ide_stats["updated"] + cli_stats["updated"]
        total_excluded = ide_stats["excluded"] + cli_stats["excluded"]
        total_null = ide_stats["left_null"] + cli_stats["left_null"]
        total_errors = ide_stats["errors"] + cli_stats["errors"]

        log.info("=== Summary ===")
        log.info(f"  Total updated:  {total_updated}")
        log.info(f"  Total excluded: {total_excluded} (dot-prefix / home dir)")
        log.info(f"  Total left NULL: {total_null}")
        log.info(f"  Total errors:   {total_errors}")

        if dry_run:
            log.info("  (dry run — no changes committed)")



if __name__ == "__main__":
    main()
