#!/usr/bin/env python3
"""Batch ingest all staged chat files directly via the ingest pipeline.

Much faster than the Kiro CLI one-at-a-time approach. Processes files
sequentially through ingest_content() which handles chunking, embedding,
and relationship discovery.

Usage: .venv/bin/python scripts/batch_ingest_staged.py [--dry-run] [--limit N]
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import ingest_content
from src.db import get_processed_source_urls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

STAGING_DIR = Path(__file__).resolve().parent.parent / "staging" / "chats"
INGESTED_DIR = Path(__file__).resolve().parent.parent / "staging" / "ingested"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max files to process (0=all)")
    args = parser.parse_args()

    INGESTED_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(STAGING_DIR.glob("*.md"))
    if not files:
        log.info("No staged files to process")
        return

    if args.limit:
        files = files[:args.limit]

    # Get already-processed URLs for dedup
    already_cli = get_processed_source_urls("kiro_cli_chat")
    already_ide = get_processed_source_urls("kiro_ide_chat")
    already = already_cli | already_ide

    log.info(f"Processing {len(files)} staged files (dry_run={args.dry_run})")

    processed = 0
    skipped = 0
    failed = 0
    start = time.time()

    for i, f in enumerate(files):
        source_type = "kiro_cli_chat" if f.name.startswith("cli_") else "kiro_ide_chat"
        source_id = f.stem.replace("cli_", "").replace("ide_", "")
        source_url = f"chat://{source_id}"

        if source_url in already:
            skipped += 1
            if not args.dry_run:
                f.rename(INGESTED_DIR / f.name)
            continue

        if args.dry_run:
            processed += 1
            continue

        try:
            content = f.read_text(encoding="utf-8")
            result = ingest_content(content, source_type, source_url)
            if result:
                processed += 1
                f.rename(INGESTED_DIR / f.name)
            else:
                skipped += 1
                f.rename(INGESTED_DIR / f.name)
        except Exception as e:
            failed += 1
            log.error(f"Failed {f.name}: {e}")

        # Progress every 100 files
        if (i + 1) % 100 == 0:
            elapsed = time.time() - start
            rate = (processed + skipped + failed) / elapsed
            remaining = (len(files) - i - 1) / max(rate, 0.01)
            log.info(
                f"Progress: {i+1}/{len(files)} | "
                f"processed={processed} skipped={skipped} failed={failed} | "
                f"{rate:.1f} files/s | ETA {remaining/60:.0f}m"
            )

    elapsed = time.time() - start
    log.info(
        f"Done in {elapsed:.0f}s: processed={processed} skipped={skipped} failed={failed}"
    )


if __name__ == "__main__":
    main()
