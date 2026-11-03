"""Capture public/article documents from the local Crawlee knowledge store.

Identity is the exact ``Source:`` header / index URL (unchanged since R1).
Already-captured URLs of *both* channels this store feeds (``article`` and
``youtube``) are pre-filtered before any file is read, as the R1 ingest did,
so a nightly run reads only what is new.

Never-indexed source documents (cookie-captured internal pages without an
``index.md`` row) are a new input for this channel (R4). Their first ingest is
bounded per run by ``--max-unindexed`` / ``SECOND_BRAIN_CRAWLEE_MAX_UNINDEXED``
(default ``DEFAULT_MAX_UNINDEXED``) and the held-back count is reported as
``source_files.unindexed_sources_deferred``; ``--dry-run`` reports what a run
would do without reading the database for writes or calling the embedder.
"""
from __future__ import annotations
import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import run_channel
from src.parsers import crawlee

CHANNELS = ("article", "youtube")
DEFAULT_MAX_UNINDEXED = int(os.environ.get("SECOND_BRAIN_CRAWLEE_MAX_UNINDEXED", "25"))


def capture(*, dry_run=False, max_unindexed=None):
    if not Path(crawlee.INDEX_PATH).exists() and not Path(crawlee.OUTPUT_DIR).is_dir() and not Path(crawlee.KNOWLEDGE_DIR, "sources").is_dir():
        return {"failed": 1, "reason": "missing_source_store"}
    max_unindexed = DEFAULT_MAX_UNINDEXED if max_unindexed is None else max_unindexed
    from src.db import get_processed_source_urls
    already = set()
    for channel in CHANNELS:
        already |= set(get_processed_source_urls(channel))
    groups = defaultdict(CaptureBatch)
    for url, source, content in crawlee.parse_all(already_processed=already, max_unindexed_sources=max_unindexed):
        if not crawlee.is_http_url(url):
            groups[source].failed += 1
        else:
            groups[source].append((url, content))
    summary = {source: run_channel(source, items, dry_run=dry_run) for source, items in groups.items()}
    stats = crawlee.last_parse_stats
    # Index rows without a source file ("unresolved") are a long-standing store
    # condition (66 today), reported for the operator but not a run failure:
    # failing every night on them would only train people to ignore the job.
    summary["source_files"] = {"failed": 0, "unresolved": stats.get("unresolved", 0),
                               "unindexed_output": stats.get("unindexed_output", 0),
                               "unindexed_sources": stats.get("unindexed_sources", 0),
                               "unindexed_sources_new": stats.get("unindexed_sources_new", 0),
                               "unindexed_sources_deferred": stats.get("unindexed_sources_deferred", 0),
                               "max_unindexed_per_run": max_unindexed}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-unindexed", type=int, default=DEFAULT_MAX_UNINDEXED,
                        help=f"never-indexed source documents ingested per run (default {DEFAULT_MAX_UNINDEXED})")
    args = parser.parse_args()
    result = capture(dry_run=args.dry_run, max_unindexed=args.max_unindexed)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
