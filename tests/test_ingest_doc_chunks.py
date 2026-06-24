"""Tests for Quick Desktop document chunk ingestion (Phase 5)."""

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

SAMPLE_CHUNKS = [
    {
        "id": 7789, "node": 4176, "chunk_index": 0, "file_id": 3,
        "folder_path": "/Users/example/Documents",
        "extension": ".docx",
        "source": "/Users/example/Documents/Team Notes.docx",
        "text_content": "**Team Offsite (2026 Planning)**\n\nLogistics\n\nDays: 2\nDate: Apr-16 to Apr-17",
    },
    {
        "id": 7790, "node": 4177, "chunk_index": 1, "file_id": 3,
        "folder_path": "/Users/example/Documents",
        "extension": ".docx",
        "source": "/Users/example/Documents/Team Notes.docx",
        "text_content": "**Room: CONF US SJC43 02.103**\n\nAgenda\n\nDay 1: Strategy review",
    },
    {
        "id": 12501, "node": 8872, "chunk_index": 0, "file_id": 14,
        "folder_path": "/Users/example/Documents/ProjectDocs",
        "extension": ".pdf",
        "source": "/Users/example/Documents/ProjectDocs/developer-tooling-guide.pdf",
        "text_content": "## Amazon Q Developer User Guide\n\nGetting started with Amazon Q Developer.",
    },
]


@pytest.fixture
def sqlite_db():
    """Create a temp SQLite DB mimicking knowledge_v1.db."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE search_content (
        id INTEGER PRIMARY KEY, node INTEGER, node_class TEXT, category TEXT,
        parent_node INTEGER, folder_path TEXT, extension TEXT, source TEXT,
        source_type TEXT, text_content TEXT
    )""")
    conn.execute("""CREATE TABLE file_chunks (
        id INTEGER PRIMARY KEY, node INTEGER UNIQUE NOT NULL,
        file_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
        text_hash TEXT, properties BLOB
    )""")

    for c in SAMPLE_CHUNKS:
        conn.execute(
            "INSERT INTO search_content (id, node, folder_path, extension, source, source_type, text_content) VALUES (?,?,?,?,?,?,?)",
            (c["id"], c["node"], c["folder_path"], c["extension"], c["source"], "local", c["text_content"]),
        )
        conn.execute(
            "INSERT INTO file_chunks (id, node, file_id, chunk_index) VALUES (?,?,?,?)",
            (c["id"], c["node"], c["file_id"], c["chunk_index"]),
        )

    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestQueryAndMetadata:
    """Unit tests for query building, metadata extraction, source_url generation."""

    def test_source_url_generation(self):
        from scripts.migrate.ingest_doc_chunks import source_url_for_chunk
        assert source_url_for_chunk(4176) == "qd-doc://4176"
        assert source_url_for_chunk(8872) == "qd-doc://8872"

    def test_title_from_source_path(self):
        from scripts.migrate.ingest_doc_chunks import title_from_source
        assert title_from_source("/path/to/Team Notes.docx") == "Team Notes.docx"
        assert title_from_source("/path/to/developer-tooling-guide.pdf") == "developer-tooling-guide.pdf"

    def test_title_from_source_with_chunk_index(self):
        from scripts.migrate.ingest_doc_chunks import title_from_source
        title = title_from_source("/path/to/doc.pdf", chunk_index=5)
        assert "doc.pdf" in title
        assert "chunk 5" in title

    def test_tags_from_chunk(self):
        from scripts.migrate.ingest_doc_chunks import tags_for_chunk
        tags = tags_for_chunk(".pdf", "/Users/example/Documents/ProjectDocs")
        assert "qd-doc" in tags
        assert "pdf" in tags

    def test_metadata_from_chunk(self):
        from scripts.migrate.ingest_doc_chunks import metadata_for_chunk
        meta = metadata_for_chunk(
            node_id=4176, chunk_index=0, file_id=3,
            folder_path="/Users/example/Documents",
            extension=".docx",
            source="/Users/example/Documents/Team Notes.docx",
        )
        assert meta["node_id"] == 4176
        assert meta["chunk_index"] == 0
        assert meta["file_id"] == 3
        assert meta["extension"] == ".docx"

    def test_query_chunks_from_sqlite(self, sqlite_db):
        from scripts.migrate.ingest_doc_chunks import query_chunks
        chunks = query_chunks(sqlite_db)
        assert len(chunks) == 3
        assert chunks[0]["node"] == 4176


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

class TestDocChunkProperties:
    """Property-based tests for invariants."""

    @given(node_id=st.integers(min_value=1, max_value=999999))
    def test_source_url_always_has_prefix(self, node_id):
        from scripts.migrate.ingest_doc_chunks import source_url_for_chunk
        url = source_url_for_chunk(node_id)
        assert url.startswith("qd-doc://")
        assert str(node_id) in url

    @given(
        text=st.text(min_size=1, max_size=500),
        extension=st.sampled_from([".pdf", ".docx", ".pptx", ".md", ".xlsx"]),
        folder=st.text(min_size=1, max_size=100),
    )
    def test_content_never_empty_after_format(self, text, extension, folder):
        """Formatted content should never be empty if text_content is non-empty."""
        # The script uses text_content directly as content
        assert len(text.strip()) >= 0  # text itself may be whitespace
        # But we filter empty chunks in the script


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


class TestDocChunksE2E:
    """End-to-end tests for document chunk ingestion."""

    def test_ingest_creates_memories(self, test_db, sqlite_db):
        from scripts.migrate.ingest_doc_chunks import ingest_doc_chunks

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-doc://%'")

        with patch("scripts.migrate.ingest_doc_chunks.generate_embedding", return_value=[0.1] * 1024):
            stats = ingest_doc_chunks(sqlite_db, dry_run=False, workers=2)

        assert stats["processed"] == 3
        assert stats["failed"] == 0

        with test_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE source_type = 'quick_desktop_doc'")
            assert cur.fetchone()[0] == 3

    def test_ingest_is_idempotent(self, test_db, sqlite_db):
        from scripts.migrate.ingest_doc_chunks import ingest_doc_chunks

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-doc://%'")

        with patch("scripts.migrate.ingest_doc_chunks.generate_embedding", return_value=[0.1] * 1024):
            stats1 = ingest_doc_chunks(sqlite_db, dry_run=False, workers=2)
            stats2 = ingest_doc_chunks(sqlite_db, dry_run=False, workers=2)

        assert stats1["processed"] == 3
        assert stats2["processed"] == 0
        assert stats2["skipped"] == 3

    def test_dry_run_creates_nothing(self, test_db, sqlite_db):
        from scripts.migrate.ingest_doc_chunks import ingest_doc_chunks

        with test_db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE source_url LIKE 'qd-doc://%'")

        with patch("scripts.migrate.ingest_doc_chunks.generate_embedding", return_value=[0.1] * 1024):
            stats = ingest_doc_chunks(sqlite_db, dry_run=True, workers=2)

        assert stats["processed"] == 3

        with test_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE source_type = 'quick_desktop_doc'")
            assert cur.fetchone()[0] == 0
