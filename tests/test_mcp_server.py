"""MCP smoke tests for src/mcp_server.py.

Validates: Requirements 1.6
"""

import uuid
from unittest.mock import patch

from tests.conftest import _deterministic_embedding


class TestMemoryCreateReturnsId:
    """memory_create returns a string containing a UUID."""

    def test_memory_create_returns_id(self, test_db, clean_tables):
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_create

            result = memory_create(
                type="idea",
                title="Smoke Test Idea",
                content=(
                    "This idea explains WHY caching matters because repeated lookups "
                    "are expensive, and when latency spikes then users leave. "
                    "Questions this answers: How does caching reduce latency?"
                ),
            )

        # Result is a string that contains a valid UUID
        assert isinstance(result, str)
        # Extract the UUID from the result string (format: "Created memory <uuid>...")
        parts = result.split()
        uuid_str = parts[2]  # "Created memory <uuid>"
        parsed = uuid.UUID(uuid_str)
        assert str(parsed) == uuid_str


class TestMemorySearchReturnsList:
    """memory_search returns a dict with results list and temporal_context."""

    def test_memory_search_returns_dict_with_results(self, test_db, clean_tables):
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_create, memory_search

            # Create a memory first so there's something to find
            memory_create(
                type="idea",
                title="Searchable Memory",
                content=(
                    "This explains WHY indexing matters because full table scans "
                    "are slow, and when data grows then queries degrade. "
                    "Questions this answers: How do indexes speed up queries?"
                ),
            )

            response = memory_search(query="indexing", limit=5)

        assert isinstance(response, dict)
        assert "results" in response
        assert "temporal_context" in response
        assert isinstance(response["results"], list)
        assert isinstance(response["temporal_context"], list)


class TestMemorySearchStableCandidateSelection:
    """The requested output size must not change the candidate population."""

    def test_smaller_limit_is_a_prefix_of_larger_limit(
        self, test_db, clean_tables
    ):
        from src.mcp_server import memory_search

        query = "monotonic codex capture provenance"
        query_vector = [1.0, *([0.0] * 1023)]
        decoy_vector = [0.99, 0.1410673598, *([0.0] * 1022)]

        target_id = _db.create_memory(
            type="decision",
            title=query,
            content="Preserve an Agent Task's durable lifecycle policy.",
            embedding=str(query_vector),
        )
        for index in range(6):
            _db.create_memory(
                type="source",
                title=f"Lexical candidate {index}",
                content=f"{query} transient candidate number {index}",
                embedding=str(decoy_vector),
            )

        # Independently place the target outside lexical retrieval. Its exact
        # title match should win utility reranking once the candidate reaches it.
        with _db.get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE memories SET search_vector = to_tsvector('english', 'durable lifecycle') "
                "WHERE id = %s",
                (target_id,),
            )
            connection.commit()

        with patch("src.mcp_server.generate_embedding", return_value=query_vector):
            larger = memory_search(query=query, limit=10)

        # memory_search reinforces returned rows, so restore the same search
        # state before comparing the second observation.
        with _db.get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE memories SET access_count = 0, last_accessed_at = NULL"
            )
            connection.commit()

        with patch("src.mcp_server.generate_embedding", return_value=query_vector):
            smaller = memory_search(query=query, limit=3)

        larger_ids = [item["id"] for item in larger["results"]]
        smaller_ids = [item["id"] for item in smaller["results"]]
        assert larger_ids[0] == target_id
        assert smaller_ids == larger_ids[:3]

    def test_root_memories_are_diversified_by_agent_task_provenance(
        self, test_db, clean_tables
    ):
        from src.mcp_server import memory_search

        query = "codex task capture lifecycle"
        query_vector = [1.0, *([0.0] * 1023)]
        other_vector = [0.8, 0.6, *([0.0] * 1022)]
        crowded_source = "codex://crowded-task"

        crowded_ids = {
            _db.create_memory(
                type="decision",
                title=f"Capture lifecycle decision {index}",
                content=f"{query} primary evidence variant {index}",
                embedding=str(query_vector),
                source_type="distilled_agent_task",
                metadata={"task_source_url": crowded_source},
            )
            for index in range(5)
        }
        other_ids = {
            _db.create_memory(
                type="insight",
                title=f"Related task evidence {index}",
                content=f"Independent supporting evidence from task {index}",
                embedding=str(other_vector),
                source_type="distilled_agent_task",
                metadata={"task_source_url": f"codex://other-task-{index}"},
            )
            for index in range(2)
        }

        with patch("src.mcp_server.generate_embedding", return_value=query_vector):
            response = memory_search(query=query, limit=4)

        result_ids = [item["id"] for item in response["results"]]
        assert len(crowded_ids.intersection(result_ids)) == 2
        assert other_ids.issubset(result_ids)
        assert all(
            _db.get_memory(memory_id)["access_count"] == 1
            for memory_id in result_ids
        )
        assert all(
            _db.get_memory(memory_id)["access_count"] == 0
            for memory_id in crowded_ids.difference(result_ids)
        )


