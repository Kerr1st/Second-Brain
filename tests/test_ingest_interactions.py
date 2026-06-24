"""Tests for Quick Desktop eventlog interaction ingestion."""

import json
import os
import tempfile
import pytest
from unittest.mock import patch
from hypothesis import given, strategies as st

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SAMPLE_INTERACTIONS = [
    {
        "id": "int_00001",
        "timestamp": "2026-04-13T20:03:51.935207",
        "interaction_type": "link_click",
        "feed_event_id": "evt_00001",
        "details": {"label": "Open in Slack", "url": "https://slack.com/archives/C123"},
    },
    {
        "id": "int_00002",
        "timestamp": "2026-04-13T21:43:34.183034",
        "interaction_type": "card_resolve",
        "feed_event_id": "evt_00002",
        "details": {"reason": "Done manually"},
    },
    {
        "id": "int_00003",
        "timestamp": "2026-04-14T06:16:04.708872",
        "interaction_type": "card_auto_resolve",
        "feed_event_id": "evt_00003",
        "details": {"reason": "expired"},
    },
]


@pytest.fixture
def interactions_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for item in SAMPLE_INTERACTIONS:
            f.write(json.dumps(item) + "\n")
        path = f.name
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestParseInteractions:

    def test_parse_interactions(self, interactions_file):
        from scripts.migrate.ingest_eventlog import parse_interactions
        result = parse_interactions(interactions_file)
        assert len(result) == 3

    def test_group_by_event(self):
        from scripts.migrate.ingest_eventlog import group_interactions_by_event
        grouped = group_interactions_by_event(SAMPLE_INTERACTIONS)
        assert "evt_00001" in grouped
        assert len(grouped["evt_00001"]) == 1
        assert grouped["evt_00001"][0]["interaction_type"] == "link_click"

    def test_classify_engagement(self):
        from scripts.migrate.ingest_eventlog import classify_engagement
        # Active engagement: link_click, recommendation_click, card_resolve
        assert classify_engagement("link_click") == "active"
        assert classify_engagement("recommendation_click") == "active"
        assert classify_engagement("card_resolve") == "active"
        # Passive: auto_resolve, agent_resolve
        assert classify_engagement("card_auto_resolve") == "passive"
        assert classify_engagement("card_agent_resolve") == "passive"

    def test_build_interaction_metadata(self):
        from scripts.migrate.ingest_eventlog import build_interaction_metadata
        interactions = [SAMPLE_INTERACTIONS[0]]  # link_click
        meta = build_interaction_metadata(interactions)
        assert meta["interaction_count"] == 1
        assert meta["engagement"] == "active"
        assert "link_click" in meta["interaction_types"]


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

class TestInteractionProperties:

    @given(
        interaction_type=st.sampled_from([
            "link_click", "recommendation_click", "card_resolve",
            "card_auto_resolve", "card_agent_resolve",
        ])
    )
    def test_classify_always_returns_valid_level(self, interaction_type):
        from scripts.migrate.ingest_eventlog import classify_engagement
        result = classify_engagement(interaction_type)
        assert result in ("active", "passive")

    @given(
        n=st.integers(min_value=1, max_value=10),
        types=st.lists(
            st.sampled_from(["link_click", "recommendation_click", "card_resolve", "card_auto_resolve"]),
            min_size=1, max_size=10,
        ),
    )
    def test_metadata_count_matches_input(self, n, types):
        from scripts.migrate.ingest_eventlog import build_interaction_metadata
        interactions = [{"interaction_type": t, "timestamp": "2026-01-01", "details": {}} for t in types]
        meta = build_interaction_metadata(interactions)
        assert meta["interaction_count"] == len(types)


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    import psycopg2
    try:
        conn = psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "5432")),
            dbname="memory_bank_test",
            user=os.environ.get("DB_USER", "memory_bank"),
            password=os.environ.get("DB_PASSWORD", "memory_bank"),
        )
        conn.autocommit = True
        yield conn
        conn.close()
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL not available")


@pytest.fixture
def seeded_feed_events(test_db):
    """Insert feed event memories that interactions will enrich."""
    with test_db.cursor() as cur:
        for i in range(1, 4):
            cur.execute("""
                INSERT INTO memories (type, title, content, source_url, source_type, metadata, tags)
                VALUES ('source', %s, 'test content', %s, 'quick_desktop_feed', '{}', %s)
                ON CONFLICT DO NOTHING
            """, (
                f"Test Event {i}",
                f"qd-feed://evt_0000{i}",
                ["qd-feed"],
            ))


class TestInteractionsE2E:

    def test_enrich_adds_interaction_metadata(self, test_db, seeded_feed_events, interactions_file):
        from scripts.migrate.ingest_eventlog import enrich_with_interactions

        with patch("scripts.migrate.ingest_eventlog.INTERACTIONS_PATH", interactions_file):
            stats = enrich_with_interactions(dry_run=False)

        assert stats["enriched"] >= 2  # evt_00001 and evt_00002 have active interactions

        with test_db.cursor() as cur:
            cur.execute("SELECT metadata FROM memories WHERE source_url = 'qd-feed://evt_00001'")
            row = cur.fetchone()
            if row:
                meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                assert "interactions" in meta

    def test_enrich_is_idempotent(self, test_db, seeded_feed_events, interactions_file):
        from scripts.migrate.ingest_eventlog import enrich_with_interactions

        with patch("scripts.migrate.ingest_eventlog.INTERACTIONS_PATH", interactions_file):
            enrich_with_interactions(dry_run=False)
            stats2 = enrich_with_interactions(dry_run=False)

        assert stats2["enriched"] == 0
        assert stats2["skipped"] > 0

    def test_dry_run_changes_nothing(self, test_db, seeded_feed_events, interactions_file):
        from scripts.migrate.ingest_eventlog import enrich_with_interactions

        # Clear any previous enrichment
        with test_db.cursor() as cur:
            cur.execute("UPDATE memories SET metadata = '{}' WHERE source_url LIKE 'qd-feed://evt_0000%'")

        with patch("scripts.migrate.ingest_eventlog.INTERACTIONS_PATH", interactions_file):
            stats = enrich_with_interactions(dry_run=True)

        assert stats["enriched"] > 0

        with test_db.cursor() as cur:
            cur.execute("SELECT metadata FROM memories WHERE source_url = 'qd-feed://evt_00001'")
            row = cur.fetchone()
            if row:
                meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                assert "interactions" not in meta
