"""Oversized content is stored without silent loss and never over-feeds the embedder.

R1 truncated every embedding input at 25k chars silently. R3 removed the silent
truncation but then let 25-50k strings reach the provider whole (over the 8,192
token limit) and raised above 50k, so an oversized hourly item failed every run
instead of being stored. These tests pin the repaired contract:

* every string handed to the provider is at most ``EMBEDDING_SAFE_CHARS`` long;
* chunked ingestion stores the complete parent and embeds bounded chunks;
* leaf writers (one row, one vector: ``memory_create``/``memory_update``) embed
  a bounded prefix, keep the full content and record ``embedding_truncated`` /
  ``original_chars`` in metadata; the hourly QD writers moved onto
  ``ingest_content`` in R4 and store oversized items as bounded chunks;
* the truncation is logged.
"""
import json
import logging
import os
import sqlite3
import tempfile
from unittest.mock import Mock, patch

import pytest

from src import db
from src import embeddings
from src.embeddings import EMBEDDING_MAX_CHARS, EMBEDDING_SAFE_CHARS, embed_leaf
from src.ingest import ingest_content


def _vector(_text=""):
    return [1.0] + [0.0] * 1023


class _BoundedEmbedder:
    """Records every provider input and refuses anything over the safe bound."""

    def __init__(self):
        self.inputs = []

    def __call__(self, text):
        assert isinstance(text, str) and text.strip()
        assert len(text) <= EMBEDDING_SAFE_CHARS, f"provider would receive {len(text)} chars"
        self.inputs.append(text)
        return _vector()


OVERSIZED = "line of text that keeps going\n" * 2_000  # 60,000 chars > provider hard limit


def test_safe_bound_is_inside_the_documented_provider_limit():
    """Titan v2: 8,192 tokens or 50,000 chars; the safe bound leaves token headroom."""
    assert EMBEDDING_MAX_CHARS == 50_000
    assert 0 < EMBEDDING_SAFE_CHARS <= 25_000
    assert "8,192 tokens" in embeddings.__doc__ or "8,192 tokens" in open(embeddings.__file__).read()


def test_embed_leaf_passes_short_text_whole_without_metadata():
    embed = Mock(side_effect=_vector)
    vector, meta = embed_leaf("short text", embed=embed)
    assert vector == _vector() and meta == {}
    embed.assert_called_once_with("short text")


def test_embed_leaf_bounds_the_prefix_records_and_logs_the_truncation(caplog):
    embed = _BoundedEmbedder()
    with caplog.at_level(logging.WARNING, logger="src.embeddings"):
        vector, meta = embed_leaf(OVERSIZED, embed=embed)
    assert vector == _vector()
    assert embed.inputs == [OVERSIZED[:EMBEDDING_SAFE_CHARS]]
    assert meta == {"embedding_truncated": True, "original_chars": len(OVERSIZED),
                    "embedded_chars": EMBEDDING_SAFE_CHARS}
    assert any("embedding_input_truncated" in r.getMessage() and str(len(OVERSIZED)) in r.getMessage()
               for r in caplog.records)


def test_embed_leaf_never_exceeds_the_provider_hard_limit():
    with pytest.raises(ValueError):
        embed_leaf("x", max_chars=EMBEDDING_MAX_CHARS + 1, embed=_vector)
    with pytest.raises(ValueError):
        embed_leaf("   ", embed=_vector)


def test_embed_leaf_defaults_to_the_provider_call(monkeypatch):
    sent = {}
    monkeypatch.setattr(embeddings, "generate_embedding", lambda text: sent.setdefault("text", text) and _vector())
    embed_leaf(OVERSIZED)
    assert len(sent["text"]) == EMBEDDING_SAFE_CHARS


def test_ingest_content_stores_the_whole_oversized_parent_with_bounded_embedded_chunks(test_db, clean_tables, monkeypatch):
    import src.ingest as ingest
    embed = _BoundedEmbedder()
    monkeypatch.setattr(ingest, "generate_embedding", embed)
    monkeypatch.setattr(ingest, "_discover_relationships", lambda *a: None)
    doc = "# Oversized\nSource: test://oversized\n---\n" + OVERSIZED
    parent_id = ingest_content(doc, "quick_desktop_chat", "test://oversized")
    assert parent_id
    parent = db.get_memory(parent_id)
    assert parent["content"] == OVERSIZED.strip()
    children = [m for m in db.list_memories() if m["parent_id"]]
    assert len(children) == len(embed.inputs) >= 2
    assert all(len(c["content"]) <= 4000 for c in children)
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FILTER (WHERE embedding IS NULL) FROM memories WHERE parent_id = %s", (parent_id,))
        assert cur.fetchone()[0] == 0
    assert "".join(c["content"] for c in sorted(children, key=lambda c: c["metadata"]["chunk_index"])) == parent["content"]
    assert all(len(text) <= EMBEDDING_SAFE_CHARS for text in embed.inputs)


def _bounded_ingest(monkeypatch):
    import src.ingest as ingest
    embed = _BoundedEmbedder()
    monkeypatch.setattr(ingest, "generate_embedding", embed)
    monkeypatch.setattr(ingest, "_discover_relationships", lambda *a: None)
    return embed


