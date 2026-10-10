"""Quick Desktop chat selection (F-094): private and incognito sessions are never captured."""
from __future__ import annotations

import sqlite3

import pytest

# Columns of the live sessions table that the selection reads; synthetic rows only.
CURRENT_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New Conversation',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, message_count INTEGER DEFAULT 0,
    agent_mode TEXT DEFAULT 'standard', session_type TEXT DEFAULT 'main', private INTEGER DEFAULT 0,
    deleted_at REAL DEFAULT NULL, incognito INTEGER DEFAULT 0, archived INTEGER DEFAULT 0);
CREATE TABLE session_messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_names TEXT);
"""
# A store written before the ``incognito``/``archived`` columns existed.
OLD_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    message_count INTEGER DEFAULT 0, agent_mode TEXT, private INTEGER DEFAULT 0, deleted_at REAL DEFAULT NULL);
CREATE TABLE session_messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_names TEXT);
"""


def _store(path, schema, rows):
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    for i, row in enumerate(rows):
        values = {"created_at": 1.0e9 + i, "updated_at": 1.0e9 + i, "message_count": 2, **row}
        conn.execute(f"INSERT INTO sessions ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
                     tuple(values.values()))
        for n, role in enumerate(("user", "assistant")):
            conn.execute("INSERT INTO session_messages VALUES (?,?,?,?,NULL)",
                         (row["id"], role, f"synthetic {role} message {n}", 1.0e9 + i + n))
    conn.commit()
    conn.close()
    return path


def _ids(path):
    from src.capture import qd_chats
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {row["id"] for row in qd_chats.eligible_sessions(conn)}
    finally:
        conn.close()


@pytest.fixture
def current_store(tmp_path):
    return _store(tmp_path / "sessions.db", CURRENT_SCHEMA, [
        {"id": "s-plain"},
        {"id": "s-private", "private": 1},
        {"id": "s-incognito", "incognito": 1},
        {"id": "s-private-null", "private": None},
        {"id": "s-deleted", "deleted_at": 1.0e9},
        {"id": "s-short", "message_count": 1},
        {"id": "s-archived", "archived": 1},
        {"id": "s-task", "session_type": "task"},
    ])


def test_private_and_incognito_sessions_are_excluded_one_fixture_row_per_flag(current_store):
    # NULL is the column default never being written, i.e. not flagged.
    assert _ids(current_store) == {"s-plain", "s-private-null", "s-archived", "s-task"}


def test_archived_and_non_main_sessions_stay_eligible_pending_a_product_decision(current_store):
    # Pins current behavior: ``archived`` and ``session_type`` are not privacy flags (F-094).
    assert {"s-archived", "s-task"} <= _ids(current_store)


def test_a_store_without_the_incognito_column_still_filters_private(tmp_path):
    path = _store(tmp_path / "old.db", OLD_SCHEMA, [{"id": "o-plain"}, {"id": "o-private", "private": 1}])
    assert _ids(path) == {"o-plain"}


def test_dry_run_plans_only_the_eligible_sessions(current_store, monkeypatch):
    from src.capture import qd_chats
    monkeypatch.setattr(qd_chats, "QD_SESSIONS_DB", str(current_store))
    result = qd_chats.capture(dry_run=True)
    assert (result["planned"], result["failed"], result["dry_run"]) == (4, 0, True)
