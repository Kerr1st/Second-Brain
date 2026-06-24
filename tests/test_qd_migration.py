"""Tests for Quick Desktop migration script."""

import json
import os
import sqlite3
import tempfile
import pytest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate.migrate_quick_desktop import (
    map_memory, map_decision, fetch_memories, fetch_kg_decisions,
    CATEGORY_MAP, PROCEDURE_MAP, load_sync_state, save_sync_state,
)


@pytest.fixture
def qd_db():
    """Create a minimal QD-like SQLite DB for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, node_id TEXT, node_class TEXT, category TEXT,
            source_type TEXT, source TEXT, parent_node INTEGER, created_at REAL, updated_at REAL);
        CREATE TABLE memories (id INTEGER PRIMARY KEY, node INTEGER, memory_type TEXT NOT NULL,
            category TEXT, name TEXT, provenance_type TEXT, source TEXT DEFAULT 'global',
            session_id TEXT, trigger_text TEXT NOT NULL, alpha REAL DEFAULT 5.0, beta REAL DEFAULT 5.0,
            confidence REAL DEFAULT 0.5, effective_confidence REAL DEFAULT 0.5,
            retrieval_count INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0, last_accessed_at REAL, global_behavior INTEGER DEFAULT 0,
            remote_record_id TEXT, properties BLOB);
        CREATE TABLE entities (id INTEGER PRIMARY KEY, node INTEGER, category TEXT NOT NULL,
            name TEXT NOT NULL, properties BLOB);
        CREATE TABLE search_content (node INTEGER, node_class TEXT, category TEXT,
            parent_node INTEGER, folder_path TEXT, extension TEXT, source TEXT,
            source_type TEXT, text_content TEXT);
    """)

    # Insert test nodes
    conn.execute("INSERT INTO nodes VALUES (1,'mem:1','memory','people',NULL,NULL,NULL,0,0)")
    conn.execute("INSERT INTO nodes VALUES (2,'mem:2','memory','terminology',NULL,NULL,NULL,0,0)")
    conn.execute("INSERT INTO nodes VALUES (3,'mem:3','memory',NULL,NULL,NULL,NULL,0,0)")
    conn.execute("INSERT INTO nodes VALUES (10,'ent:10','entity','Decision',NULL,NULL,NULL,0,0)")

    # Insert test memories
    conn.execute("""INSERT INTO memories (id, node, memory_type, category, name, trigger_text,
        confidence, effective_confidence, retrieval_count, source)
        VALUES (1, 1, 'fact', 'people', NULL,
        'Gyan Singh is Kerr''s manager at AWS CSS.', 0.9, 0.9, 20, 'trace')""")
    conn.execute("""INSERT INTO memories (id, node, memory_type, category, name, trigger_text,
        confidence, effective_confidence, retrieval_count, source)
        VALUES (2, 2, 'fact', 'terminology', NULL,
        'Kiro CLI 2.0 has PAT support for headless auth.', 0.85, 0.85, 5, 'trace')""")
    conn.execute("""INSERT INTO memories (id, node, memory_type, category, name,
        trigger_text, confidence, effective_confidence, retrieval_count, source)
        VALUES (3, 3, 'procedure', NULL, 'Set up monitoring agent',
        'To set up a recurring monitoring agent, use the agents.json config.', 0.7, 0.7, 3, 'trace')""")

    # Low confidence memory (should be filtered)
    conn.execute("INSERT INTO nodes VALUES (4,'mem:4','memory',NULL,NULL,NULL,NULL,0,0)")
    conn.execute("""INSERT INTO memories (id, node, memory_type, category, name, trigger_text,
        confidence, effective_confidence, retrieval_count, source)
        VALUES (4, 4, 'fact', 'people', NULL, 'Low confidence fact', 0.3, 0.3, 0, 'trace')""")

    # Insert test decision entity
    conn.execute("INSERT INTO entities VALUES (10, 10, 'Decision', 'Acme Auth Priority', NULL)")
    conn.execute("""INSERT INTO search_content VALUES (10, 'entity', 'Decision', NULL, NULL, NULL,
        NULL, NULL, 'Acme auth was deprioritized in favor of firewall work.')""")

    conn.commit()
    yield conn
    conn.close()
    os.unlink(path)


