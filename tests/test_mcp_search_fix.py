"""Regressions for the 2026-10-29 MCP findings M-4, M-5 and M-8.

M-5: the lexical OR pass must leave out terms that are in most rows (the
     full-corpus OR matched 76 % of memories and took ~3 s to rank).
M-8: memory_search returns one result per parent on both registries;
     memory_list leaves out a chunk whose parent is on the same page.
M-4: booleans are rejected for integer parameters over the protocol, in each
     tool's documented error shape.
"""
import asyncio
import json
from unittest.mock import patch

from psycopg2.extras import RealDictCursor

from tests.conftest import _deterministic_embedding, owner_connect
import src.db as _db
from src.retrieval import fts_passes


def _call(server, name, arguments):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool(name, arguments)

    result = asyncio.run(go())
    return result, " ".join(c.text for c in result.content if hasattr(c, "text"))


def _analyze_memories():
    conn = owner_connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ANALYZE memories")
    finally:
        conn.close()


# --------------------------------------------------------------------------- M-5

class TestLexicalOrPassSkipsCommonTerms:
    def test_fts_passes_drops_common_terms_from_the_or_pass_only(self):
        assert fts_passes(["zebra", "widget", "quasar"], {"widget"}) == [
            "zebra & widget & quasar", "zebra | quasar"]
        assert fts_passes(["zebra", "widget"]) == ["zebra & widget", "zebra | widget"]
        assert fts_passes(["widget", "gadget"], {"widget", "gadget"}) == ["widget & gadget"]
        assert fts_passes(["widget"], {"widget"}) == ["widget"]

    # ANALYZE statistics outlive the per-test TRUNCATE, so these rows use lexemes no
    # other test searches for.
    def test_or_pass_does_not_rank_rows_matching_only_a_common_term(self, test_db, clean_tables):
        from src.db.search_queries import common_terms, fetch_candidates
        common = [_db.create_memory(type="source", title="flurblex", content=f"flurblex gronkle {i}",
                                    source_type="article", source_url=f"a://{i}") for i in range(40)]
        rare = _db.create_memory(type="source", title="xyzzyzebra", content="xyzzyzebra",
                                 source_type="article", source_url="a://zebra")
        _analyze_memories()
        with _db.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert common_terms(cur, ["xyzzyzebra", "flurblex", "quasarix"]) == {"flurblex"}
        query = "xyzzyzebra flurblex quasarix"
        _, fts, _ = fetch_candidates(query, _deterministic_embedding(query), 20)
        assert set(fts) == {str(rare)}
        assert not set(fts) & {str(c) for c in common}

    def test_terms_without_statistics_are_not_pruned(self, test_db, clean_tables):
        from src.db.search_queries import common_terms
        _db.create_memory(type="source", title="w", content="plinthwort", source_type="article", source_url="a://w")
        with _db.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert common_terms(cur, ["plinthwort", "snorvel"]) == set()


# --------------------------------------------------------------------------- M-8

def _chunked_doc(title, text, n):
    parent = _db.create_memory(type="source", title=title, content=text * 3,
                               source_type="article", source_url=f"a://{title}")
    chunks = [_db.create_memory(type="source", title=f"{title} (chunk {i + 1}/{n})", content=f"{text} part {i}",
                                source_type="article", source_url=f"a://{title}", parent_id=parent,
                                metadata={"chunk_index": i},
                                embedding=str(_deterministic_embedding(f"{text} part {i}")))
              for i in range(n)]
    return parent, chunks


class TestSearchCollapsesChunksPerParent:
    def _seed(self):
        parent, chunks = _chunked_doc("Rate limits", "token bucket rate limiting for the gateway", 4)
        others = [_db.create_memory(type="insight", title=f"Other {i}", content=f"rate limiting note {i}",
                                    embedding=str(_deterministic_embedding(f"rate limiting note {i}")))
                  for i in range(3)]
        return parent, chunks, others

    def _check(self, results, parent, chunks, others):
        keys = [r["parent_id"] or r["id"] for r in results]
        assert len(keys) == len(set(keys)), f"two results share a parent: {keys}"
        assert len(results) == 4  # the doc once, then the next distinct parents fill the limit
        doc = [r for r in results if r["parent_id"] == str(parent)]
        assert len(doc) == 1 and doc[0]["id"] in {str(c) for c in chunks}
        assert doc[0]["other_matching_chunks"] == 3
        assert {r["id"] for r in results} - {doc[0]["id"]} == {str(o) for o in others}

    def test_full_server(self, test_db, clean_tables):
        from src.mcp_server import memory_search
        seeded = self._seed()
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            out = memory_search(query="token bucket rate limiting", limit=4)
        self._check(out["results"], *seeded)

    def test_read_only_server(self, test_db, clean_tables):
        from src.mcp_readonly import memory_search
        seeded = self._seed()
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            out = memory_search(query="token bucket rate limiting", limit=4)
        self._check(out["results"], *seeded)


