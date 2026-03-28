#!/usr/bin/env python3
"""Import Claude conversation exports into the Second Brain.

Reads conversations.json from an unzipped Claude export directory,
filters and formats conversations as markdown, and ingests via the
existing pipeline.

Usage: python scripts/migrate/migrate_claude.py <export_path> [--dry-run] [--limit N]
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate.migration_utils import (
    format_chat_as_markdown,
    passes_chat_filter,
    run_migration,
    MigrationError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def _extract_text(content_blocks):
    """Extract plain text from Claude's content block array, skipping tool-use blocks."""
    if isinstance(content_blocks, str):
        return content_blocks
    if not isinstance(content_blocks, list):
        return ""
    parts = []
    for block in content_blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def parse_claude_export(export_path):
    """Yield (source_url, markdown) for each conversation passing filters."""
    conversations_file = os.path.join(export_path, "conversations.json")
    with open(conversations_file) as f:
        conversations = json.load(f)

    for conv in conversations:
        uuid = conv.get("uuid", "")
        name = conv.get("name", "Untitled")
        created_at = conv.get("created_at", "")
        updated_at = conv.get("updated_at", "")

        messages = []
        for msg in conv.get("chat_messages", []):
            sender = msg.get("sender", "")
            if sender not in ("human", "assistant"):
                continue
            text = _extract_text(msg.get("content", msg.get("text", "")))
            if text.strip():
                role = "human" if sender == "human" else "bot"
                messages.append((role, text))

        if not passes_chat_filter(messages):
            continue

        date_str = created_at[:10] if created_at else "unknown"
        source_url = f"claude://{uuid}"

        metadata = {}

        # Extract model if available
        model = conv.get("model")

        extra_fields = {}
        if model:
            extra_fields["Model"] = model
        extra_fields["Message-Count"] = str(len(messages))
        extra_fields["Original-Created-At"] = created_at
        extra_fields["Original-Updated-At"] = updated_at

        markdown = format_chat_as_markdown(
            f"Chat: {name}", messages, "claude_chat", source_url, date_str,
            extra_fields=extra_fields,
        )
        yield source_url, markdown


def main():
    parser = argparse.ArgumentParser(description="Import Claude conversations into Second Brain")
    parser.add_argument("export_path", help="Path to unzipped Claude export directory")
    parser.add_argument("--dry-run", action="store_true", help="Parse and filter without writing to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max conversations to process")
    args = parser.parse_args()

    try:
        run_migration(
            name="Claude",
            export_path=args.export_path,
            parse_fn=parse_claude_export,
            source_type="claude_chat",
            expected_file="conversations.json",
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except MigrationError as e:
        logging.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