class TestMemoryCreateDepthWarning:
    """Shallow content triggers depth warning."""

    def test_memory_create_depth_warning(self, test_db, clean_tables):
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_create

            # Shallow content: no causal connectors, no "Questions this answers:"
            result = memory_create(
                type="idea",
                title="Shallow Idea",
                content="This is a short shallow idea with no depth.",
            )

        assert isinstance(result, str)
        # Should contain a warning indicator
        assert "⚠" in result


# --- Property-Based Tests ---

import datetime
from hypothesis import given, settings
from hypothesis import strategies as st
import src.db as _db


def _make_temporal_neighbor(id_str, title, type_val, created_at):
    """Helper to build a temporal neighbor dict as returned by find_temporal_neighbors."""
    return {
        "id": id_str,
        "title": title,
        "type": type_val,
        "created_at": created_at,
    }


# Strategy: generate 0-5 fake main result IDs
_result_ids_st = st.lists(st.uuids().map(str), min_size=1, max_size=5)

# Strategy: generate 0-6 fake temporal neighbor IDs (some may overlap with main results)
_neighbor_count_st = st.integers(min_value=0, max_value=6)


class TestTemporalContextInvariants:
    """Property 15: Temporal Context Invariants

    Feature: retrieval-quality, Property 15: Temporal Context Invariants

    For any memory_search response containing a temporal_context list:
    (a) each entry shall contain id, title, type, created_at, and relation = "temporal_neighbor",
    (b) the list shall contain at most 3 entries, and
    (c) no entry's id shall match any id in the main search results.

    Validates: Requirements 9.2, 9.3, 9.4
    """

    @given(
        result_ids=st.lists(st.uuids().map(str), min_size=1, max_size=5),
        neighbor_count=st.integers(min_value=0, max_value=6),
        overlap_count=st.integers(min_value=0, max_value=3),
    )
    @settings(max_examples=25, deadline=None)
    def test_temporal_context_invariants(self, test_db, result_ids, neighbor_count, overlap_count):
        """Feature: retrieval-quality, Property 15: Temporal Context Invariants

        Validates: Requirements 9.2, 9.3, 9.4
        """
        from src.mcp_server import memory_search
        from src.db import create_memory
        from tests.conftest import _deterministic_embedding

        now = datetime.datetime.now(datetime.timezone.utc)

        try:
            # Create real memories for the "main results" — we need at least one
            # so that memory_search has a top result to find temporal neighbors for
            created_ids = []
            for i, rid in enumerate(result_ids):
                embedding = _deterministic_embedding(f"main result content {i}")
                mid = create_memory(
                    type="idea",
                    title=f"Main Result {i}",
                    content=f"main result content {i} about testing retrieval quality",
                    embedding=embedding,
                )
                created_ids.append(mid)

            # Create temporal neighbors (within ±24h of the first memory)
            neighbor_ids = []
            for i in range(neighbor_count):
                embedding = _deterministic_embedding(f"neighbor content {i}")
                mid = create_memory(
                    type="insight",
                    title=f"Temporal Neighbor {i}",
                    content=f"neighbor content {i} about something different",
                    embedding=embedding,
                )
                neighbor_ids.append(mid)

            # Call memory_search with mocked embedding
            with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
                response = memory_search(query="testing retrieval", limit=10)

            # Validate response structure
            assert isinstance(response, dict)
            assert "results" in response
            assert "temporal_context" in response

            temporal_context = response["temporal_context"]
            main_results = response["results"]

            # (b) At most 3 entries
            assert len(temporal_context) <= 3

            # Collect main result IDs
            main_ids = {r["id"] for r in main_results}

            for entry in temporal_context:
                # (a) Each entry has required fields
                assert "id" in entry
                assert "title" in entry
                assert "type" in entry
                assert "created_at" in entry
                assert "relation" in entry
                assert entry["relation"] == "temporal_neighbor"

                # (c) No duplicates with main results
                assert entry["id"] not in main_ids
        finally:
            with _db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM memory_relationships")
                    cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
                    cur.execute("DELETE FROM memories")
                conn.commit()


class TestMemorySearchFilters:
    """memory_search passes source_type + since_days (-> created_after) through to hybrid_search."""

    def test_source_type_and_since_days_filter(self, test_db, clean_tables):
        base = "Vector index tuning notes for HNSW recall"
        _db.create_memory(type="insight", title="Recent distilled", content=base + " distilled",
                          embedding=str(_deterministic_embedding(base + " distilled")),
                          source_type="distilled_chat")
        _db.create_memory(type="insight", title="Recent article", content=base + " article",
                          embedding=str(_deterministic_embedding(base + " article")),
                          source_type="article")
        old_id = _db.create_memory(type="insight", title="Old distilled",
                                   content=base + " older distilled variant",
                                   embedding=str(_deterministic_embedding(base + " older distilled variant")),
                                   source_type="distilled_chat")
        with _db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = %s WHERE id = %s",
                            (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=120), old_id))
            conn.commit()

        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_search
            resp = memory_search(query=base, limit=10, source_type="distilled_chat", since_days=30)

        ids = {r["id"] for r in resp["results"]}
        assert old_id not in ids, "since_days should exclude the 120-day-old memory"
        for r in resp["results"]:
            mem = _db.get_memory(r["id"])
            assert mem["source_type"] == "distilled_chat", "source_type filter leaked another channel"


