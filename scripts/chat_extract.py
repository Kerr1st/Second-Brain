#!/usr/bin/env python3
"""Daily chat extraction — Phase 1 of hybrid chat ingestion.

Reads from all three chat sources, applies structural stripping and filtering,
writes cleaned markdown to staging/chats/. Runs via launchd at 2:30 AM.

Usage: python scripts/chat_extract.py [--backfill] [--dry-run]
  --backfill  Process all chats (initial run), not just new ones.
  --dry-run   Print stats without writing files.
"""

import os
import sys
import logging
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.parsers.ide_chat import parse_all as parse_ide_chats, find_chat_files
from src.parsers.cli_chat import parse_all as parse_cli_chats
from src.db import get_processed_source_urls

STAGING_DIR = os.path.join(os.path.dirname(__file__), "..", "staging", "chats")
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(LOG_DIR, f"chat_extract-{date_str}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


def get_already_staged():
    """Get set of filenames already in staging (to avoid re-extracting)."""
    if not os.path.isdir(STAGING_DIR):
        return set()
    return {f.replace(".md", "") for f in os.listdir(STAGING_DIR) if f.endswith(".md")}


def main():
    dry_run = "--dry-run" in sys.argv
    log = setup_logging()
    os.makedirs(STAGING_DIR, exist_ok=True)

    already_staged = set() if "--backfill" in sys.argv else get_already_staged()
    # Skip chats already ingested in the DB (either source_url scheme) so
    # re-extraction doesn't re-stage the whole history. batch_ingest_staged is
    # the downstream dedup backstop, so a DB hiccup here is non-fatal.
    try:
        ingested = set() if "--backfill" in sys.argv else (
            get_processed_source_urls("kiro_cli_chat") | get_processed_source_urls("kiro_ide_chat"))
    except Exception:
        ingested = set()

    log.info("=== Phase 1: Chat Extraction ===")
    log.info(f"Mode: {'backfill' if '--backfill' in sys.argv else 'incremental'}")
    log.info(f"Dry run: {dry_run}")

    ide_passed = 0
    cli_passed = 0
    errors = 0

    # Process IDE chats
    log.info("Processing IDE chats...")
    try:
        for filename, markdown, meta in parse_ide_chats(already_processed=already_staged):
            if f"chat://{filename}" in ingested or f"ide_{filename}.md" in ingested:
                continue
            ide_passed += 1
            if not dry_run:
                out_path = os.path.join(STAGING_DIR, f"ide_{filename}.md")
                with open(out_path, "w") as f:
                    f.write(markdown)
    except Exception as e:
        log.error(f"IDE parser error: {e}")
        errors += 1

    # Process CLI chats
    log.info("Processing CLI chats...")
    try:
        for conv_id, markdown, project in parse_cli_chats(already_processed=already_staged):
            short_id = conv_id[:12]
            if f"chat://{short_id}" in ingested or f"cli_{short_id}.md" in ingested:
                continue
            cli_passed += 1
            if not dry_run:
                short_id = conv_id[:12]
                out_path = os.path.join(STAGING_DIR, f"cli_{short_id}.md")
                with open(out_path, "w") as f:
                    f.write(markdown)
    except Exception as e:
        log.error(f"CLI parser error: {e}")
        errors += 1

    total = ide_passed + cli_passed
    log.info(f"=== Complete: IDE={ide_passed}, CLI={cli_passed}, total={total}, errors={errors} ===")


if __name__ == "__main__":
    main()
