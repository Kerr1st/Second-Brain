#!/usr/bin/env python3
"""Parallel batch ingest of staged chat files.

Uses a thread pool to parallelize Bedrock embedding calls across files.
Each file is still processed atomically (chunk→embed→store) but multiple
files run concurrently.

Usage: .venv/bin/python scripts/batch_ingest_parallel.py [--workers 10] [--limit N]
"""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("DB_POOL_MAX", "20")

from src.ingest import ingest_content
from src.db import get_processed_source_urls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

STAGING_DIR = Path(__file__).resolve().parent.parent / "staging" / "chats"
INGESTED_DIR = Path(__file__).resolve().parent.parent / "staging" / "ingested"

stats_lock = Lock()
stats = {"processed": 0, "skipped": 0, "failed": 0}


def process_file(f, already):
    source_type = "kiro_cli_chat" if f.name.startswith("cli_") else "kiro_ide_chat"
    source_id = f.stem.replace("cli_", "").replace("ide_", "")
    source_url = f"chat://{source_id}"

    if source_url in already:
        f.rename(INGESTED_DIR / f.name)
        with stats_lock:
            stats["skipped"] += 1
        return "skipped"

    try:
        content = f.read_text(encoding="utf-8")
        result = ingest_content(content, source_type, source_url)
        if result:
            f.rename(INGESTED_DIR / f.name)
            with stats_lock:
                stats["processed"] += 1
            return "processed"
        else:
            f.rename(INGESTED_DIR / f.name)
            with stats_lock:
                stats["skipped"] += 1
            return "skipped"
    except Exception as e:
        with stats_lock:
            stats["failed"] += 1
        log.error(f"Failed {f.name}: {e}")
        return "failed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    INGESTED_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(STAGING_DIR.glob("*.md"))
    if not files:
        log.info("No staged files to process")
        return

    if args.limit:
        files = files[:args.limit]

    already_cli = get_processed_source_urls("kiro_cli_chat")
    already_ide = get_processed_source_urls("kiro_ide_chat")
    already = already_cli | already_ide

    log.info(f"Processing {len(files)} files with {args.workers} workers")
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_file, f, already): f for f in files}

        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            if done_count % 100 == 0:
                elapsed = time.time() - start
                rate = done_count / elapsed
                remaining = (len(files) - done_count) / max(rate, 0.01)
                with stats_lock:
                    s = dict(stats)
                log.info(
                    f"Progress: {done_count}/{len(files)} | "
                    f"processed={s['processed']} skipped={s['skipped']} failed={s['failed']} | "
                    f"{rate:.1f} files/s | ETA {remaining/60:.0f}m"
                )

    elapsed = time.time() - start
    log.info(
        f"Done in {elapsed:.0f}s ({elapsed/60:.1f}m): "
        f"processed={stats['processed']} skipped={stats['skipped']} failed={stats['failed']}"
    )


if __name__ == "__main__":
    main()
