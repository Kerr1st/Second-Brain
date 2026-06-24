#!/usr/bin/env python3
"""Import Slack social graph (channels + users) into the Second Brain knowledge graph.

Reads ~/.quickwork/slack_cache/channels.jsonl and users.jsonl, upserting
Channel and Person entities into the entities table.

Usage: .venv/bin/python scripts/migrate/import_slack_graph.py [--dry-run]

Idempotent — uses ON CONFLICT (category, name) DO UPDATE to refresh properties.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from src.qd_profile import slack_cache_dir

_SLACK_DIR = slack_cache_dir()
CHANNELS_PATH = os.path.join(_SLACK_DIR, "channels.jsonl")
USERS_PATH = os.path.join(_SLACK_DIR, "users.jsonl")
SOURCE_TYPE = "slack_cache"


def parse_channels(path):
    """Parse channels JSONL. Returns list of channel dicts."""
    channels = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                channels.append(json.loads(line))
    return channels


def parse_users(path):
    """Parse users JSONL. Filters out bots. Returns list of user dicts."""
    users = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            u = json.loads(line)
            if not u.get("is_bot", False):
                users.append(u)
    return users


def build_channel_entity(ch):
    """Build an entity record from a channel dict. Returns None for unnamed (IM/DM) channels."""
    name = ch.get("name")
    if not name:
        return None
    parts = []
    if ch.get("topic"):
        parts.append(ch["topic"])
    if ch.get("purpose"):
        parts.append(ch["purpose"])
    summary = " — ".join(parts) if parts else None

    return {
        "category": "Channel",
        "name": name,
        "summary": summary,
        "source_type": SOURCE_TYPE,
        "properties": {
            "slack_id": ch["id"],
            "is_private": ch.get("is_private", False),
            "is_archived": ch.get("is_archived", False),
            "is_member": ch.get("is_member", False),
            "num_members": ch.get("num_members", 0),
        },
    }


def build_person_entity(u):
    """Build an entity record from a user dict (supports nested 'profile')."""
    p = u.get("profile") or {}
    real_name = u.get("real_name") or p.get("real_name") or ""
    alias = u.get("display_name") or p.get("display_name") or u.get("name") or ""
    name = real_name or alias
    if not name:
        return None
    return {
        "category": "Person",
        "name": name,
        "summary": f"Slack user @{alias}",
        "source_type": SOURCE_TYPE,
        "properties": {
            "slack_id": u["id"],
            "alias": alias,
            "is_admin": u.get("is_admin", False),
            "deleted": u.get("deleted", False),
        },
    }


def run_import(channels_path=None, users_path=None, dry_run=False):
    """Run the import. Returns stats dict."""
    channels_path = channels_path or CHANNELS_PATH
    users_path = users_path or USERS_PATH

    channels = parse_channels(channels_path) if os.path.exists(channels_path) else []
    users = parse_users(users_path) if os.path.exists(users_path) else []

    log.info(f"Parsed {len(channels)} channels, {len(users)} users (non-bot)")

    channel_entities = [e for e in (build_channel_entity(ch) for ch in channels) if e]
    person_entities = [e for e in (build_person_entity(u) for u in users) if e]

    if dry_run:
        log.info(f"DRY RUN — would upsert {len(channel_entities)} channels, {len(person_entities)} persons")
        return {"channels_upserted": len(channel_entities), "persons_upserted": len(person_entities)}

    ch_count = 0
    p_count = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            for e in channel_entities:
                cur.execute("""
                    INSERT INTO entities (category, name, summary, source_type, properties)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (category, name) DO UPDATE SET
                        summary = COALESCE(EXCLUDED.summary, entities.summary),
                        properties = EXCLUDED.properties,
                        source_type = EXCLUDED.source_type,
                        updated_at = now()
                """, (e["category"], e["name"], e["summary"], e["source_type"],
                      json.dumps(e["properties"])))
                ch_count += 1

            for e in person_entities:
                cur.execute("""
                    INSERT INTO entities (category, name, summary, source_type, properties)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (category, name) DO UPDATE SET
                        summary = COALESCE(EXCLUDED.summary, entities.summary),
                        properties = entities.properties || EXCLUDED.properties,
                        updated_at = now()
                """, (e["category"], e["name"], e["summary"], e["source_type"],
                      json.dumps(e["properties"])))
                p_count += 1

        conn.commit()

    log.info(f"Done: channels_upserted={ch_count} persons_upserted={p_count}")
    return {"channels_upserted": ch_count, "persons_upserted": p_count}


def main():
    parser = argparse.ArgumentParser(description="Import Slack social graph into KG")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_import(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