class TestCategoryMapping:
    def test_people_maps_to_source(self):
        assert CATEGORY_MAP["people"] == ("source", "quick_desktop_people")

    def test_terminology_maps_to_research(self):
        assert CATEGORY_MAP["terminology"] == ("research", "quick_desktop_terminology")

    def test_tool_strategy_maps_to_insight(self):
        assert CATEGORY_MAP["tool-strategy"] == ("insight", "quick_desktop_tool_strategy")

    def test_procedure_maps_to_insight(self):
        assert PROCEDURE_MAP == ("insight", "quick_desktop_procedure")


class TestMapMemory:
    def test_people_fact(self, qd_db):
        rows = fetch_memories(qd_db, min_confidence=0.5, since_id=0)
        mapped = map_memory(rows[0])
        assert mapped["type"] == "source"
        assert mapped["source_type"] == "quick_desktop_people"
        assert mapped["source_url"] == "qd://memory/1"
        assert mapped["confidence"] == 0.9
        assert "qd:people" in mapped["tags"]
        assert mapped["mem_class"] == "semantic"
        assert "Gyan Singh" in mapped["content"]

    def test_terminology_fact(self, qd_db):
        rows = fetch_memories(qd_db, min_confidence=0.5, since_id=0)
        mapped = map_memory(rows[1])
        assert mapped["type"] == "research"
        assert mapped["source_type"] == "quick_desktop_terminology"

    def test_procedure(self, qd_db):
        rows = fetch_memories(qd_db, min_confidence=0.5, since_id=0)
        mapped = map_memory(rows[2])
        assert mapped["type"] == "insight"
        assert mapped["source_type"] == "quick_desktop_procedure"
        assert mapped["title"] == "Set up monitoring agent"  # uses name field
        assert mapped["mem_class"] == "procedural"

    def test_confidence_filter(self, qd_db):
        rows = fetch_memories(qd_db, min_confidence=0.5, since_id=0)
        ids = [r["id"] for r in rows]
        assert 4 not in ids  # low confidence filtered out

    def test_since_id_filter(self, qd_db):
        rows = fetch_memories(qd_db, min_confidence=0.5, since_id=2)
        assert len(rows) == 1
        assert rows[0]["id"] == 3


class TestMapDecision:
    def test_decision_entity(self, qd_db):
        rows = fetch_kg_decisions(qd_db, since_id=0)
        mapped = map_decision(rows[0])
        assert mapped["type"] == "decision"
        assert mapped["title"] == "Acme Auth Priority"
        assert mapped["source_url"] == "qd://entity/10"
        assert "Acme auth" in mapped["content"]
        assert mapped["confidence"] == 0.8

    def test_since_id_filter(self, qd_db):
        rows = fetch_kg_decisions(qd_db, since_id=10)
        assert len(rows) == 0


class TestSyncState:
    def test_roundtrip(self, tmp_path):
        import scripts.migrate.migrate_quick_desktop as mod
        orig_path = mod.SYNC_STATE_PATH
        mod.SYNC_STATE_PATH = str(tmp_path / "state.json")
        try:
            state = load_sync_state()
            assert state["last_memory_id"] == 0

            state["last_memory_id"] = 42
            state["last_entity_id"] = 99
            save_sync_state(state)

            reloaded = load_sync_state()
            assert reloaded["last_memory_id"] == 42
            assert reloaded["last_entity_id"] == 99
            assert reloaded["last_sync"] is not None
        finally:
            mod.SYNC_STATE_PATH = orig_path