class TestListCollapsesChunksUnderTheirParent:
    def test_chunk_is_left_out_when_its_parent_is_on_the_page(self, test_db, clean_tables):
        from src.mcp_server import memory_list
        older = _db.create_memory(type="source", title="Older doc", content="c", source_type="slack",
                                  source_url="slack://older")
        with _db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = now() - interval '1 hour' WHERE id = %s", (older,))
            conn.commit()
        parent, chunks = _chunked_doc("Slack day", "conversation", 2)
        with _db.get_connection() as conn:  # ingest writes a document and its chunks in one transaction
            with conn.cursor() as cur:
                cur.execute("UPDATE memories m SET created_at = p.created_at FROM memories p "
                            "WHERE m.parent_id = p.id AND p.id = %s", (parent,))
            conn.commit()
        out = memory_list(source_type="article", limit=5) + memory_list(source_type="slack", limit=5)
        ids = [r["id"] for r in out]
        assert str(parent) in ids and not {str(c) for c in chunks} & set(ids)
        page = memory_list(limit=2)
        assert [r["id"] for r in page] == [str(parent), str(older)]  # refilled after collapsing

    def test_chunk_without_its_parent_on_the_page_is_kept(self, test_db, clean_tables):
        from src.mcp_server import memory_list
        parent, chunks = _chunked_doc("Doc", "text", 1)
        with _db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = now() - interval '1 day' WHERE id = %s", (parent,))
            conn.commit()
        assert [r["id"] for r in memory_list(limit=1)] == [str(chunks[0])]


# --------------------------------------------------------------------------- M-4

class TestBooleanIntegersAreRejected:
    def test_list_limit_true_is_an_error_result(self, test_db, clean_tables):
        from src.mcp_server import mcp
        _db.create_memory(type="idea", title="I", content="c")
        for args in ({"limit": True}, {"since_days": False}):
            result, text = _call(mcp, "memory_list", args)
            assert result.isError is True and "must be an integer" in text, text

    def test_dict_tools_return_the_error_object(self, test_db, clean_tables):
        from src.mcp_server import mcp
        from src import mcp_readonly
        cases = [(mcp, "memory_search", {"query": "x", "limit": True}, "limit"),
                 (mcp, "memory_search", {"query": "x", "since_days": True}, "since_days"),
                 (mcp, "memory_recent_activity", {"days": True}, "days"),
                 (mcp_readonly.mcp, "memory_search", {"query": "x", "limit": False}, "limit"),
                 (mcp_readonly.mcp, "memory_list", {"limit": True}, None)]
        with patch("src.mcp_server.generate_embedding", side_effect=AssertionError("embedding called")):
            for server, tool, args, field in cases:
                result, text = _call(server, tool, args)
                if field is None:
                    assert result.isError is True and "limit must be an integer" in text
                    continue
                assert result.isError is False, text
                assert json.loads(text) == {"error": f"{field} must be an integer (got {args[field]!r})"}

    def test_brief_window_days_true_is_an_error(self, test_db, clean_tables):
        from src.mcp_server import mcp
        result, text = _call(mcp, "memory_brief", {"window_days": True})
        assert json.loads(text) == {"error": "window_days must be an integer (got True)"}

    def test_integer_schema_and_plain_integers_unchanged(self, test_db, clean_tables):
        from src.mcp_server import mcp
        schema = {t.name: t.inputSchema for t in asyncio.run(mcp.list_tools())}
        assert schema["memory_list"]["properties"]["limit"] == {"default": 20, "title": "Limit", "type": "integer"}
        assert schema["memory_search"]["properties"]["since_days"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
        _db.create_memory(type="idea", title="I", content="c")
        result, text = _call(mcp, "memory_list", {"limit": 1})
        assert result.isError is False and json.loads(text)["title"] == "I"
