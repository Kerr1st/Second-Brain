"""Capture public/article documents from the local Crawlee knowledge store."""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from pathlib import Path
from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import run_channel
from src.parsers import crawlee


def capture(*, dry_run=False):
    if not Path(crawlee.INDEX_PATH).exists() and not Path(crawlee.OUTPUT_DIR).is_dir() and not Path(crawlee.KNOWLEDGE_DIR, "sources").is_dir():
        return {"failed": 1, "reason": "missing_source_store"}
    groups = defaultdict(CaptureBatch)
    for url, source, content in crawlee.parse_all(already_processed=set()):
        if not crawlee.is_http_url(url):
            groups[source].failed += 1
        else:
            groups[source].append((url, content))
    summary = {source: run_channel(source, items, dry_run=dry_run) for source,items in groups.items()}
    summary["source_files"] = {"failed": crawlee.last_parse_stats.get("unresolved", 0),
                               "unindexed_output": crawlee.last_parse_stats.get("unindexed_output", 0),
                               "unindexed_sources": crawlee.last_parse_stats.get("unindexed_sources", 0)}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = capture(dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
