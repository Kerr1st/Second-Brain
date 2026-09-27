#!/usr/bin/env python3
"""Import Quick Desktop memories and KG decisions into the Second Brain.

Reads QD's knowledge_v1.db (SQLite) and imports:
  - Long-term memories (facts + procedures) as typed Second Brain memories
  - KG Decision entities as 'decision' type memories

Refreshes the current eligible snapshot with stable source identities.

Usage:
  python -m src.capture.quick_desktop [--dry-run] [--min-confidence 0.5]
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

from src.qd_profile import qd_path, warn_if_stale, QD_ROOT

QD_DB_PATH = qd_path("knowledge_storage", "knowledge_v1.db")
# QD category -> Second Brain (type, source_type)
CATEGORY_MAP = {
    "people":         ("source",   "quick_desktop_people"),
    "terminology":    ("research", "quick_desktop_terminology"),
    "source":         ("source",   "quick_desktop_source"),
    "tool-strategy":  ("insight",  "quick_desktop_tool_strategy"),
    "preference":     ("insight",  "quick_desktop_preference"),
    "anti-pattern":   ("insight",  "quick_desktop_anti_pattern"),
    "profile":        ("source",   "quick_desktop_profile"),
}
PROCEDURE_MAP = ("insight", "quick_desktop_procedure")
DEFAULT_MAP = ("source", "quick_desktop")



def open_qd_db():
    warn_if_stale(QD_DB_PATH)
    if not os.path.exists(QD_DB_PATH):
        raise FileNotFoundError(f"Quick Desktop DB not found: {QD_DB_PATH}")
    conn = sqlite3.connect(f"file:{QD_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_memories(qd_conn, min_confidence=0.5, since_id=0):
    """Fetch QD memories above confidence threshold, optionally after a given ID."""
    return qd_conn.execute("""
        SELECT id, memory_type, category, name, trigger_text,
               confidence, effective_confidence, retrieval_count, source
        FROM memories
        WHERE effective_confidence >= ? AND id > ?
        ORDER BY id
    """, (min_confidence, since_id)).fetchall()


def fetch_kg_decisions(qd_conn, since_id=0):
    """Fetch Decision entities from the KG."""
    return qd_conn.execute("""
        SELECT e.id, e.category, e.name, e.properties,
               sc.text_content
        FROM entities e
        LEFT JOIN nodes n ON n.id = e.node
        LEFT JOIN search_content sc ON sc.node = n.id
        WHERE e.category = 'Decision' AND e.id > ?
        ORDER BY e.id
    """, (since_id,)).fetchall()


def map_memory(row):
    """Map a QD memory row to Second Brain create_memory kwargs."""
    category = row["category"]
    mem_type = row["memory_type"]

    if mem_type == "procedure":
        sb_type, sb_source_type = PROCEDURE_MAP
    elif category in CATEGORY_MAP:
        sb_type, sb_source_type = CATEGORY_MAP[category]
    else:
        sb_type, sb_source_type = DEFAULT_MAP

    text = row["trigger_text"]
    # Build a title from first line or first 80 chars
    first_line = text.split("\n")[0].strip()
    title = first_line[:80] if len(first_line) > 80 else first_line
    if row["name"]:
        title = row["name"]

    source_url = f"qd://memory/{row['id']}"

    return {
        "type": sb_type,
        "title": title,
        "content": text,
        "source_type": sb_source_type,
        "source_url": source_url,
        "confidence": row["effective_confidence"],
        "tags": [f"qd:{category or 'uncategorized'}", f"qd_type:{mem_type}"],
        "metadata": {
            "qd_id": row["id"],
            "qd_category": category,
            "qd_memory_type": mem_type,
            "qd_retrieval_count": row["retrieval_count"],
            "qd_confidence": row["confidence"],
            "qd_effective_confidence": row["effective_confidence"],
        },
        "mem_class": "semantic" if mem_type == "fact" else "procedural",
    }


def map_decision(row):
    """Map a QD Decision entity to Second Brain create_memory kwargs."""
    name = row["name"]
    text = row["text_content"] or name
    source_url = f"qd://entity/{row['id']}"

    return {
        "type": "decision",
        "title": name,
        "content": text,
        "source_type": "quick_desktop_decision",
        "source_url": source_url,
        "confidence": 0.8,
        "tags": ["qd:decision", "qd_type:entity"],
        "metadata": {"qd_entity_id": row["id"], "qd_category": "Decision"},
        "mem_class": "semantic",
    }



def capture(*, dry_run=False, min_confidence=0.5, decisions=True):
    """Refresh the bounded upstream snapshot; failed rows remain retryable.

    Numeric upstream IDs retain existing provenance URLs. No advancing cursor can
    skip a failed row or conceal an edit to an earlier fact. Graph tables are
    historical backup data; this channel no longer writes them.
    """
    from src.capture.framework import run_records
    conn = open_qd_db()
    try:
        records = [map_memory(row) for row in fetch_memories(conn, min_confidence)]
        if decisions:
            records.extend(map_decision(row) for row in fetch_kg_decisions(conn))
    finally:
        conn.close()
    return run_records(records, dry_run=dry_run, refresh=True)


def main():
    from src.capture.common import exit_code
    parser = argparse.ArgumentParser(description="Capture Quick Desktop facts and decisions")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--no-decisions", action="store_true")
    args = parser.parse_args()
    result = capture(dry_run=args.dry_run, min_confidence=args.min_confidence, decisions=not args.no_decisions)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
