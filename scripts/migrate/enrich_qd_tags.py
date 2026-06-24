#!/usr/bin/env python3
"""Enrich existing QD memories with tags and domains from Quick Desktop.

Reads memory_tags and memory_domains from QD's knowledge_v1.db and merges
them into the corresponding Second Brain memories' tags[] array and metadata.

Usage:
  .venv/bin/python scripts/migrate/enrich_qd_tags.py [--dry-run]

Designed to be idempotent — safe to run repeatedly. Already-present tags
are not duplicated.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from src.qd_profile import qd_path

QD_DB_PATH = qd_path("knowledge_storage", "knowledge_v1.db")


def fetch_tags_for_memories(conn):
    """Fetch all tags grouped by memory_id. Returns {memory_id: [tag, ...]}."""
    rows = conn.execute("SELECT memory_id, tag FROM memory_tags ORDER BY memory_id").fetchall()
    result = {}
    for row in rows:
        mid = row[0] if isinstance(row, (tuple, list)) else row["memory_id"]
        tag = row[1] if isinstance(row, (tuple, list)) else row["tag"]
        tag = tag.strip()
        if tag:
            result.setdefault(mid, []).append(tag)
    return result


def fetch_domains_for_memories(conn):
    """Fetch all domains grouped by memory_id. Returns {memory_id: [domain, ...]}."""
    rows = conn.execute("SELECT memory_id, domain FROM memory_domains ORDER BY memory_id").fetchall()
    result = {}
    for row in rows:
        mid = row[0] if isinstance(row, (tuple, list)) else row["memory_id"]
        domain = row[1] if isinstance(row, (tuple, list)) else row["domain"]
        domain = domain.strip()
        if domain:
            result.setdefault(mid, []).append(domain)
    return result


def merge_tags(existing, new_tags):
    """Merge new tags into existing tags list, preserving existing and deduplicating.

    Args:
        existing: Current tags list (preserved in order at front).
        new_tags: New tags to add (appended after existing, deduplicated).

    Returns:
        Merged list with no duplicates. Existing tags come first.
    """
    seen = set()
    result = []
    for tag in existing:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    for tag in new_tags:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def get_qd_memory_id_mapping():
    """Get mapping from QD memory ID (int) to Second Brain memory UUID.

    Looks up memories with source_url like 'qd://memory/{id}'.
    Returns {qd_id: sb_uuid_str}.
    """
    mapping = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, source_url FROM memories
                WHERE source_url LIKE 'qd://memory/%'
            """)
            for row in cur.fetchall():
                sb_id = str(row[0])
                source_url = row[1]
                # Extract QD ID from 'qd://memory/123'
                try:
                    qd_id = int(source_url.split("/")[-1])
                    mapping[qd_id] = sb_id
                except (ValueError, IndexError):
                    continue
    return mapping


def enrich_memories(dry_run=False):
    """Main enrichment: add QD tags and domains to existing Second Brain memories.

    Returns stats dict with counts.
    """
    if not os.path.exists(QD_DB_PATH):
        log.error(f"QD database not found: {QD_DB_PATH}")
        return {"tags_enriched": 0, "domains_enriched": 0, "skipped": 0}

    qd_conn = sqlite3.connect(f"file:{QD_DB_PATH}?mode=ro", uri=True)
    qd_conn.row_factory = sqlite3.Row

    tags_map = fetch_tags_for_memories(qd_conn)
    domains_map = fetch_domains_for_memories(qd_conn)
    qd_conn.close()

    log.info(f"QD data: {sum(len(v) for v in tags_map.values())} tags across {len(tags_map)} memories, "
             f"{sum(len(v) for v in domains_map.values())} domains across {len(domains_map)} memories")

    # Get QD→SB ID mapping
    id_mapping = get_qd_memory_id_mapping()
    log.info(f"Found {len(id_mapping)} QD memories in Second Brain")

    stats = {"tags_enriched": 0, "domains_enriched": 0, "skipped": 0}

    with get_connection() as conn:
        with conn.cursor() as cur:
            for qd_id, sb_id in id_mapping.items():
                new_tags = tags_map.get(qd_id, [])
                new_domains = domains_map.get(qd_id, [])

                if not new_tags and not new_domains:
                    stats["skipped"] += 1
                    continue

                # Fetch current state
                cur.execute("SELECT tags, metadata FROM memories WHERE id = %s", (sb_id,))
                row = cur.fetchone()
                if not row:
                    stats["skipped"] += 1
                    continue

                existing_tags = row[0] or []
                existing_metadata = row[1] if row[1] else {}
                if isinstance(existing_metadata, str):
                    existing_metadata = json.loads(existing_metadata)

                # Merge tags
                updated_tags = merge_tags(existing_tags, new_tags)
                tags_changed = set(updated_tags) != set(existing_tags)

                # Merge domains into metadata
                existing_domains = existing_metadata.get("qd_domains", [])
                merged_domains = list(set(existing_domains) | set(new_domains))
                domains_changed = set(merged_domains) != set(existing_domains)

                if not tags_changed and not domains_changed:
                    stats["skipped"] += 1
                    continue

                if dry_run:
                    if tags_changed:
                        stats["tags_enriched"] += 1
                    if domains_changed:
                        stats["domains_enriched"] += 1
                    continue

                # Apply updates
                if tags_changed:
                    cur.execute("UPDATE memories SET tags = %s WHERE id = %s",
                                (updated_tags, sb_id))
                    stats["tags_enriched"] += 1

                if domains_changed:
                    existing_metadata["qd_domains"] = merged_domains
                    cur.execute("UPDATE memories SET metadata = %s WHERE id = %s",
                                (json.dumps(existing_metadata), sb_id))
                    stats["domains_enriched"] += 1

        if not dry_run:
            conn.commit()

    log.info(f"Enrichment complete: tags_enriched={stats['tags_enriched']}, "
             f"domains_enriched={stats['domains_enriched']}, skipped={stats['skipped']}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Enrich QD memories with tags and domains")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    stats = enrich_memories(dry_run=args.dry_run)
    if args.dry_run:
        log.info("DRY RUN — no changes written")


if __name__ == "__main__":
    main()
