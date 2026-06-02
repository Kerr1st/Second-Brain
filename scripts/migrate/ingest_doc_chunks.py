#!/usr/bin/env python3
"""Ingest pre-chunked document content from Quick Desktop into Second Brain.

Reads the ACTIVE QD profile's knowledge_v1.db (search_content + file_chunks) via
the qd_profile resolver and creates a memory per local document chunk
(source_type='quick_desktop_doc'). Low-signal folders (Downloads) are excluded.

Usage:
    .venv/bin/python scripts/migrate/ingest_doc_chunks.py [--dry-run] [--workers 20]

Idempotent — uses source_url 'qd-doc://{node_id}' for deduplication.
Scheduled incrementally via qd_sync.sh.
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

from src.db import create_memory, get_processed_source_urls
from src.embeddings import generate_embedding
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
    conn = sqlite3.connect(db_path)
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


def ingest_doc_chunks(db_path=None, dry_run=False, workers=20):
    """Ingest document chunks. Returns stats dict."""
    db_path = db_path or QD_DB_PATH

    if not os.path.exists(db_path):
        log.error(f"SQLite DB not found: {db_path}")
        return {"processed": 0, "skipped": 0, "failed": 0}

    chunks = query_chunks(db_path)
    log.info(f"Found {len(chunks)} document chunks")

    if dry_run:
        # In dry-run, just count non-empty chunks
        non_empty = sum(1 for c in chunks if c["text_content"] and c["text_content"].strip())
        log.info(f"DRY RUN: would process {non_empty} chunks")
        return {"processed": non_empty, "skipped": len(chunks) - non_empty, "failed": 0}

    already = get_processed_source_urls(SOURCE_TYPE)
    log.info(f"Already ingested: {len(already)} chunks")

    stats = {"processed": 0, "skipped": 0, "failed": 0}
    stats_lock = Lock()

    def process_chunk(chunk):
        url = source_url_for_chunk(chunk["node"])
        if url in already:
            return "skipped"

        text = chunk["text_content"]
        if not text or not text.strip():
            return "skipped"

        try:
            embedding = generate_embedding(text)
            create_memory(
                type="source",
                title=title_from_source(chunk["source"], chunk["chunk_index"]),
                content=text,
                embedding=embedding,
                tags=tags_for_chunk(chunk["extension"], chunk["folder_path"]),
                source_url=url,
                source_type=SOURCE_TYPE,
                metadata=metadata_for_chunk(
                    node_id=chunk["node"],
                    chunk_index=chunk["chunk_index"],
                    file_id=chunk["file_id"],
                    folder_path=chunk["folder_path"],
                    extension=chunk["extension"],
                    source=chunk["source"],
                ),
            )
            return "processed"
        except Exception as e:
            log.error(f"Failed node={chunk['node']}: {e}")
            return "failed"

    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_chunk, c): c for c in chunks}
        done_count = 0

        for future in as_completed(futures):
            result = future.result()
            with stats_lock:
                stats[result] += 1
                done_count += 1

            if done_count % 500 == 0:
                elapsed = time.time() - start
                rate = done_count / elapsed
                with stats_lock:
                    s = dict(stats)
                log.info(
                    f"Progress: {done_count}/{len(chunks)} | "
                    f"processed={s['processed']} skipped={s['skipped']} failed={s['failed']} | "
                    f"{rate:.1f}/s | ETA {(len(chunks)-done_count)/max(rate,0.01)/60:.0f}m"
                )

    elapsed = time.time() - start
    log.info(
        f"Done in {elapsed:.0f}s ({elapsed/60:.1f}m): "
        f"processed={stats['processed']} skipped={stats['skipped']} failed={stats['failed']}"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest QD document chunks into Second Brain")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()
    ingest_doc_chunks(dry_run=args.dry_run, workers=args.workers)


if __name__ == "__main__":
    main()
