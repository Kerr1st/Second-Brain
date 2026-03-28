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
