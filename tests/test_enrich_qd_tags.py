"""Tests for Quick Desktop memory tag and domain enrichment.

Tests the enrichment of existing QD memories with their tags and domains
from the memory_tags and memory_domains tables in knowledge_v1.db.
"""

import json
import os
import sqlite3
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from hypothesis import given, strategies as st, assume

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def qd_db_with_tags():
    """Create a QD-like SQLite DB with memories, tags, and domains."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, memory_type TEXT, category TEXT,
            name TEXT, trigger_text TEXT, confidence REAL DEFAULT 0.5,
            effective_confidence REAL DEFAULT 0.5, retrieval_count INTEGER DEFAULT 0,
            source TEXT DEFAULT 'trace'
        );
        CREATE TABLE memory_tags (
            id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL,
            tag TEXT NOT NULL, UNIQUE(memory_id, tag),
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        );
        CREATE TABLE memory_domains (
            id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL,
            domain TEXT NOT NULL, UNIQUE(memory_id, domain),
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        );
    """)

    # Insert test memories
    conn.execute("INSERT INTO memories VALUES (1,'fact','people','Alice','Alice is an engineer',0.9,0.9,5,'trace')")
    conn.execute("INSERT INTO memories VALUES (2,'fact','terminology','RAG','RAG is retrieval augmented generation',0.8,0.8,3,'trace')")
    conn.execute("INSERT INTO memories VALUES (3,'procedure',NULL,'Deploy','How to deploy the app',0.7,0.7,1,'trace')")
    conn.execute("INSERT INTO memories VALUES (4,'fact','tool-strategy','MCP','MCP server pattern',0.6,0.6,0,'trace')")

    # Insert tags (including leading spaces like real data has)
    conn.executemany("INSERT INTO memory_tags (memory_id, tag) VALUES (?, ?)", [
        (1, "people"), (1, " css-team"), (1, " q-dev"),
        (2, "knowledge-graph"), (2, " ai"), (2, " rag"),
        (3, "deployment"), (3, " infrastructure"),
        (4, "mcp"), (4, " q-dev"), (4, " tools"),
    ])

    # Insert domains
    conn.executemany("INSERT INTO memory_domains (memory_id, domain) VALUES (?, ?)", [
        (1, "people"), (1, "css"),
        (2, "knowledge-graph"),
        (4, "agent_management"), (4, "tools"),
    ])

    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestFetchTagsAndDomains:
    """Unit tests for fetching tags and domains from QD database."""

    def test_fetch_tags_for_memory(self, qd_db_with_tags):
        from scripts.migrate.enrich_qd_tags import fetch_tags_for_memories
        conn = sqlite3.connect(qd_db_with_tags)
        conn.row_factory = sqlite3.Row
        result = fetch_tags_for_memories(conn)
        assert 1 in result
        assert "people" in result[1]
        assert "css-team" in result[1]  # leading space should be stripped
        assert "q-dev" in result[1]
        conn.close()

    def test_fetch_domains_for_memory(self, qd_db_with_tags):
        from scripts.migrate.enrich_qd_tags import fetch_domains_for_memories
        conn = sqlite3.connect(qd_db_with_tags)
        conn.row_factory = sqlite3.Row
        result = fetch_domains_for_memories(conn)
        assert 1 in result
        assert "people" in result[1]
        assert "css" in result[1]
        assert 3 not in result  # memory 3 has no domains
        conn.close()

    def test_tags_are_stripped(self, qd_db_with_tags):
        from scripts.migrate.enrich_qd_tags import fetch_tags_for_memories
        conn = sqlite3.connect(qd_db_with_tags)
        conn.row_factory = sqlite3.Row
        result = fetch_tags_for_memories(conn)
        # All tags should be stripped of whitespace
        for mem_id, tags in result.items():
            for tag in tags:
                assert tag == tag.strip(), f"Tag '{tag}' not stripped for memory {mem_id}"
        conn.close()

    def test_empty_db_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE memory_tags (id INTEGER PRIMARY KEY, memory_id INTEGER, tag TEXT)")
        conn.execute("CREATE TABLE memory_domains (id INTEGER PRIMARY KEY, memory_id INTEGER, domain TEXT)")
        conn.commit()

        from scripts.migrate.enrich_qd_tags import fetch_tags_for_memories, fetch_domains_for_memories
        assert fetch_tags_for_memories(conn) == {}
        assert fetch_domains_for_memories(conn) == {}
        conn.close()
        os.unlink(path)


class TestMergeTags:
    """Unit tests for the tag merging logic."""

    def test_merge_new_tags_with_existing(self):
        from scripts.migrate.enrich_qd_tags import merge_tags
        existing = ["qd:people", "qd_type:fact"]
        new_tags = ["people", "css-team", "q-dev"]
        result = merge_tags(existing, new_tags)
        assert "qd:people" in result  # existing preserved
        assert "qd_type:fact" in result  # existing preserved
        assert "people" in result
        assert "css-team" in result
        assert "q-dev" in result

    def test_merge_deduplicates(self):
        from scripts.migrate.enrich_qd_tags import merge_tags
        existing = ["qd:people", "people"]
        new_tags = ["people", "css-team"]
        result = merge_tags(existing, new_tags)
        assert result.count("people") == 1

    def test_merge_preserves_order_existing_first(self):
        from scripts.migrate.enrich_qd_tags import merge_tags
        existing = ["qd:people", "qd_type:fact"]
        new_tags = ["z-tag", "a-tag"]
        result = merge_tags(existing, new_tags)
        # Existing tags come first
        assert result[0] == "qd:people"
        assert result[1] == "qd_type:fact"

    def test_merge_empty_new_tags(self):
        from scripts.migrate.enrich_qd_tags import merge_tags
        existing = ["qd:people"]
        result = merge_tags(existing, [])
        assert result == ["qd:people"]

    def test_merge_empty_existing(self):
        from scripts.migrate.enrich_qd_tags import merge_tags
        result = merge_tags([], ["new-tag"])
        assert result == ["new-tag"]


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

