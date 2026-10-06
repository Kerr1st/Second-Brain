"""R4 Quick Desktop fixes: feed rendering is byte-stable with the rows the legacy
writer stored (fix 4, D N-3) and the facts/tags stages commute (fix 5, D N-4).

Synthetic events and records against the disposable test database; the
embedder is a Mock, so a re-embed is an assertion failure, not a cost.
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import Mock

import pytest


# --------------------------------------------------------------------------- #
# Fix 4 — QD feed rendering is byte-stable with the rows the legacy writer stored
# --------------------------------------------------------------------------- #
FEED_SOURCE_TYPE = "quick_desktop_feed"


def legacy_format_event_as_markdown(event):
    """Verbatim copy of scripts/migrate/ingest_eventlog.py:format_event_as_markdown
    at R3 head (e1cff5c) — the writer of every live quick_desktop_feed row."""
    summary = event.get("summary", "Untitled Event")
    details = event.get("details", {})
    full_message = details.get("full_message", "")
    context = details.get("context", {})
    lines = [
        f"# {summary}",
        "",
        f"Source-Type: {FEED_SOURCE_TYPE}",
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


FEED_EVENTS = [
    {"id": "test-evt_a", "timestamp": "2026-04-13T20:00:52.249329", "source": "agent:slack-monitor",
     "event_type": "notification", "summary": "Plain summary ",
     "details": {"full_message": "**Someone** replied.", "context": {"channel_name": "general", "user_name": "Someone"},
                 "importance": "important", "choices": [{"label": "Open"}, {"label": ""}]}},
    {"id": "test-evt_b", "timestamp": "2026-04-14T08:00:00", "source": "agent:day-planner", "event_type": "day_plan",
     "summary": "Multi\nline summary", "details": {"full_message": "## Focus\n- One\n- Two"}},
    {"id": "test-evt_c", "timestamp": "", "source": "", "event_type": "",
     "summary": "Edge: hyphen-key context", "details": {"full_message": "", "context": {"Odd Key-Name": "value with  spaces ", "n": 3}}},
    {"id": "test-evt_d", "details": {}},
]


@pytest.mark.parametrize("event", FEED_EVENTS, ids=[e["id"] for e in FEED_EVENTS])
def test_qd_eventlog_rendering_is_byte_stable_with_legacy_rows(event):
    from src.capture.qd_eventlog import format_event_as_markdown
    assert format_event_as_markdown(event) == legacy_format_event_as_markdown(event)


def _seed_legacy_feed_row(conn, event):
    """What the legacy writer stored: full markdown as content, one leaf vector, no chunks."""
    from src import db
    from src.capture.qd_eventlog import tags_for_event, source_url_for_event
    return db.create_memory(type="source", title=event.get("summary", "QD Feed Event"),
                            content=legacy_format_event_as_markdown(event), embedding=[0.5] * 1024,
                            tags=tags_for_event(event), source_url=source_url_for_event(event),
                            source_type=FEED_SOURCE_TYPE,
                            metadata={"event_type": event.get("event_type"), "agent_source": event.get("source"),
                                      "timestamp": event.get("timestamp")})


def test_first_qd_sync_re_embeds_zero_unchanged_feed_rows(test_conn, tmp_path, monkeypatch):
    from src import db
    from src.capture import qd_eventlog
    import src.ingest
    ids = [_seed_legacy_feed_row(test_conn, e) for e in FEED_EVENTS[:3]]
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in FEED_EVENTS[:3]))
    embed = Mock(side_effect=AssertionError("unchanged rows must not be re-embedded"))
    monkeypatch.setattr(src.ingest, "generate_embedding", embed)
    monkeypatch.setattr(qd_eventlog, "EVENTLOG_PATH", str(log))
    stats = qd_eventlog.ingest_eventlog(dry_run=False)
    assert stats["failed"] == 0 and embed.call_count == 0
    with test_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE parent_id IS NOT NULL AND source_type = %s", (FEED_SOURCE_TYPE,))
        assert cur.fetchone()[0] == 0, "no duplicate chunk children under unchanged leaf rows"
        cur.execute("SELECT count(*) FROM memories WHERE parent_id IS NULL AND source_type = %s AND embedding IS NOT NULL", (FEED_SOURCE_TYPE,))
        assert cur.fetchone()[0] == 3, "leaf vectors untouched"
    for mid in ids:
        assert db.get_memory(mid)["metadata"]["capture_identity"] is True


def test_changed_feed_event_is_refreshed_and_its_stale_leaf_vector_cleared(test_conn, tmp_path, monkeypatch):
    from src import db
    from src.capture import qd_eventlog
    import src.ingest
    mid = _seed_legacy_feed_row(test_conn, FEED_EVENTS[0])
    changed = json.loads(json.dumps(FEED_EVENTS[0]))
    changed["details"]["full_message"] = "**Someone** replied, then edited the message."
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps(changed) + "\n")
    embed = Mock(return_value=[0.25] * 1024)
    monkeypatch.setattr(src.ingest, "generate_embedding", embed)
    monkeypatch.setattr(src.ingest, "_discover_relationships", lambda *a: None)
    monkeypatch.setattr(qd_eventlog, "EVENTLOG_PATH", str(log))
    stats = qd_eventlog.ingest_eventlog(dry_run=False)
    assert stats["processed"] == 1 and embed.call_count == 1
    row = db.get_memory(mid)
    assert row["content"] == legacy_format_event_as_markdown(changed) and row["embedding"] is None
    with test_conn.cursor() as cur:
        cur.execute("SELECT count(*) FILTER (WHERE status='active'), count(*) FILTER (WHERE embedding IS NOT NULL) FROM memories WHERE parent_id = %s", (mid,))
        assert cur.fetchone() == (1, 1)
        cur.execute("SELECT count(*) FROM memories WHERE parent_id IS NULL AND source_type = %s", (FEED_SOURCE_TYPE,))
        assert cur.fetchone()[0] == 1, "same identity, no second parent"


# --------------------------------------------------------------------------- #
# Fix 5 — facts and tags stages commute (N-4)                                   #
# --------------------------------------------------------------------------- #
def _fact_record(tags=("qd:people", "qd_type:fact")):
    return {"type": "source", "title": "Synthetic person", "content": "A synthetic colleague works on retrieval.",
            "source_type": "quick_desktop_people", "source_url": "qd://memory/9001", "confidence": 0.9,
            "tags": list(tags), "metadata": {"qd_id": 9001, "qd_category": "people"}, "mem_class": "semantic"}


def test_facts_refresh_merges_enriched_tags_instead_of_overwriting(test_conn, monkeypatch):
    from src import db
    from src.capture.framework import run_records
    from src.capture.qd_tags import merge_tags
    import src.ingest
    monkeypatch.setattr(src.ingest, "generate_embedding", lambda text: [0.1] * 1024)
    monkeypatch.setattr(src.ingest, "_discover_relationships", lambda *a: None)
    assert run_records([_fact_record()], refresh=True)["quick_desktop_people"]["processed"] == 1
    with test_conn.cursor() as cur:
        cur.execute("SELECT id, tags FROM memories WHERE source_url = 'qd://memory/9001' AND parent_id IS NULL")
        mid, tags = cur.fetchone()
        enriched = merge_tags(tags, ["people", "css-team"])  # what qd_tags.enrich_memories writes
        cur.execute("UPDATE memories SET tags = %s WHERE id = %s", (enriched, mid))
        cur.execute("SELECT updated_at FROM memories WHERE id = %s", (mid,))
        before = cur.fetchone()[0]
    time.sleep(0.01)
    second = run_records([_fact_record()], refresh=True)["quick_desktop_people"]
    assert second == {"processed": 0, "skipped": 1, "failed": 0}, "nothing to write once tags are merged"
    row = db.get_memory(mid)
    assert row["tags"] == ["qd:people", "qd_type:fact", "people", "css-team"]
    assert row["updated_at"] == before


def test_tag_merge_also_applies_when_content_changes(test_conn, monkeypatch):
    from src import db
    from src.capture.framework import run_records
    import src.ingest
    monkeypatch.setattr(src.ingest, "generate_embedding", lambda text: [0.1] * 1024)
    monkeypatch.setattr(src.ingest, "_discover_relationships", lambda *a: None)
    run_records([_fact_record()], refresh=True)
    with test_conn.cursor() as cur:
        cur.execute("UPDATE memories SET tags = tags || ARRAY['enriched'] WHERE source_url = 'qd://memory/9001'")
    changed = _fact_record(tags=("qd:people", "qd_type:fact", "qd:renamed"))
    changed["content"] = "A synthetic colleague now works on ingestion."
    assert run_records([changed], refresh=True)["quick_desktop_people"]["processed"] == 1
    with test_conn.cursor() as cur:
        cur.execute("SELECT tags FROM memories WHERE source_url = 'qd://memory/9001' AND parent_id IS NULL")
        assert cur.fetchone()[0] == ["qd:people", "qd_type:fact", "enriched", "qd:renamed"]


def test_merge_tags_keeps_order_and_dedups():
    from src.ingest import merge_tags
    assert merge_tags(["b", "a"], ["a", "c", "b", "c"]) == ["b", "a", "c"]
    assert merge_tags(None, ["x"]) == ["x"] and merge_tags(["x"], None) == ["x"]
