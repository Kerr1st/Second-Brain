#!/usr/bin/env python3
"""Import ChatGPT conversation exports into the Second Brain.

Reads conversations.json from an unzipped ChatGPT export directory,
linearizes the mapping tree, filters and formats conversations as
markdown, and ingests via the existing pipeline.

Usage: python scripts/migrate/migrate_chatgpt.py <export_path> [--dry-run] [--limit N]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate.migration_utils import (
    format_chat_as_markdown,
    passes_chat_filter,
    run_migration,
    MigrationError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def linearize_mapping(mapping):
    """Walk the mapping tree root→leaf following first-child pointers.

    Returns ordered list of message dicts with 'role' and 'text' keys.
    """
    if not mapping:
        return []

    # Find root node (no parent or parent not in mapping)
    root_id = None
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if parent is None or parent not in mapping:
            root_id = node_id
            break

    if root_id is None:
        return []

    # Walk first-child path
    messages = []
    current_id = root_id
    while current_id and current_id in mapping:
        node = mapping[current_id]
        msg = node.get("message")
        if msg and msg.get("content") and msg.get("author"):
            role = msg["author"].get("role", "")
            parts = msg["content"].get("parts", [])
            text = "\n".join(str(p) for p in parts if isinstance(p, str))
            if text.strip():
                messages.append({"role": role, "text": text})

        children = node.get("children", [])
        current_id = children[0] if children else None

    return messages


def parse_chatgpt_export(export_path):
    """Yield (source_url, markdown) for each conversation passing filters."""
    conversations_file = os.path.join(export_path, "conversations.json")
    with open(conversations_file) as f:
        conversations = json.load(f)

    for conv in conversations:
        conv_id = conv.get("id", "")
        title = conv.get("title", "Untitled")
        create_time = conv.get("create_time")
        update_time = conv.get("update_time")

        raw_messages = linearize_mapping(conv.get("mapping", {}))

        messages = []
        for msg in raw_messages:
            role = msg["role"]
            if role == "user":
                messages.append(("human", msg["text"]))
            elif role == "assistant":
                messages.append(("bot", msg["text"]))
            # Skip system, tool roles

        if not passes_chat_filter(messages):
            continue

        date_str = (
            datetime.fromtimestamp(create_time, tz=timezone.utc).strftime("%Y-%m-%d")
            if create_time else "unknown"
        )
        source_url = f"chatgpt://{conv_id}"

        metadata = {}

        extra_fields = {}
        extra_fields["Message-Count"] = str(len(messages))
        if create_time:
            extra_fields["Original-Created-At"] = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
        if update_time:
            extra_fields["Original-Updated-At"] = datetime.fromtimestamp(update_time, tz=timezone.utc).isoformat()

        # Extract model slug if available from any message
        for node in conv.get("mapping", {}).values():
            msg = node.get("message") or {}
            model_slug = (msg.get("metadata") or {}).get("model_slug")
            if model_slug:
                extra_fields["Model"] = model_slug
                break

        markdown = format_chat_as_markdown(
            f"Chat: {title}", messages, "chatgpt_chat", source_url, date_str,
            extra_fields=extra_fields,
        )
        yield source_url, markdown


def main():
    parser = argparse.ArgumentParser(description="Import ChatGPT conversations into Second Brain")
    parser.add_argument("export_path", help="Path to unzipped ChatGPT export directory")
    parser.add_argument("--dry-run", action="store_true", help="Parse and filter without writing to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max conversations to process")
    args = parser.parse_args()

    try:
        run_migration(
            name="ChatGPT",
            export_path=args.export_path,
            parse_fn=parse_chatgpt_export,
            source_type="chatgpt_chat",
            expected_file="conversations.json",
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except MigrationError as e:
        logging.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
