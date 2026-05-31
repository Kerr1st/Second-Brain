"""Tests for Quick Desktop session_events ingestion (message_user events)."""

import json
import os
import sqlite3
import tempfile
import pytest
from unittest.mock import patch
from hypothesis import given, strategies as st

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_SESSIONS = [
    {"id": "sess-001", "title": "Finding Will Mateos AI DLC Talk Recording"},
    {"id": "sess-002", "title": "Draft reply to Nicolas"},
]

SAMPLE_EVENTS = [
    {
        "id": "evt-aaa111",
        "session_id": "sess-001",
        "type": "message_user",
        "timestamp": 1776705709.50716,
        "data": json.dumps({
            "content": "Can you find a broadcast video for last Friday? Will Mateos gave a talk on the AI DLC.",
            "source": "user",
            "_agent_mode": "thinking",
        }),
        "message_id": "msg-001",
        "thread_id": None,
    },
    {
        "id": "evt-bbb222",
        "session_id": "sess-001",
        "type": "message_user",
        "timestamp": 1776705800.0,
        "data": json.dumps({
            "content": "Can you set up a reminder for yourself to check on that recording every 24 hours?",
            "source": "user",
        }),
        "message_id": "msg-002",
        "thread_id": None,
    },
    {
        "id": "evt-ccc333",
        "session_id": "sess-002",
        "type": "message_user",
        "timestamp": 1776790637.75173,
        "data": json.dumps({
            "content": "Draft a brief reply to Nicolas Sylvain's positive response about the Make-to-Bazel hackathon video.",
            "source": "user",
        }),
        "message_id": "msg-003",
        "thread_id": None,
    },
    {
        "id": "evt-ddd444",
        "session_id": "sess-002",
        "type": "message_user",
        "timestamp": 1776790687.0,
        "data": json.dumps({"content": "Send it", "source": "user"}),
        "message_id": "msg-004",
        "thread_id": None,
    },
]


@pytest.fixture
def sessions_db():
    """Create a temp SQLite sessions.db with sample data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New Conversation',
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE session_events (
        rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, session_id TEXT,
        type TEXT, timestamp REAL, data TEXT, message_id TEXT, thread_id TEXT)""")

    for s in SAMPLE_SESSIONS:
        conn.execute("INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, 0, 0)",
                     (s["id"], s["title"]))
    for e in SAMPLE_EVENTS:
        conn.execute(
            "INSERT INTO session_events (id, session_id, type, timestamp, data, message_id, thread_id) VALUES (?,?,?,?,?,?,?)",
            (e["id"], e["session_id"], e["type"], e["timestamp"], e["data"], e["message_id"], e["thread_id"]))
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestParsing:
    """Unit tests for event parsing and formatting."""

    def test_parse_event_content(self):
        from scripts.migrate.ingest_session_events import parse_event_content
        data_str = json.dumps({"content": "Hello world", "source": "user"})
        assert parse_event_content(data_str) == "Hello world"

    def test_parse_event_content_empty(self):
        from scripts.migrate.ingest_session_events import parse_event_content
        assert parse_event_content(json.dumps({"source": "user"})) == ""
        assert parse_event_content("") == ""
        assert parse_event_content("not json") == ""

    def test_source_url_generation(self):
        from scripts.migrate.ingest_session_events import source_url_for_event
        assert source_url_for_event("evt-abc123") == "qd-session-event://evt-abc123"

    def test_format_memory_content(self):
        from scripts.migrate.ingest_session_events import format_memory_content
        content = format_memory_content(
            "Can you find a broadcast video?",
            "Finding Will Mateos Talk",
            1776705709.50716,
        )
        assert "Can you find a broadcast video?" in content
        assert "Finding Will Mateos Talk" in content

    def test_is_meaningful_message(self):
        from scripts.migrate.ingest_session_events import is_meaningful_message
        assert is_meaningful_message("Can you find a broadcast video for last Friday?") is True
        assert is_meaningful_message("Send it") is False
        assert is_meaningful_message("yes") is False
        assert is_meaningful_message("ok sure thing") is False
        assert is_meaningful_message("") is False


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

class TestProperties:
    """Property-based tests for invariants."""

    @given(event_id=st.text(min_size=1, max_size=40, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-"))
    def test_source_urls_unique_per_event(self, event_id):
        from scripts.migrate.ingest_session_events import source_url_for_event
        url = source_url_for_event(event_id)
        assert url == f"qd-session-event://{event_id}"
        assert event_id in url

    @given(content=st.text(min_size=21, max_size=200))
    def test_format_always_non_empty(self, content):
        from scripts.migrate.ingest_session_events import format_memory_content
        result = format_memory_content(content, "Test Session", 1776705709.0)
        assert len(result) > 0
        assert content in result


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


class TestSessionEventsE2E:
    """End-to-end tests for session_events ingestion."""

    def test_ingest_creates_memories(self, test_db, sessions_db):
        from scripts.migrate.ingest_session_events import ingest_session_events

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-session-event://evt-%'")

        with patch("scripts.migrate.ingest_session_events.SESSIONS_DB_PATH", sessions_db):
            with patch("scripts.migrate.ingest_session_events.generate_embedding", return_value=[0.1] * 1024):
                stats = ingest_session_events(dry_run=False)

        # 4 events total, but evt-ddd444 ("Send it") is < 20 chars → 3 ingested
        assert stats["processed"] == 3
        assert stats["filtered"] == 1
        assert stats["failed"] == 0

        with test_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE source_url LIKE 'qd-session-event://evt-%'")
            assert cur.fetchone()[0] == 3

    def test_ingest_is_idempotent(self, test_db, sessions_db):
        from scripts.migrate.ingest_session_events import ingest_session_events

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-session-event://evt-%'")

        with patch("scripts.migrate.ingest_session_events.SESSIONS_DB_PATH", sessions_db):
            with patch("scripts.migrate.ingest_session_events.generate_embedding", return_value=[0.1] * 1024):
                stats1 = ingest_session_events(dry_run=False)
                stats2 = ingest_session_events(dry_run=False)

        assert stats2["processed"] == 0
        assert stats2["skipped"] == 3

    def test_dry_run_creates_nothing(self, test_db, sessions_db):
        from scripts.migrate.ingest_session_events import ingest_session_events

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-session-event://evt-%'")

        with patch("scripts.migrate.ingest_session_events.SESSIONS_DB_PATH", sessions_db):
            stats = ingest_session_events(dry_run=True)

        assert stats["processed"] == 3

        with test_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE source_url LIKE 'qd-session-event://evt-%'")
            assert cur.fetchone()[0] == 0
