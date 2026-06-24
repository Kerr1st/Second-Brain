"""Tests for Quick Desktop eventlog feed ingestion."""

import json
import os
import tempfile
import pytest
from unittest.mock import patch
from hypothesis import given, strategies as st

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {
        "id": "evt_00001",
        "timestamp": "2026-04-13T20:00:52.249329",
        "source": "agent:slack-monitor",
        "event_type": "notification",
        "summary": "Ryan answered your FedRAMP questions",
        "details": {
            "title": "Ryan answered your FedRAMP questions",
            "full_message": "**Ryan** replied in #kiro-field-interest with FedRAMP answers.",
            "context": {"channel_name": "kiro-field-interest", "user_name": "Ryan"},
            "importance": "important",
        },
    },
    {
        "id": "evt_00002",
        "timestamp": "2026-04-14T08:00:00.000000",
        "source": "agent:day-planner",
        "event_type": "day_plan",
        "summary": "Your day plan for Monday",
        "details": {
            "title": "Day Plan — Monday April 14",
            "full_message": "## Focus\n- Finish enterprise readiness note\n- Review project docs",
        },
    },
    {
        "id": "evt_00003",
        "timestamp": "2026-04-14T09:30:00.000000",
        "source": "agent:outlook-monitor",
        "event_type": "email_fyi",
        "summary": "Team meeting rescheduled",
        "details": {
            "title": "Team meeting rescheduled",
            "full_message": "CSCOE team meeting moved to Thursday.",
            "context": {"subject": "CSCOE ALL - Team mtg", "sender_name": "Bhargs"},
        },
    },
]


@pytest.fixture
def eventlog_file():
    """Create a temp eventlog JSONL file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for event in SAMPLE_EVENTS:
            f.write(json.dumps(event) + "\n")
        path = f.name
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestParseEvents:
    """Unit tests for event parsing."""

    def test_parse_events_from_jsonl(self, eventlog_file):
        from scripts.migrate.ingest_eventlog import parse_events
        events = parse_events(eventlog_file)
        assert len(events) == 3
        assert events[0]["id"] == "evt_00001"

    def test_format_event_as_markdown(self):
        from scripts.migrate.ingest_eventlog import format_event_as_markdown
        md = format_event_as_markdown(SAMPLE_EVENTS[0])
        assert "# Ryan answered your FedRAMP questions" in md
        assert "Source-Type: quick_desktop_feed" in md
        assert "evt_00001" in md
        assert "FedRAMP" in md

    def test_format_event_includes_full_message(self):
        from scripts.migrate.ingest_eventlog import format_event_as_markdown
        md = format_event_as_markdown(SAMPLE_EVENTS[0])
        assert "replied in #kiro-field-interest" in md

    def test_format_event_includes_metadata(self):
        from scripts.migrate.ingest_eventlog import format_event_as_markdown
        md = format_event_as_markdown(SAMPLE_EVENTS[0])
        assert "agent:slack-monitor" in md
        assert "notification" in md

    def test_source_url_from_event(self):
        from scripts.migrate.ingest_eventlog import source_url_for_event
        assert source_url_for_event(SAMPLE_EVENTS[0]) == "qd-feed://evt_00001"

    def test_tags_from_event(self):
        from scripts.migrate.ingest_eventlog import tags_for_event
        tags = tags_for_event(SAMPLE_EVENTS[0])
        assert "qd-feed" in tags
        assert "slack-monitor" in tags
        assert "notification" in tags


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

class TestEventProperties:
    """Property-based tests for event formatting invariants."""

    @given(
        event_id=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz0123456789_"),
        summary=st.text(min_size=1, max_size=100),
        event_type=st.sampled_from(["notification", "email_fyi", "day_plan", "slack_dm", "observation"]),
        source=st.sampled_from(["agent:slack-monitor", "agent:outlook-monitor", "agent:day-planner"]),
    )
    def test_format_always_produces_valid_markdown_header(self, event_id, summary, event_type, source):
        from scripts.migrate.ingest_eventlog import format_event_as_markdown
        event = {
            "id": event_id,
            "timestamp": "2026-04-13T20:00:00.000000",
            "source": source,
            "event_type": event_type,
            "summary": summary,
            "details": {"full_message": "test content"},
        }
        md = format_event_as_markdown(event)
        assert md.startswith("# ")
        assert "---" in md
        assert "Source-Type: quick_desktop_feed" in md

    @given(
        event_id=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz0123456789_"),
    )
    def test_source_url_always_has_prefix(self, event_id):
        from scripts.migrate.ingest_eventlog import source_url_for_event
        event = {"id": event_id}
        url = source_url_for_event(event)
        assert url.startswith("qd-feed://")
        assert event_id in url


# ---------------------------------------------------------------------------
# E2E Integration Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    """Connect to test database."""
    import psycopg2
    DB_CONFIG = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": "memory_bank_test",
        "user": os.environ.get("DB_USER", "memory_bank"),
        "password": os.environ.get("DB_PASSWORD", "memory_bank"),
    }
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        yield conn
        conn.close()
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL not available")


class TestEventlogE2E:
    """End-to-end tests for eventlog ingestion."""

    def test_ingest_events_creates_memories(self, test_db, eventlog_file):
        from scripts.migrate.ingest_eventlog import ingest_eventlog

        # Clean up any previous test data
        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-feed://evt_0000%'")

        with patch("scripts.migrate.ingest_eventlog.EVENTLOG_PATH", eventlog_file):
            with patch("scripts.migrate.ingest_eventlog.generate_embedding", return_value=[0.1] * 1024):
                stats = ingest_eventlog(dry_run=False)

        assert stats["processed"] == 3
        assert stats["failed"] == 0

        # Verify in DB
        with test_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE source_url LIKE 'qd-feed://evt_0000%'")
            assert cur.fetchone()[0] == 3

    def test_ingest_is_idempotent(self, test_db, eventlog_file):
        from scripts.migrate.ingest_eventlog import ingest_eventlog

        with patch("scripts.migrate.ingest_eventlog.EVENTLOG_PATH", eventlog_file):
            with patch("scripts.migrate.ingest_eventlog.generate_embedding", return_value=[0.1] * 1024):
                stats1 = ingest_eventlog(dry_run=False)
                stats2 = ingest_eventlog(dry_run=False)

        assert stats2["processed"] == 0
        assert stats2["skipped"] == 3

    def test_dry_run_creates_nothing(self, test_db, eventlog_file):
        from scripts.migrate.ingest_eventlog import ingest_eventlog

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-feed://evt_0000%'")

        with patch("scripts.migrate.ingest_eventlog.EVENTLOG_PATH", eventlog_file):
            stats = ingest_eventlog(dry_run=True)

        assert stats["processed"] == 3

        with test_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE source_url LIKE 'qd-feed://evt_0000%'")
            assert cur.fetchone()[0] == 0
