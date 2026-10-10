"""F-090: the Quick Desktop facts stage mirrors the current upstream selection.

A ``qd://`` parent the selection no longer contains is retired (status
``superseded`` with its active chunks, attributable via ``metadata.capture_retired``)
and is reactivated when it is selected again. Synthetic upstream rows only.
"""
from __future__ import annotations

import sqlite3

import pytest

QD_SCHEMA = """
CREATE TABLE nodes (id INTEGER PRIMARY KEY);
CREATE TABLE memories (id INTEGER PRIMARY KEY, node INTEGER, memory_type TEXT NOT NULL, category TEXT,
    name TEXT, source TEXT DEFAULT 'global', trigger_text TEXT NOT NULL, confidence REAL DEFAULT 0.5,
    effective_confidence REAL DEFAULT 0.5, retrieval_count INTEGER DEFAULT 0);
CREATE TABLE entities (id INTEGER PRIMARY KEY, node INTEGER, category TEXT NOT NULL, name TEXT NOT NULL, properties BLOB);
CREATE TABLE search_content (node INTEGER, text_content TEXT);
"""


class Upstream:
    """A synthetic knowledge_v1.db the test edits between sync runs."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(QD_SCHEMA)

    def execute(self, sql, params=()):
        with sqlite3.connect(self.path) as conn:
            conn.execute(sql, params)

    def fact(self, qd_id, confidence=0.9, category="people", text=None):
        self.execute("""INSERT OR REPLACE INTO memories (id, memory_type, category, trigger_text, confidence,
            effective_confidence) VALUES (?, 'fact', ?, ?, ?, ?)""",
                     (qd_id, category, text or f"Synthetic fact number {qd_id}.", confidence, confidence))

    def decision(self, entity_id):
        self.execute("INSERT INTO entities (id, node, category, name) VALUES (?, NULL, 'Decision', ?)",
                     (entity_id, f"Synthetic decision {entity_id}"))


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    from src.capture import quick_desktop
    import src.ingest
    store = Upstream(tmp_path / "knowledge_v1.db")
    store.decision(99)  # a stable decision keeps qd://entity/ in scope
    monkeypatch.setattr(quick_desktop, "QD_DB_PATH", store.path)
    monkeypatch.setattr(src.ingest, "generate_embedding", lambda text: [0.1] * 1024)
    monkeypatch.setattr(src.ingest, "_discover_relationships", lambda *a: None)
    return store


def _rows(conn, url):
    """(parent status, parent retired?, [(chunk status, chunk retired?)]) for one identity."""
    with conn.cursor() as cur:
        cur.execute("""SELECT id, status, metadata ? 'capture_retired' FROM memories
                       WHERE source_url = %s AND parent_id IS NULL ORDER BY created_at, id""", (url,))
        parents = cur.fetchall()
        assert len(parents) == 1, "one parent per identity"
        pid, status, retired = parents[0]
        cur.execute("""SELECT status, metadata ? 'capture_retired' FROM memories WHERE parent_id = %s
                       ORDER BY created_at, id""", (pid,))
        return status, retired, cur.fetchall()


def test_dry_run_counts_rows_outside_the_selection_and_writes_nothing(test_conn, upstream):
    from src.capture import quick_desktop
    for qd_id in (1, 2, 3):
        upstream.fact(qd_id)
    upstream.decision(10)
    quick_desktop.capture()
    upstream.fact(2, confidence=0.3)          # below QD's own threshold now
    upstream.execute("DELETE FROM memories WHERE id = 3")  # deleted upstream
    preview = quick_desktop.capture(dry_run=True)
    assert preview["selection"] == {"retire": 2, "reactivate": 0, "dry_run": True}
    assert preview["quick_desktop_people"]["dry_run"] is True
    for qd_id in (1, 2, 3):
        status, retired, chunks = _rows(test_conn, f"qd://memory/{qd_id}")
        assert (status, retired) == ("active", False) and all(s == "active" for s, _ in chunks)


def test_rows_outside_the_selection_are_retired_with_their_chunks_and_restored_on_return(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.fact(2)
    quick_desktop.capture()
    upstream.fact(2, text="Synthetic fact number 2, edited upstream.")  # leaves one superseded history chunk
    quick_desktop.capture()
    assert [s for s, _ in _rows(test_conn, "qd://memory/2")[2]] == ["superseded", "active"]

    upstream.fact(2, confidence=0.3, text="Synthetic fact number 2, edited upstream.")
    result = quick_desktop.capture()
    assert result["selection"] == {"retire": 1, "reactivate": 0}
    assert _rows(test_conn, "qd://memory/2") == ("superseded", True, [("superseded", False), ("superseded", True)])
    assert _rows(test_conn, "qd://memory/1")[:2] == ("active", False)
    assert quick_desktop.capture()["selection"] == {"retire": 0, "reactivate": 0}, "idempotent"

    upstream.fact(2, confidence=0.8, text="Synthetic fact number 2, edited upstream.")
    result = quick_desktop.capture()
    assert result["selection"] == {"retire": 0, "reactivate": 1}
    # Only what retirement touched comes back; the older history chunk stays history.
    assert _rows(test_conn, "qd://memory/2") == ("active", False, [("superseded", False), ("active", False)])


def test_a_record_that_returns_with_new_content_has_exactly_one_active_chunk_set(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)  # keeps qd://memory/ non-empty while fact 5 is out
    upstream.fact(5)
    quick_desktop.capture()
    upstream.fact(5, confidence=0.2)
    quick_desktop.capture()
    upstream.fact(5, confidence=0.9, text="Synthetic fact number 5, rewritten while it was out.")
    assert quick_desktop.capture()["selection"]["reactivate"] == 1
    status, retired, chunks = _rows(test_conn, "qd://memory/5")
    assert (status, retired) == ("active", False)
    assert [s for s, _ in chunks] == ["superseded", "active"]


def test_a_relabelled_record_retires_its_old_label_row(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(7, category="anti-pattern")
    quick_desktop.capture()
    upstream.fact(7, category="preference")
    quick_desktop.capture()
    with test_conn.cursor() as cur:
        cur.execute("""SELECT source_type, status FROM memories WHERE source_url = 'qd://memory/7'
                       AND parent_id IS NULL ORDER BY source_type""")
        assert cur.fetchall() == [("quick_desktop_anti_pattern", "superseded"), ("quick_desktop_preference", "active")]


def test_an_empty_namespace_is_refused_not_emptied(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.decision(10)
    quick_desktop.capture()
    # Decisions not read this run: qd://entity/ is out of scope, not absent.
    assert quick_desktop.capture(decisions=False)["selection"] == {"retire": 0, "reactivate": 0}
    upstream.execute("DELETE FROM memories")
    result = quick_desktop.capture()
    assert result["selection"] == {"retire": 0, "reactivate": 0, "refused_prefixes": ["qd://memory/"]}
    assert _rows(test_conn, "qd://memory/1")[0] == "active"
    assert _rows(test_conn, "qd://entity/10")[0] == "active"


def test_rows_another_writer_superseded_are_never_reactivated(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    quick_desktop.capture()
    with test_conn.cursor() as cur:
        cur.execute("UPDATE memories SET status = 'superseded' WHERE source_url = 'qd://memory/1' AND parent_id IS NULL")
    assert quick_desktop.capture()["selection"] == {"retire": 0, "reactivate": 0}
    assert _rows(test_conn, "qd://memory/1")[:2] == ("superseded", False)


def test_other_channels_are_outside_the_reconciled_namespace(test_conn, upstream):
    from src import db
    from src.capture import quick_desktop
    import src.ingest
    src.ingest.ingest_content("Synthetic chat transcript.", "quick_desktop_chat", "qd-chat://s-1", parse_header=False)
    upstream.fact(1)
    quick_desktop.capture()
    with test_conn.cursor() as cur:
        cur.execute("SELECT status FROM memories WHERE source_url = 'qd-chat://s-1' AND parent_id IS NULL")
        assert cur.fetchone() == ("active",)
    with pytest.raises(ValueError):
        db.reconcile_source_selection((), {("quick_desktop_people", "qd://memory/1")})
