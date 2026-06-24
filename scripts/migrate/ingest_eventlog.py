#!/usr/bin/env python3
"""Ingest Quick Desktop eventlog feed events into the Second Brain.

Reads ~/.quickwork/eventlog/events.jsonl and creates memories for each
curated feed event (Slack notifications, email FYIs, day plans, etc.).

Usage: .venv/bin/python scripts/migrate/ingest_eventlog.py [--dry-run]

Idempotent — uses source_url 'qd-feed://{event_id}' for deduplication.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import create_memory, get_processed_source_urls, get_connection
from src.embeddings import generate_embedding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from src.qd_profile import qd_path

EVENTLOG_PATH = qd_path("eventlog", "events.jsonl")
INTERACTIONS_PATH = qd_path("eventlog", "interactions.jsonl")
SOURCE_TYPE = "quick_desktop_feed"


def parse_events(path):
    """Parse JSONL eventlog file. Returns list of event dicts."""
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def source_url_for_event(event):
    """Generate dedup source_url for an event."""
    return f"qd-feed://{event['id']}"


def tags_for_event(event):
    """Generate tags for an event based on its source and type."""
    tags = ["qd-feed"]
    source = event.get("source", "")
    if ":" in source:
        tags.append(source.split(":", 1)[1])  # e.g. "slack-monitor"
    tags.append(event.get("event_type", "unknown"))
    return tags


def format_event_as_markdown(event):
    """Format an event as markdown with metadata header for ingestion."""
    summary = event.get("summary", "Untitled Event")
    details = event.get("details", {})
    full_message = details.get("full_message", "")
    context = details.get("context", {})

    lines = [
        f"# {summary}",
        "",
        f"Source-Type: {SOURCE_TYPE}",
        f"Source-ID: {event['id']}",
        f"Date: {event.get('timestamp', '')[:10]}",
        f"Event-Type: {event.get('event_type', '')}",
        f"Agent-Source: {event.get('source', '')}",
    ]

    if context and isinstance(context, dict):
        for k, v in context.items():
            if isinstance(v, str) and v:
                lines.append(f"{k}: {v}")

    lines.extend(["", "---", "", full_message])

    importance = details.get("importance")
    if importance:
        lines.extend(["", f"**Importance:** {importance}"])

    choices = details.get("choices", [])
    if choices:
        lines.append("\n**Suggested actions:**")
        for c in choices:
            label = c.get("label", "")
            if label:
                lines.append(f"- {label}")

    return "\n".join(lines)


def ingest_eventlog(dry_run=False):
    """Ingest all events from the eventlog. Returns stats dict."""
    if not os.path.exists(EVENTLOG_PATH):
        log.error(f"Eventlog not found: {EVENTLOG_PATH}")
        return {"processed": 0, "skipped": 0, "failed": 0}

    events = parse_events(EVENTLOG_PATH)
    log.info(f"Found {len(events)} events in eventlog")

    already = get_processed_source_urls(SOURCE_TYPE) if not dry_run else set()

    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for event in events:
        url = source_url_for_event(event)

        if url in already:
            stats["skipped"] += 1
            continue

        if dry_run:
            stats["processed"] += 1
            continue

        try:
            md = format_event_as_markdown(event)
            tags = tags_for_event(event)
            embedding = generate_embedding(md)

            create_memory(
                type="source",
                title=event.get("summary", "QD Feed Event"),
                content=md,
                embedding=embedding,
                tags=tags,
                source_url=url,
                source_type=SOURCE_TYPE,
                metadata={
                    "event_type": event.get("event_type"),
                    "agent_source": event.get("source"),
                    "importance": event.get("details", {}).get("importance"),
                    "timestamp": event.get("timestamp"),
                },
            )
            stats["processed"] += 1
        except Exception as e:
            stats["failed"] += 1
            log.error(f"Failed {event.get('id')}: {e}")

    log.info(f"Done: processed={stats['processed']} skipped={stats['skipped']} failed={stats['failed']}")
    return stats


def parse_interactions(path):
    """Parse interactions JSONL file."""
    interactions = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                interactions.append(json.loads(line))
    return interactions


def group_interactions_by_event(interactions):
    """Group interactions by their feed_event_id."""
    grouped = {}
    for i in interactions:
        eid = i.get("feed_event_id")
        if eid:
            grouped.setdefault(eid, []).append(i)
    return grouped


def classify_engagement(interaction_type):
    """Classify interaction as active (user-initiated) or passive (auto/agent)."""
    if interaction_type in ("link_click", "recommendation_click", "card_resolve"):
        return "active"
    return "passive"


def build_interaction_metadata(interactions):
    """Build metadata summary from a list of interactions for one event."""
    types = [i["interaction_type"] for i in interactions]
    engagements = [classify_engagement(t) for t in types]
    return {
        "interaction_count": len(interactions),
        "interaction_types": types,
        "engagement": "active" if "active" in engagements else "passive",
        "first_interaction": interactions[0].get("timestamp"),
        "last_interaction": interactions[-1].get("timestamp"),
    }


def enrich_with_interactions(dry_run=False):
    """Enrich feed event memories with interaction metadata."""
    if not os.path.exists(INTERACTIONS_PATH):
        log.error(f"Interactions file not found: {INTERACTIONS_PATH}")
        return {"enriched": 0, "skipped": 0, "not_found": 0}

    interactions = parse_interactions(INTERACTIONS_PATH)
    grouped = group_interactions_by_event(interactions)
    log.info(f"Found {len(interactions)} interactions across {len(grouped)} events")

    stats = {"enriched": 0, "skipped": 0, "not_found": 0}

    with get_connection() as conn:
        with conn.cursor() as cur:
            for event_id, event_interactions in grouped.items():
                source_url = f"qd-feed://{event_id}"
                cur.execute("SELECT id, metadata FROM memories WHERE source_url = %s", (source_url,))
                row = cur.fetchone()
                if not row:
                    stats["not_found"] += 1
                    continue

                mem_id, existing_meta = row
                if isinstance(existing_meta, str):
                    existing_meta = json.loads(existing_meta)
                existing_meta = existing_meta or {}

                if "interactions" in existing_meta:
                    stats["skipped"] += 1
                    continue

                if dry_run:
                    stats["enriched"] += 1
                    continue

                existing_meta["interactions"] = build_interaction_metadata(event_interactions)
                cur.execute("UPDATE memories SET metadata = %s WHERE id = %s",
                            (json.dumps(existing_meta), mem_id))
                stats["enriched"] += 1

        if not dry_run:
            conn.commit()

    log.info(f"Interactions: enriched={stats['enriched']} skipped={stats['skipped']} not_found={stats['not_found']}")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ingest_eventlog(dry_run=args.dry_run)
    enrich_with_interactions(dry_run=args.dry_run)
    if args.dry_run:
        log.info("DRY RUN — no changes written")


if __name__ == "__main__":
    main()