class TestMemoryReadMcp:
    """memory_read returns full content (no embedding) or a not-found error."""

    def test_read_not_found(self, test_db, clean_tables):
        from src.mcp_server import memory_read
        assert memory_read(str(uuid.uuid4())) == {"error": "Not found"}

    def test_read_returns_full_content_without_embedding(self, test_db, clean_tables):
        mid = _db.create_memory(type="idea", title="Readable", content="Full content here",
                                embedding=str(_deterministic_embedding("Full content here")))
        from src.mcp_server import memory_read
        out = memory_read(mid)
        assert out["id"] == mid
        assert out["title"] == "Readable"
        assert "embedding" not in out
        assert isinstance(out["created_at"], str)  # datetimes serialized to ISO


class TestMemoryGraphMcp:
    """memory_graph returns a memory plus its relationships, or a not-found error."""

    def test_graph_not_found(self, test_db, clean_tables):
        from src.mcp_server import memory_graph
        assert memory_graph(str(uuid.uuid4())) == {"error": "Not found"}

    def test_graph_returns_relationships(self, test_db, clean_tables):
        a = _db.create_memory(type="idea", title="A", content="memory A")
        b = _db.create_memory(type="idea", title="B", content="memory B")
        _db.create_relationship(a, b, "supports", "because reasons")
        from src.mcp_server import memory_graph
        out = memory_graph(a)
        assert out["memory"]["id"] == a
        assert b in {rel["target_id"] for rel in out["relationships"]}


class TestMemoryRelateMcp:
    """memory_relate persists a relationship retrievable via get_relationships."""

    def test_relate_creates_relationship(self, test_db, clean_tables):
        a = _db.create_memory(type="idea", title="A", content="memory A")
        b = _db.create_memory(type="idea", title="B", content="memory B")
        from src.mcp_server import memory_relate
        msg = memory_relate(a, b, "extends", "adds detail")
        assert a in msg and b in msg
        rels = _db.get_relationships(a)
        assert any(str(r["target_id"]) == b and r["relation_type"] == "extends" for r in rels)


class TestMemoryListMcp:
    """memory_list filters by type and returns only matching memories."""

    def test_list_filters_by_type(self, test_db, clean_tables):
        _db.create_memory(type="decision", title="D1", content="a decision was made")
        _db.create_memory(type="insight", title="I1", content="an insight emerged")
        from src.mcp_server import memory_list
        out = memory_list(type="decision", limit=20)
        assert len(out) >= 1
        assert all(r["type"] == "decision" for r in out)
        titles = {r["title"] for r in out}
        assert "D1" in titles and "I1" not in titles


class TestMemoryUpdateMcp:
    """memory_update content change re-embeds and recomputes depth_score."""

    def test_update_content_sets_depth_score(self, test_db, clean_tables):
        mid = _db.create_memory(type="idea", title="Updatable", content="original content",
                                embedding=str(_deterministic_embedding("original content")))
        new_content = ("WHY this matters: because repeated work is wasteful, and when we cache "
                       "then latency drops. Questions this answers: how do we speed up lookups?")
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_update
            msg = memory_update(mid, content=new_content)
        assert mid in msg
        mem = _db.get_memory(mid)
        assert mem["content"] == new_content
        meta = mem["metadata"]
        if isinstance(meta, str):
            import json
            meta = json.loads(meta)
        assert meta.get("depth_score") is not None


class TestMemoryLearnMcp:
    """memory_learn returns the 'internalize' synthesis template with related memories."""

    def test_learn_returns_internalize_template(self, test_db, clean_tables):
        _db.create_memory(type="insight", title="Caching basics",
                          content="caching reduces latency for repeated lookups",
                          embedding=str(_deterministic_embedding("caching reduces latency")))
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_learn
            out = memory_learn(content="New article about caching strategies", topics="caching")
        assert "External Knowledge to Internalize" in out
        assert "Your Task" in out
        assert "New article about caching strategies" in out


class TestMemoryBrief:
    """memory_brief returns a Markdown briefing surfacing synthesized items."""

    def test_memory_brief_surfaces_dream_cycle_insight(self, test_db, clean_tables):
        import src.db as db

        db.create_memory(
            type="insight",
            title="A cross-project synthesis",
            content="connects project A and project B in a non-obvious way",
            tags=["dream-cycle", "assimilation"],
            metadata={"dream_cycle": True, "strategy": "cross_project_collision",
                      "source_memories": [], "confidence": "high"},
        )

        from src.mcp_server import memory_brief

        out = memory_brief(use_llm=False)  # deterministic — no LLM subprocess
        assert isinstance(out, str)
        assert "# Your briefing" in out
        assert "A cross-project synthesis" in out

    def test_memory_brief_empty_is_graceful(self, test_db, clean_tables):
        from src.mcp_server import memory_brief

        out = memory_brief(use_llm=False)
        assert isinstance(out, str)
        assert "quiet" in out.lower()