def test_eventlog_oversized_event_is_stored_whole_with_bounded_embedded_chunks(test_conn, monkeypatch):
    """R4: the hourly feed writer goes through ``ingest_content`` (R3 embeds a
    bounded prefix per leaf; R4 stores the complete event and embeds bounded
    chunks). Either way an oversized event is stored, never ``Failed`` hourly."""
    from src.capture import qd_eventlog as mod
    event = {"id": "test-evt_oversize", "timestamp": "2026-10-05T12:00:00", "source": "agent:session-recorder",
             "event_type": "session_event", "summary": "Very long session",
             "details": {"full_message": OVERSIZED}}
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(event) + "\n")
        path = f.name
    try:
        embed = _bounded_ingest(monkeypatch)
        with patch.object(mod, "EVENTLOG_PATH", path):
            stats = mod.ingest_eventlog(dry_run=False)
    finally:
        os.unlink(path)
    assert stats == {"processed": 1, "skipped": 0, "failed": 0}
    assert len(embed.inputs) >= 2 and all(len(text) <= 4000 for text in embed.inputs)
    with test_conn.cursor() as cur:
        cur.execute("SELECT id, length(content), metadata FROM memories WHERE source_url = 'qd-feed://test-evt_oversize' AND parent_id IS NULL")
        parent_id, length, metadata = cur.fetchone()
        cur.execute("SELECT count(*), count(*) FILTER (WHERE embedding IS NULL) FROM memories WHERE parent_id = %s", (parent_id,))
        children, unembedded = cur.fetchone()
    assert length > EMBEDDING_MAX_CHARS
    assert children == len(embed.inputs) and unembedded == 0
    assert metadata["event_type"] == "session_event"


def test_eventlog_normal_event_is_stored_once(test_conn, monkeypatch):
    from src.capture import qd_eventlog as mod
    event = {"id": "test-evt_small", "timestamp": "2026-10-05T12:00:00", "source": "agent:x",
             "event_type": "notification", "summary": "Small", "details": {"full_message": "hello"}}
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(event) + "\n")
        path = f.name
    try:
        embed = _bounded_ingest(monkeypatch)
        with patch.object(mod, "EVENTLOG_PATH", path):
            assert mod.ingest_eventlog(dry_run=False)["processed"] == 1
    finally:
        os.unlink(path)
    assert len(embed.inputs) == 1
    with test_conn.cursor() as cur:
        cur.execute("SELECT metadata FROM memories WHERE source_url = 'qd-feed://test-evt_small' AND parent_id IS NULL")
        assert "embedding_truncated" not in cur.fetchone()[0]


def test_doc_chunk_oversized_text_is_stored_whole_with_bounded_embedded_chunks(test_conn, monkeypatch):
    from src.capture import qd_docs as mod
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE search_content (id INTEGER PRIMARY KEY, node INTEGER, folder_path TEXT, extension TEXT, source TEXT, source_type TEXT, text_content TEXT)")
    conn.execute("CREATE TABLE file_chunks (id INTEGER PRIMARY KEY, node INTEGER UNIQUE NOT NULL, file_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL)")
    conn.execute("INSERT INTO search_content VALUES (1, -901, '/Users/x/Documents', '.md', '/Users/x/Documents/big.md', 'local', ?)", (OVERSIZED,))
    conn.execute("INSERT INTO file_chunks VALUES (1, -901, 7, 0)")
    conn.commit(); conn.close()
    try:
        embed = _bounded_ingest(monkeypatch)
        stats = mod.ingest_doc_chunks(path, dry_run=False, workers=1)
    finally:
        os.unlink(path)
    assert stats == {"processed": 1, "skipped": 0, "failed": 0}
    assert len(embed.inputs) >= 2 and all(len(text) <= EMBEDDING_SAFE_CHARS for text in embed.inputs)
    with test_conn.cursor() as cur:
        cur.execute("SELECT length(content), metadata FROM memories WHERE source_url = 'qd-doc://-901' AND parent_id IS NULL")
        length, metadata = cur.fetchone()
    assert length == len(OVERSIZED)
    assert metadata["node_id"] == -901


def test_memory_create_and_update_bound_the_embedding_and_record_it(test_db, clean_tables):
    from src import mcp_server as server
    embed = _BoundedEmbedder()
    with patch.object(server, "generate_embedding", embed):
        result = server.memory_create(type="source", title="Big", content=OVERSIZED, source_type="manual")
    assert result.startswith("Created memory ")
    mid = result.split()[2]
    row = db.get_memory(mid)
    assert row["content"] == OVERSIZED and row["embedding"] is not None
    assert row["metadata"]["embedding_truncated"] is True and row["metadata"]["original_chars"] == len(OVERSIZED)
    assert len(embed.inputs[-1]) == EMBEDDING_SAFE_CHARS
    # Shrinking the content clears the stale truncation record.
    with patch.object(server, "generate_embedding", embed):
        assert server.memory_update(mid, content="now short").startswith("Updated")
    row = db.get_memory(mid)
    assert "embedding_truncated" not in row["metadata"] and row["metadata"]["depth_score"] is not None