class TestMergeTagsProperties:
    """Property-based tests for tag merging invariants."""

    @given(
        existing=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=10),
        new_tags=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=20),
    )
    def test_merge_never_loses_existing_tags(self, existing, new_tags):
        from scripts.migrate.enrich_qd_tags import merge_tags
        result = merge_tags(existing, new_tags)
        for tag in existing:
            assert tag in result

    @given(
        existing=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=10),
        new_tags=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=20),
    )
    def test_merge_result_has_no_duplicates(self, existing, new_tags):
        from scripts.migrate.enrich_qd_tags import merge_tags
        result = merge_tags(existing, new_tags)
        assert len(result) == len(set(result))

    @given(
        existing=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=10),
        new_tags=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=20),
    )
    def test_merge_result_size_bounded(self, existing, new_tags):
        from scripts.migrate.enrich_qd_tags import merge_tags
        result = merge_tags(existing, new_tags)
        # Result can't be larger than union of both
        assert len(result) <= len(set(existing) | set(new_tags))

    @given(
        existing=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=10),
        new_tags=st.lists(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "P"))), max_size=20),
    )
    def test_merge_is_superset_of_existing(self, existing, new_tags):
        from scripts.migrate.enrich_qd_tags import merge_tags
        result = merge_tags(existing, new_tags)
        assert set(existing).issubset(set(result))


# ---------------------------------------------------------------------------
# Integration / E2E Tests (require running PostgreSQL)
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    """Ensure test database exists with schema. Session-scoped would be better
    but keeping function-scoped for isolation."""
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


@pytest.fixture
def seeded_memories(test_db):
    """Insert QD-style memories into test DB and return their IDs."""
    with test_db.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd://memory/test_%'")
        ids = []
        for i in range(1, 4):
            cur.execute("""
                INSERT INTO memories (type, title, content, tags, source_url, source_type, metadata)
                VALUES ('source', %s, %s, %s, %s, 'quick_desktop', '{}')
                RETURNING id
            """, (
                f"Test QD Memory {i}",
                f"Content for test memory {i}",
                [f"qd:test", f"qd_type:fact"],
                f"qd://memory/test_{i}",
            ))
            ids.append((str(cur.fetchone()[0]), i))
    return ids


class TestEnrichmentE2E:
    """End-to-end tests that verify enrichment against real PostgreSQL."""

    def test_enrichment_adds_tags_to_existing_memories(self, test_db, seeded_memories, qd_db_with_tags):
        from scripts.migrate.enrich_qd_tags import enrich_memories

        # Map test QD IDs to our seeded memory source_urls
        qd_to_sb = {i: sb_id for sb_id, i in seeded_memories}

        with patch("scripts.migrate.enrich_qd_tags.QD_DB_PATH", qd_db_with_tags):
            with patch("scripts.migrate.enrich_qd_tags.get_qd_memory_id_mapping") as mock_mapping:
                mock_mapping.return_value = qd_to_sb
                stats = enrich_memories(dry_run=False)

        assert stats["tags_enriched"] > 0

        # Verify tags were actually added
        with test_db.cursor() as cur:
            sb_id = seeded_memories[0][0]
            cur.execute("SELECT tags FROM memories WHERE id = %s", (sb_id,))
            tags = cur.fetchone()[0]
            assert "people" in tags or "css-team" in tags

    def test_enrichment_dry_run_changes_nothing(self, test_db, seeded_memories, qd_db_with_tags):
        from scripts.migrate.enrich_qd_tags import enrich_memories

        qd_to_sb = {i: sb_id for sb_id, i in seeded_memories}

        # Get tags before
        with test_db.cursor() as cur:
            sb_id = seeded_memories[0][0]
            cur.execute("SELECT tags FROM memories WHERE id = %s", (sb_id,))
            tags_before = cur.fetchone()[0]

        with patch("scripts.migrate.enrich_qd_tags.QD_DB_PATH", qd_db_with_tags):
            with patch("scripts.migrate.enrich_qd_tags.get_qd_memory_id_mapping") as mock_mapping:
                mock_mapping.return_value = qd_to_sb
                stats = enrich_memories(dry_run=True)

        # Tags unchanged
        with test_db.cursor() as cur:
            cur.execute("SELECT tags FROM memories WHERE id = %s", (sb_id,))
            tags_after = cur.fetchone()[0]
        assert tags_before == tags_after

    def test_enrichment_is_idempotent(self, test_db, seeded_memories, qd_db_with_tags):
        from scripts.migrate.enrich_qd_tags import enrich_memories

        qd_to_sb = {i: sb_id for sb_id, i in seeded_memories}

        with patch("scripts.migrate.enrich_qd_tags.QD_DB_PATH", qd_db_with_tags):
            with patch("scripts.migrate.enrich_qd_tags.get_qd_memory_id_mapping") as mock_mapping:
                mock_mapping.return_value = qd_to_sb
                stats1 = enrich_memories(dry_run=False)
                stats2 = enrich_memories(dry_run=False)

        # Second run should find nothing new to enrich
        assert stats2["tags_enriched"] == 0
        assert stats2["domains_enriched"] == 0
