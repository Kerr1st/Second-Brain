#!/usr/bin/env python3
"""Import Quick Desktop memories and KG decisions into the Second Brain.

Reads QD's knowledge_v1.db (SQLite) and imports:
  - Long-term memories (facts + procedures) as typed Second Brain memories
  - KG Decision entities as 'decision' type memories

Supports both one-time backfill and incremental sync via --since timestamp.

Usage:
  python scripts/migrate/migrate_quick_desktop.py [--dry-run] [--since TIMESTAMP] [--min-confidence 0.5]
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import create_memory as _create_memory, get_processed_source_urls, is_reachable

def create_memory(**kwargs):
    """Wrapper that drops unsupported columns for older DB schemas."""
    kwargs.pop("encoding_context", None)
    return _create_memory(**kwargs)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

from src.qd_profile import qd_path, warn_if_stale, QD_ROOT

QD_DB_PATH = qd_path("knowledge_storage", "knowledge_v1.db")
SYNC_STATE_PATH = os.path.join(QD_ROOT, ".second_brain_sync_state.json")

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


def load_sync_state():
    if os.path.exists(SYNC_STATE_PATH):
        with open(SYNC_STATE_PATH) as f:
            return json.load(f)
    return {"last_memory_id": 0, "last_entity_id": 0, "last_sync": None}


def save_sync_state(state):
    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    with open(SYNC_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


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


def import_kg_graph(qd_conn, dry_run=False, since_entity_id=0):
    """Import QD's knowledge graph (entities + edges) into Second Brain's KG tables.
    
    Incremental: only imports entities with id > since_entity_id and their edges.
    """
    from src.db import get_connection

    entities = qd_conn.execute("""
        SELECT e.id, e.category, e.name, e.summary, e.source_type,
               sc.text_content
        FROM entities e
        LEFT JOIN nodes n ON n.id = e.node
        LEFT JOIN search_content sc ON sc.node = n.id
        WHERE e.id > ?
    """, (since_entity_id,)).fetchall()

    if not entities:
        logger.info("KG sync: 0 new entities since id=%d", since_entity_id)
        return {"entities": 0, "edges": 0, "bridge": 0, "edge_failed": 0, "max_entity_id": since_entity_id}

    # Get edges involving any new entity (chunked: large backfills exceed SQLite's variable limit)
    new_ids = [e["id"] for e in entities]
    edges = []
    seen = set()
    CHUNK = 400
    for i in range(0, len(new_ids), CHUNK):
        chunk = new_ids[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = qd_conn.execute(f"""
            SELECT e1.name as from_name, e1.category as from_cat,
                   e2.name as to_name, e2.category as to_cat,
                   ed.relation, ed.weight, ed.source_type
            FROM edges ed
            JOIN nodes n1 ON n1.id = ed.from_node
            JOIN entities e1 ON e1.node = n1.id
            JOIN nodes n2 ON n2.id = ed.to_node
            JOIN entities e2 ON e2.node = n2.id
            WHERE e1.id IN ({ph}) OR e2.id IN ({ph})
        """, chunk + chunk).fetchall()
        for r in rows:
            key = (r["from_cat"], r["from_name"], r["to_cat"], r["to_name"], r["relation"])
            if key not in seen:
                seen.add(key)
                edges.append(r)

    max_ent_id = max(e["id"] for e in entities)

    if dry_run:
        logger.info("[DRY RUN] Would import %d entities and %d edges into KG tables", len(entities), len(edges))
        return {"entities": len(entities), "edges": len(edges), "bridge": 0, "edge_failed": 0, "max_entity_id": max_ent_id}

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Upsert entities
            ent_count = 0
            for e in entities:
                cur.execute("""
                    INSERT INTO entities (category, name, summary, source_type, properties)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (category, name) DO UPDATE SET
                        summary = COALESCE(EXCLUDED.summary, entities.summary),
                        updated_at = now()
                """, (e["category"], e["name"], e["text_content"], e["source_type"] or "quick_desktop",
                      json.dumps({"qd_entity_id": e["id"]})))
                ent_count += 1

            # Insert edges (lookup entity UUIDs by category+name)
            edge_count = 0
            edge_failed = 0
            for ed in edges:
                cur.execute("""
                    INSERT INTO entity_edges (from_entity, to_entity, relation, weight, source_type, properties)
                    SELECT e1.id, e2.id, %s, %s, %s, '{}'
                    FROM entities e1, entities e2
                    WHERE e1.category = %s AND e1.name = %s
                      AND e2.category = %s AND e2.name = %s
                    ON CONFLICT (from_entity, to_entity, relation) DO NOTHING
                """, (ed["relation"], ed["weight"] or 1.0, ed["source_type"] or "quick_desktop",
                      ed["from_cat"], ed["from_name"], ed["to_cat"], ed["to_name"]))
                if cur.rowcount > 0:
                    edge_count += 1
                else:
                    edge_failed += 1

            # Bridge: link QD memories to entities by matching people names
            cur.execute("""
                INSERT INTO memory_entities (memory_id, entity_id, relation)
                SELECT m.id, e.id, 'about'
                FROM memories m
                JOIN entities e ON m.content ILIKE '%%' || e.name || '%%'
                WHERE m.source_url LIKE 'qd://memory/%%'
                  AND e.category = 'Person'
                  AND length(e.name) > 5
                ON CONFLICT DO NOTHING
            """)
            bridge_count = cur.rowcount

        conn.commit()

    logger.info("KG import: %d entities, %d edges, %d memory-entity links (bridge)", ent_count, edge_count, bridge_count)
    return {"entities": ent_count, "edges": edge_count, "bridge": bridge_count, "edge_failed": edge_failed, "max_entity_id": max_ent_id}


def main():
    parser = argparse.ArgumentParser(description="Import Quick Desktop memories into Second Brain")
    parser.add_argument("--dry-run", action="store_true", help="Parse without writing to DB")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="Min effective_confidence threshold")
    parser.add_argument("--full", action="store_true", help="Full backfill (ignore sync state)")
    parser.add_argument("--no-decisions", action="store_true", help="Skip KG Decision entities")
    parser.add_argument("--no-kg", action="store_true", help="Skip KG graph import (entities + edges)")
    args = parser.parse_args()

    if not args.dry_run and not is_reachable():
        logger.error("PostgreSQL not reachable")
        sys.exit(1)

    qd_conn = open_qd_db()
    state = load_sync_state() if not args.full else {"last_memory_id": 0, "last_entity_id": 0, "last_kg_entity_id": 0}
    already = get_processed_source_urls("quick_desktop") if not args.dry_run else set()
    # Also grab all qd:// source URLs across all QD source types
    if not args.dry_run:
        from src.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT source_url FROM memories WHERE source_url LIKE 'qd://%'")
                already.update(row[0] for row in cur.fetchall())

    stats = {"memories": 0, "decisions": 0, "skipped_dup": 0, "skipped_filter": 0, "failed": 0}

    # --- Phase 1: Memories ---
    memories = fetch_memories(qd_conn, args.min_confidence, state["last_memory_id"])
    logger.info("Found %d QD memories to process (since id=%d, min_conf=%.2f)",
                len(memories), state["last_memory_id"], args.min_confidence)

    max_mem_id = state["last_memory_id"]
    for row in memories:
        mapped = map_memory(row)
        max_mem_id = max(max_mem_id, row["id"])

        if mapped["source_url"] in already:
            stats["skipped_dup"] += 1
            continue

        if args.dry_run:
            stats["memories"] += 1
            logger.info("[DRY RUN] Would import memory %d: %s (%s)", row["id"], mapped["title"][:60], mapped["type"])
        else:
            try:
                mid = create_memory(**mapped)
                stats["memories"] += 1
                already.add(mapped["source_url"])
                logger.debug("Imported memory %d -> %s", row["id"], mid)
            except Exception as e:
                logger.error("Failed memory %d: %s", row["id"], e)
                stats["failed"] += 1

    # --- Phase 2: KG Decisions ---
    max_ent_id = state["last_entity_id"]
    if not args.no_decisions:
        decisions = fetch_kg_decisions(qd_conn, state["last_entity_id"])
        logger.info("Found %d KG Decision entities to process", len(decisions))

        for row in decisions:
            mapped = map_decision(row)
            max_ent_id = max(max_ent_id, row["id"])

            if mapped["source_url"] in already:
                stats["skipped_dup"] += 1
                continue

            if args.dry_run:
                stats["decisions"] += 1
                logger.info("[DRY RUN] Would import decision %d: %s", row["id"], mapped["title"][:60])
            else:
                try:
                    mid = create_memory(**mapped)
                    stats["decisions"] += 1
                    already.add(mapped["source_url"])
                except Exception as e:
                    logger.error("Failed decision %d: %s", row["id"], e)
                    stats["failed"] += 1

    qd_conn.close()

    # --- Phase 3: KG Graph (entities + edges) ---
    kg_stats = {"entities": 0, "edges": 0, "bridge": 0, "edge_failed": 0, "max_entity_id": state.get("last_kg_entity_id", 0)}
    if not args.no_kg:
        qd_conn = open_qd_db()
        kg_stats = import_kg_graph(qd_conn, dry_run=args.dry_run, since_entity_id=state.get("last_kg_entity_id", 0))
        qd_conn.close()

    # Save sync state
    if not args.dry_run:
        state["last_memory_id"] = max_mem_id
        state["last_entity_id"] = max_ent_id
        state["last_kg_entity_id"] = kg_stats["max_entity_id"]
        save_sync_state(state)

    print(f"\nQuick Desktop migration complete:")
    print(f"  Memories imported:  {stats['memories']}")
    print(f"  Decisions imported: {stats['decisions']}")
    print(f"  KG entities:       {kg_stats['entities']}")
    print(f"  KG edges:          {kg_stats['edges']}")
    print(f"  Memory-entity links: {kg_stats.get('bridge', 0)}")
    print(f"  Skipped (dup):     {stats['skipped_dup']}")
    print(f"  Failed:            {stats['failed']}")
    print(f"  Sync state:        memory_id={max_mem_id}, entity_id={max_ent_id}")


if __name__ == "__main__":
    main()
