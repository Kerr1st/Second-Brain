"""Tests for src/skim/store.py — memory_skim persistence with a fake connection.

Same style as tests/test_dream_cycle_db.py: ``get_connection`` is patched with a
MagicMock connection/cursor, the SQL text and parameters are asserted. Synthetic
ids and records only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from src.skim import store

WINDOW = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
MODEL, PV = "us.amazon.nova-2-lite-v1:0", "skim-v1"


def _conn(fetchall=None, fetchone=None):
    """A fake connection whose cursor returns the given rows."""
    from tests.conftest import mock_conn
    conn, cur = mock_conn(rows=fetchall or (), first=fetchone)
    get_conn = MagicMock()
    get_conn.return_value.__enter__ = MagicMock(return_value=conn)
    get_conn.return_value.__exit__ = MagicMock(return_value=False)
    return get_conn, conn, cur


class TestArrivalsFilter:
    def test_filter_is_parent_level_not_superseded_and_not_dream_cycle(self):
        f = store.ARRIVALS_FILTER_SQL
        assert "parent_id IS NULL" in f
        assert "status IS DISTINCT FROM 'superseded'" in f
        assert "'dream-cycle' = ANY(COALESCE(tags, '{}'))" in f
        assert "metadata->>'dream_cycle'" in f

    def test_alias_prefixes_every_column(self):
        f = store.arrivals_filter_sql("m.")
        assert "m.parent_id" in f and "m.status" in f and "m.tags" in f and "m.metadata" in f
        assert " parent_id" not in f.replace("m.parent_id", "")

    def test_list_arrivals_selects_no_content(self):
        a, b = uuid4(), uuid4()
        get_conn, conn, cur = _conn(fetchall=[
            {"id": a, "title": "T1", "source_type": "slack", "created_at": WINDOW},
            {"id": b, "title": "T2", "source_type": None, "created_at": WINDOW},
        ])
        with patch("src.skim.store.get_connection", get_conn):
            rows = store.list_arrivals(WINDOW, limit=100)
        sql, params = cur.execute.call_args[0]
        assert "content" not in sql.lower()
        assert "created_at >= %s" in sql and store.ARRIVALS_FILTER_SQL in sql
        assert params == (WINDOW, 100)
        assert [r["id"] for r in rows] == [str(a), str(b)]
        conn.commit.assert_not_called()


class TestFetchDocuments:
    def test_returns_in_requested_order_and_skips_missing(self):
        a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
        get_conn, _, cur = _conn(fetchall=[
            {"id": b, "title": "B", "source_type": "email", "created_at": WINDOW, "content": "body b"},
            {"id": a, "title": "A", "source_type": "slack", "created_at": WINDOW, "content": "body a"},
        ])
        with patch("src.skim.store.get_connection", get_conn):
            docs = store.fetch_documents([a, c, b])
        sql, params = cur.execute.call_args[0]
        assert "content" in sql and "id = ANY(%s::uuid[])" in sql
        assert params == ([a, c, b],)
        assert [d["id"] for d in docs] == [a, b] and docs[0]["content"] == "body a"

    def test_empty_ids_make_no_query(self):
        get_conn, _, cur = _conn()
        with patch("src.skim.store.get_connection", get_conn):
            assert store.fetch_documents([]) == []
        cur.execute.assert_not_called()


class TestUpsertSkim:
    def test_upsert_writes_all_columns_and_commits(self):
        mid, rid, row = str(uuid4()), str(uuid4()), uuid4()
        get_conn, conn, cur = _conn(fetchone=(row,))
        skim = {"summary": "s", "people": [], "novelty": 1}
        with patch("src.skim.store.get_connection", get_conn):
            out = store.upsert_skim(mid, model=MODEL, prompt_version=PV, skim=skim, run_id=rid,
                                    input_tokens=2500, output_tokens=340, cost_usd=0.00091234567,
                                    truncated=True)
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO memory_skim" in sql
        assert "ON CONFLICT (memory_id, model, prompt_version) DO UPDATE" in sql
        assert "skim = EXCLUDED.skim" in sql and "created_at = now()" in sql
        assert params == (mid, rid, MODEL, PV, json.dumps(skim), None, 2500, 340, 0.000912, True)
        assert out == str(row)
        conn.commit.assert_called_once()

    def test_defaults_are_null_and_not_truncated(self):
        get_conn, _, cur = _conn(fetchone=(uuid4(),))
        with patch("src.skim.store.get_connection", get_conn):
            store.upsert_skim("m1", model=MODEL, prompt_version=PV, skim={})
        params = cur.execute.call_args[0][1]
        assert params == ("m1", None, MODEL, PV, "{}", None, None, None, None, False)


class TestFetchAndMissing:
    def test_missing_ids_keeps_order_drops_duplicates_and_known(self):
        a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
        get_conn, _, cur = _conn(fetchall=[(b,)])
        with patch("src.skim.store.get_connection", get_conn):
            missing = store.missing_ids([c, a, b, a], MODEL, PV)
        sql, params = cur.execute.call_args[0]
        assert "memory_id = ANY(%s::uuid[])" in sql
        assert params == ([c, a, b], MODEL, PV)
        assert missing == [c, a]

    def test_missing_ids_empty(self):
        get_conn, _, cur = _conn()
        with patch("src.skim.store.get_connection", get_conn):
            assert store.missing_ids([], MODEL, PV) == []
        cur.execute.assert_not_called()


class TestWindow:
    def test_fetch_window_records_joins_memories_with_the_arrivals_filter(self):
        a = uuid4()
        get_conn, _, cur = _conn(fetchall=[
            {"id": a, "title": "T", "source_type": "article", "created_at": WINDOW,
             "skim": {"summary": "s"}, "cluster_id": "c1", "truncated": False},
        ])
        with patch("src.skim.store.get_connection", get_conn):
            rows = store.fetch_window_records(WINDOW, MODEL, PV)
        sql, params = cur.execute.call_args[0]
        assert "JOIN memories m ON m.id = s.memory_id" in sql
        assert store.arrivals_filter_sql("m.") in sql
        assert "m.content" not in sql and "content" not in sql.replace("m.created_at", "")
        assert params == (WINDOW, MODEL, PV, 5000)
        assert rows == [{"id": str(a), "title": "T", "source_type": "article", "created_at": WINDOW,
                         "skim": {"summary": "s"}, "cluster_id": "c1", "truncated": False}]



class TestClusterStorage:
    def test_centroids_use_own_embedding_then_chunk_average_and_read_no_text(self):
        a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
        get_conn, conn, cur = _conn()
        cur.fetchall.side_effect = [
            [(a, "[0.1,0.2,0.3]")],                      # own embeddings: only a
            [(b, "[1, 0, 0]"), (c, None)],               # chunk averages for the rest; c has none
        ]
        with patch("src.skim.store.get_connection", get_conn):
            out = store.fetch_window_centroids([a, b, c])
        assert out == {a: [0.1, 0.2, 0.3], b: [1.0, 0.0, 0.0]}
        first, second = [call[0] for call in cur.execute.call_args_list]
        assert "embedding::text" in first[0] and first[1] == ([a, b, c],)
        assert "avg(embedding)::text" in second[0] and "GROUP BY parent_id" in second[0]
        assert second[1] == ([b, c],)                     # only the ids still missing
        for sql in (first[0], second[0]):
            assert "content" not in sql and "title" not in sql
        conn.commit.assert_not_called()

    def test_centroids_skip_the_second_query_when_all_found_and_handle_empty(self):
        a = str(uuid4())
        get_conn, _, cur = _conn(fetchall=[(a, "[0.5,0.5]")])
        with patch("src.skim.store.get_connection", get_conn):
            assert store.fetch_window_centroids([a]) == {a: [0.5, 0.5]}
        assert cur.execute.call_count == 1
        assert store.fetch_window_centroids([]) == {}

    def test_parse_vector(self):
        assert store._parse_vector("[1,2.5, -3]") == [1.0, 2.5, -3.0]
        assert store._parse_vector([1, 2]) == [1.0, 2.0]
        assert store._parse_vector(None) is None and store._parse_vector("nope") is None
        assert store._parse_vector("[a,b]") is None

    def test_set_cluster_ids_updates_by_key_and_commits(self):
        a, b = str(uuid4()), str(uuid4())
        get_conn, conn, cur = _conn()
        cur.rowcount = 2
        with patch("src.skim.store.get_connection", get_conn):
            n = store.set_cluster_ids({a: "c1", b: None}, MODEL, PV)
        sql, params = cur.execute.call_args[0]
        assert "UPDATE memory_skim" in sql and "s.model = %s AND s.prompt_version = %s" in sql
        assert params == ([a, b], ["c1", None], MODEL, PV)
        assert n == 2
        conn.commit.assert_called_once()

    def test_set_cluster_ids_empty_makes_no_query(self):
        get_conn, _, cur = _conn()
        with patch("src.skim.store.get_connection", get_conn):
            assert store.set_cluster_ids({}, MODEL, PV) == 0
        cur.execute.assert_not_called()
