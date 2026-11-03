#!/usr/bin/env python3
"""Ingest pre-chunked document content from Quick Desktop into Second Brain.

Reads the ACTIVE QD profile's knowledge_v1.db (search_content + file_chunks) via
the qd_profile resolver and creates a memory per local document chunk
(source_type='quick_desktop_doc'). Low-signal folders (Downloads) are excluded.

Usage:
    .venv/bin/python -m src.capture.qd_docs [--dry-run] [--workers 20]

Idempotent — uses source_url 'qd-doc://{node_id}' for deduplication.
Explicit replay adapter for the retired QD document-export channel.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DB_POOL_MAX", "20")

from src.qd_profile import qd_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

QD_DB_PATH = qd_path("knowledge_storage", "knowledge_v1.db")
SOURCE_TYPE = "quick_desktop_doc"

# Low-signal folders excluded from capture (SQL LIKE patterns). See investigation 2026-06-02.
EXCLUDE_FOLDER_PATTERNS = ("%Downloads%",)


def source_url_for_chunk(node_id):
    return f"qd-doc://{node_id}"


def title_from_source(source_path, chunk_index=None):
    filename = os.path.basename(source_path) if source_path else "Unknown Document"
    if chunk_index is not None and chunk_index > 0:
        return f"{filename} (chunk {chunk_index})"
    return filename


def tags_for_chunk(extension, folder_path):
    tags = ["qd-doc"]
    if extension:
        tags.append(extension.lstrip("."))
    if folder_path:
        # Extract meaningful folder name
        parts = folder_path.rstrip("/").split("/")
        if parts:
            tags.append(parts[-1].lower().replace(" ", "-")[:40])
    return tags


def metadata_for_chunk(node_id, chunk_index, file_id, folder_path, extension, source):
    return {
        "node_id": node_id,
        "chunk_index": chunk_index,
        "file_id": file_id,
        "folder_path": folder_path,
        "extension": extension,
        "source_file": source,
    }


def query_chunks(db_path):
    """Query local document chunks from the SQLite DB, excluding low-signal folders."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where = "sc.source_type = 'local'"
    params = []
    for pat in EXCLUDE_FOLDER_PATTERNS:
        where += " AND COALESCE(sc.folder_path, '') NOT LIKE ?"
        params.append(pat)
    cur = conn.execute(f"""
        SELECT sc.id, sc.node, sc.folder_path, sc.extension, sc.source,
               sc.text_content, fc.chunk_index, fc.file_id
        FROM search_content sc
        JOIN file_chunks fc ON fc.node = sc.node
        WHERE {where}
        ORDER BY fc.file_id, fc.chunk_index
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def ingest_doc_chunks(db_path=None, dry_run=False, workers=1):
    """Capture source chunks through the same provenance-preserving service."""
    from src.capture.framework import run_records
    db_path = db_path or QD_DB_PATH
    if not os.path.isfile(db_path):
        return {"processed": 0, "skipped": 0, "failed": 1}
    records, empty = [], 0
    for chunk in query_chunks(db_path):
        if not chunk["text_content"] or not chunk["text_content"].strip():
            empty += 1
            continue
        records.append({"type": "source", "title": title_from_source(chunk["source"], chunk["chunk_index"]),
            "content": chunk["text_content"], "source_type": SOURCE_TYPE,
            "source_url": source_url_for_chunk(chunk["node"]),
            "tags": tags_for_chunk(chunk["extension"], chunk["folder_path"]),
            "metadata": metadata_for_chunk(chunk["node"], chunk["chunk_index"], chunk["file_id"],
                chunk["folder_path"], chunk["extension"], chunk["source"])})
    stats = run_records(records, dry_run=dry_run, refresh=True).get(SOURCE_TYPE,
        {"processed": 0, "skipped": 0, "failed": 0})
    stats["skipped"] += empty
    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest QD document chunks into Second Brain")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    from src.capture.common import exit_code
    return exit_code(ingest_doc_chunks(dry_run=args.dry_run, workers=args.workers))


if __name__ == "__main__":
    sys.exit(main())
