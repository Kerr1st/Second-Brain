"""Daily Crawlee ingestion — reads new content from Crawlee's knowledge store.

Reads knowledge/index.md, compares against PostgreSQL, ingests new entries
through the shared pipeline (chunk → embed → store).

Usage: python scripts/crawlee_ingest.py
"""

import sys
import os
import logging
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.parsers.crawlee import parse_all
from src.db import get_processed_source_urls, is_reachable
from src.ingest import ingest_content

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(LOG_DIR, f"crawlee_ingest-{date_str}.log")

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


def main():
    log = setup_logging()
    log.info("=== Crawlee Ingestion ===")

    if not is_reachable():
        log.error("ABORT: PostgreSQL is not reachable")
        sys.exit(1)

    already = get_processed_source_urls()
    processed = 0
    skipped = 0
    failed = 0

    for source_url, source_type, content in parse_all(already_processed=already):
        try:
            result = ingest_content(content, source_type, source_url)
            if result:
                log.info(f"OK: {source_url[:80]}")
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            log.error(f"FAIL: {source_url[:80]} — {e}")
            failed += 1

    log.info(f"=== Complete: processed={processed}, skipped={skipped}, failed={failed} ===")


if __name__ == "__main__":
    main()
