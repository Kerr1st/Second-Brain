"""Shared utilities for memory migration scripts.

Provides content filtering, markdown formatting, deduplication, and the
shared CLI entrypoint used by migrate_claude.py, migrate_chatgpt.py,
and migrate_notion.py.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_processed_source_urls, is_reachable
from src.ingest import ingest_content

logger = logging.getLogger(__name__)

# Thresholds matching src/parsers/cli_chat.py and src/parsers/ide_chat.py
MIN_CONTENT_CHARS = 200
MIN_USER_MESSAGES = 2
MIN_PARAGRAPH_WORDS = 50


def format_markdown_header(title, source_type, source_url, date_str, extra_fields=None):
    """Build metadata header + separator matching cli_chat.py:format_as_markdown."""
    lines = [
        f"# {title}",
        "",
        f"Source-Type: {source_type}",
        f"Source-ID: {source_url}",
        f"Date: {date_str}",
    ]
    for key, value in (extra_fields or {}).items():
        lines.append(f"{key}: {value}")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def format_chat_as_markdown(title, messages, source_type, source_url, date_str, extra_fields=None):
    """Format a conversation as markdown with metadata header.

    Args:
        messages: list of (role, content) tuples where role is 'human' or 'bot'/'assistant'.
    """
    header = format_markdown_header(title, source_type, source_url, date_str, extra_fields)
    body_lines = []
    for role, content in messages:
        label = "**User:**" if role in ("human", "user") else "**Assistant:**"
        body_lines.extend([label, "", content.strip(), ""])
    return header + "\n".join(body_lines)


def passes_chat_filter(messages):
    """Check chat meets minimum thresholds. Same logic as cli_chat.py filters.

    Args:
        messages: list of (role, content) tuples.
    """
    user_messages = [c for r, c in messages if r in ("human", "user")]
    if len(user_messages) < MIN_USER_MESSAGES:
        return False

    total_chars = sum(len(c) for _, c in messages)
    if total_chars < MIN_CONTENT_CHARS:
        return False

    bot_messages = [c for r, c in messages if r not in ("human", "user")]
    for content in bot_messages:
        for para in content.split("\n\n"):
            if len(para.split()) >= MIN_PARAGRAPH_WORDS:
                return True

    return False


def passes_page_filter(body):
    """Check non-chat content meets minimum length."""
    return len(body.strip()) >= MIN_CONTENT_CHARS


class MigrationError(Exception):
    """Raised when migration cannot proceed (missing path, DB unreachable, etc.)."""


def run_migration(name, export_path, parse_fn, source_type, expected_file=None,
                  dry_run=False, limit=None):
    """Shared entrypoint for all migration scripts.

    Args:
        name: Human-readable source name for logging (e.g., "Claude").
        export_path: Path to the export directory.
        parse_fn: Generator function(export_path) yielding (source_url, markdown) tuples.
        source_type: e.g., 'claude_chat', 'chatgpt_chat', 'notion_page'.
        expected_file: If set, validate this file exists in export_path. None for directory-only validation.
        dry_run: If True, parse and filter but don't write to DB.
        limit: Optional cap on items yielded by the parser (total scanned, not just processed).

    Raises:
        MigrationError: If export path is invalid, expected file is missing, or DB is unreachable.
    """
    # Validate export path
    if not os.path.isdir(export_path):
        raise MigrationError(f"{name} export path does not exist: {export_path}")

    if expected_file:
        target = os.path.join(export_path, expected_file)
        if not os.path.isfile(target):
            raise MigrationError(f"Expected {expected_file} in {export_path} but not found")

    # Check DB connectivity (skip in dry-run)
    if not dry_run and not is_reachable():
        raise MigrationError("PostgreSQL not reachable")

    # Load dedup set
    already = get_processed_source_urls(source_type) if not dry_run else set()

    stats = {"processed": 0, "skipped_dup": 0, "skipped_filter": 0, "failed": 0}
    total = 0

    for source_url, markdown in parse_fn(export_path):
        if limit is not None and total >= limit:
            break

        total += 1

        if source_url in already:
            stats["skipped_dup"] += 1
            continue

        if dry_run:
            stats["processed"] += 1
            logger.info("[DRY RUN] Would import: %s", source_url)
        else:
            try:
                result = ingest_content(markdown, source_type=source_type, source_url=source_url)
                if result:
                    stats["processed"] += 1
                    already.add(source_url)
                else:
                    stats["skipped_filter"] += 1
            except Exception as e:
                logger.error("Failed to import %s: %s", source_url, e, exc_info=True)
                stats["failed"] += 1

        if total % 100 == 0:
            logger.info("Progress: %d items (%s)", total, stats)

    print(f"\n{name} migration complete:")
    print(f"  Processed:        {stats['processed']}")
    print(f"  Skipped (dup):    {stats['skipped_dup']}")
    print(f"  Skipped (filter): {stats['skipped_filter']}")
    print(f"  Failed:           {stats['failed']}")
    print(f"  Total scanned:    {total}")
