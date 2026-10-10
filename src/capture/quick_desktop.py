#!/usr/bin/env python3
"""Import Quick Desktop memories and KG decisions into the Second Brain.

Reads QD's knowledge_v1.db (SQLite) and imports:
  - Long-term memories (facts + procedures) as typed Second Brain memories
  - KG Decision entities as 'decision' type memories

Refreshes the current eligible snapshot with stable source identities.

Usage:
  python -m src.capture.quick_desktop [--dry-run] [--min-confidence 0.5] [--allow-mass-retire]
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

from src.qd_profile import resolve_profile_base, warn_if_stale, QD_ROOT

# QD_PROFILE_FALLBACK is set when profiles.json could not name the active profile and
# the legacy top-level store was chosen instead; that store is not trusted (F-090).
_QD_BASE, QD_PROFILE_FALLBACK = resolve_profile_base()
QD_DB_PATH = os.path.join(_QD_BASE, "knowledge_storage", "knowledge_v1.db")
# QD's own selection threshold. The corpus mirrors the selection (F-090) only at
# this threshold; a parent retired below it needs a margin to move either way.
DEFAULT_MIN_CONFIDENCE = 0.5
RETIRE_HYSTERESIS = 0.05      # retire below_threshold only under DEFAULT - this
REACTIVATE_HYSTERESIS = 0.05  # reactivate a below_threshold row only at DEFAULT + this
# Operator override for the mass-retire guard; set in no plist (one-off, after review).
ALLOW_MASS_RETIRE_ENV = "SECOND_BRAIN_QD_ALLOW_MASS_RETIRE"
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
MEMORY_URL_PREFIX = "qd://memory/"
DECISION_URL_PREFIX = "qd://entity/"
DEFAULT_MAP = ("source", "quick_desktop")



def open_qd_db():
    warn_if_stale(QD_DB_PATH)
    if not os.path.exists(QD_DB_PATH):
        raise FileNotFoundError(f"Quick Desktop DB not found: {QD_DB_PATH}")
    conn = sqlite3.connect(f"file:{QD_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_memories(qd_conn, min_confidence=DEFAULT_MIN_CONFIDENCE, since_id=0):
    """Fetch QD memories above confidence threshold, optionally after a given ID."""
    return qd_conn.execute("""
        SELECT id, memory_type, category, name, trigger_text,
               confidence, effective_confidence, retrieval_count, source
        FROM memories
        WHERE effective_confidence >= ? AND id > ?
        ORDER BY id
    """, (min_confidence, since_id)).fetchall()


def fetch_upstream_confidence(qd_conn):
    """Every upstream memory URL, selected or not, with its effective confidence."""
    return {f"{MEMORY_URL_PREFIX}{row[0]}": row[1]
            for row in qd_conn.execute("SELECT id, effective_confidence FROM memories")}


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

    source_url = f"{MEMORY_URL_PREFIX}{row['id']}"

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
    source_url = f"{DECISION_URL_PREFIX}{row['id']}"

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



def capture(*, dry_run=False, min_confidence=DEFAULT_MIN_CONFIDENCE, decisions=True, allow_mass_retire=None):
    """Refresh the bounded upstream snapshot; failed rows remain retryable.

    Numeric upstream IDs retain existing provenance URLs. No advancing cursor can
    skip a failed row or conceal an edit to an earlier fact. Graph tables are
    historical backup data; this channel no longer writes them.

    The corpus mirrors the current selection (F-090): a ``qd://`` parent the
    selection no longer contains (``deleted_upstream``, ``below_threshold`` or
    ``relabelled``) is retired as superseded history before the refresh, and one
    that returns is reactivated. Guards, each logged:

    - a store resolved through the legacy profile fallback (profiles.json
      unreadable or naming no entry) is not trusted to mirror: nothing is retired
      or reactivated, the fallback is reported and logged; the refresh still runs;
    - a non-default ``min_confidence`` never retires;
    - a namespace that would lose more than ``max(25, 10%)`` of its active parents
      retires none unless ``allow_mass_retire`` (CLI ``--allow-mass-retire`` or
      ``SECOND_BRAIN_QD_ALLOW_MASS_RETIRE=1``); the refresh still runs.

    A dry run reports per-reason counts and the guard decision under
    ``"selection"`` without writing.
    """
    from src.capture.framework import run_records
    from src.db import reconcile_source_selection
    if allow_mass_retire is None:
        allow_mass_retire = os.environ.get(ALLOW_MASS_RETIRE_ENV) == "1"
    conn = open_qd_db()
    try:
        records = [map_memory(row) for row in fetch_memories(conn, min_confidence)]
        upstream = fetch_upstream_confidence(conn)
        if decisions:
            records.extend(map_decision(row) for row in fetch_kg_decisions(conn))
    finally:
        conn.close()
    if QD_PROFILE_FALLBACK:
        # A legacy store's numeric IDs may name other facts than the active profile's.
        logger.warning("Quick Desktop profile UNRESOLVED (%s): reading the legacy fallback store %s; "
                       "selection reconcile disabled (no retirement, no reactivation)", QD_PROFILE_FALLBACK, QD_DB_PATH)
        skip = "legacy_profile_fallback"
    elif min_confidence != DEFAULT_MIN_CONFIDENCE:
        skip = "non_default_threshold"
    else:
        skip = None
    # Decisions are authoritative for qd://entity/ only when they were read.
    prefixes = (MEMORY_URL_PREFIX, DECISION_URL_PREFIX) if decisions else (MEMORY_URL_PREFIX,)
    selection = reconcile_source_selection(
        prefixes, {(r["source_type"], r["source_url"]) for r in records}, upstream_confidence=upstream,
        retire_below=DEFAULT_MIN_CONFIDENCE - RETIRE_HYSTERESIS,
        reactivate_at=DEFAULT_MIN_CONFIDENCE + REACTIVATE_HYSTERESIS,
        skip_retirement=skip, reactivate=not QD_PROFILE_FALLBACK,
        allow_mass_retire=allow_mass_retire, dry_run=dry_run)
    if QD_PROFILE_FALLBACK:
        selection["profile_fallback"] = QD_PROFILE_FALLBACK
    # Selected identities that stay superseded (held, or replaced by another writer)
    # are not refreshed: that would write active chunks under a superseded parent.
    records = [r for r in records if (r["source_type"], r["source_url"]) not in selection.withheld]
    summary = run_records(records, dry_run=dry_run, refresh=True)
    summary["selection"] = selection
    return summary


def main():
    from src.capture.common import exit_code
    parser = argparse.ArgumentParser(description="Capture Quick Desktop facts and decisions")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                        help="selection threshold; any non-default value never retires (F-090)")
    parser.add_argument("--no-decisions", action="store_true")
    parser.add_argument("--allow-mass-retire", action="store_true", default=None,
                        help=f"override the mass-retire guard for this run (or {ALLOW_MASS_RETIRE_ENV}=1)")
    args = parser.parse_args()
    result = capture(dry_run=args.dry_run, min_confidence=args.min_confidence, decisions=not args.no_decisions,
                     allow_mass_retire=args.allow_mass_retire)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
