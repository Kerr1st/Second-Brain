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


class TestMemorySearchSchemaContext:
    """memory_search surfaces the schemas that its top results belong to: one batched
    lookup, deduplicated across results, newest first per result, capped at three."""

    def test_schema_context_deduped_and_capped(self, test_db, clean_tables):
        import src.db as db
        from src.mcp_server import memory_search

        base = "distributed consensus protocols and leader election"
        constituents = [
            db.create_memory(type="idea", title=f"Consensus {i}", content=f"{base} variant {i}",
                             embedding=str(_deterministic_embedding(f"{base} variant {i}")))
            for i in range(3)
        ]
        # Schemas carry no embedding, so they never appear as search results themselves.
        schemas = [db.create_memory(type="schema", title=f"Schema {j}", content=f"schema body {j}")
                   for j in range(4)]
        for s in schemas:  # every schema covers constituent 0 ...
            db.create_relationship(s, constituents[0], "derived_from")
        db.create_relationship(schemas[1], constituents[1], "derived_from")  # ... schema 1 also covers 1 and 2
        db.create_relationship(schemas[1], constituents[2], "derived_from")

        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding), \
             patch("src.mcp_server.find_schemas_for_memories", wraps=db.find_schemas_for_memories) as batched:
            response = memory_search(query=base, limit=5)

        assert {r["title"] for r in response["results"]} == {"Consensus 0", "Consensus 1", "Consensus 2"}
        ctx = response["schema_context"]
        assert len(ctx) == 3, "capped at three schemas"
        ids = [c["id"] for c in ctx]
        assert len(ids) == len(set(ids)), "a schema shared by several results appears once"
        assert set(ids) <= set(schemas)
        assert all(c["type"] == "schema" and c["title"] and c["content"] for c in ctx)
        # One round trip for all top results, not one per result.
        assert batched.call_count == 1




class TestProvenanceInToolOutputs:
    """The Explorer must be able to tell a Slack day from a May synthesis: search and
    list results carry source_type and created_at, and memory_recent_activity
    summarises what arrived by channel."""

    def test_search_results_carry_source_type_and_created_at(self, test_db, clean_tables):
        base = "Quarterly business review prep with the Boeing account team"
        _db.create_memory(type="source", title="Slack: #boeing — 2026-09-15", content=base + " slack",
                          embedding=str(_deterministic_embedding(base + " slack")), source_type="slack")
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            from src.mcp_server import memory_search
            resp = memory_search(query=base, limit=5)
        assert resp["results"], "expected at least one hit"
        hit = resp["results"][0]
        assert hit["source_type"] == "slack"
        assert isinstance(hit["created_at"], str) and hit["created_at"][:4] == "2026" or hit["created_at"][:2] == "20"
        assert set(hit) >= {"id", "title", "type", "source_type", "created_at", "score", "content", "tags"}

    def test_list_since_days_and_parent_id(self, test_db, clean_tables):
        new_id = _db.create_memory(type="source", title="Slack today", content="c", source_type="slack")
        old_id = _db.create_memory(type="source", title="Slack long ago", content="c", source_type="slack")
        with _db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = now() - interval '40 days' WHERE id = %s", (old_id,))
            conn.commit()
        from src.mcp_server import memory_list
        out = memory_list(source_type="slack", since_days=2)
        ids = {r["id"] for r in out}
        assert new_id in ids and old_id not in ids
        assert out[0]["parent_id"] is None and "T" in out[0]["created_at"]

    def test_recent_activity_counts_documents_not_chunks_and_labels_generated(self, test_db, clean_tables):
        parent = _db.create_memory(type="source", title="Slack: DM with grace — 2026-09-15", content="c",
                                   source_type="slack")
        for i in range(3):
            _db.create_memory(type="source", title=f"Slack: DM with grace — 2026-09-15 (chunk {i+1}/3)",
                              content="c", source_type="slack", parent_id=parent)
        _db.create_memory(type="source", title="Budget alert", content="c", source_type="outlook_email")
        _db.create_memory(type="insight", title="A dream-cycle synthesis", content="c")  # no source_type
        old = _db.create_memory(type="source", title="Old article", content="c", source_type="article")
        with _db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = now() - interval '30 days' WHERE id = %s", (old,))
            conn.commit()

        from src.mcp_server import memory_recent_activity
        out = memory_recent_activity(days=7, samples_per_source=2)
        by = {s["source_type"]: s for s in out["by_source"]}
        assert by["slack"]["documents"] == 1 and by["slack"]["memories"] == 4
        assert by["outlook_email"]["documents"] == 1
        assert by["generated:insight"]["documents"] == 1
        assert "article" not in by                                   # outside the window
        assert out["total_documents"] == 3 and out["total_memories"] == 6
        assert [s["title"] for s in by["slack"]["samples"]] == ["Slack: DM with grace — 2026-09-15"]  # parent only
        assert out["by_source"][0]["source_type"] == "slack"        # sorted by documents desc, then memories

    def test_recent_activity_clamps_arguments(self, test_db, clean_tables):
        from src.mcp_server import memory_recent_activity
        assert memory_recent_activity(days=0)["days"] == 1
        assert memory_recent_activity(days=400)["days"] == 90



# ---------------------------------------------------------------------------
# Boundary validation — Phase 3 of docs/PLAN-mcp-review.md
#
# Every rejected input returns a clean, actionable error in the shape of the tool's
# normal return type (dict → {"error"}, str → "Error: ...", list → ValueError) and
# leaves the database untouched. A raw Postgres error (SQL text, constraint names)
# must never reach the client. All fixtures are synthetic; embeddings are patched.
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import Mock

import psycopg2
import pytest

from src import express
from src import validation as V

_SQL_MARKERS = ("LINE 1", "SELECT", "INSERT", "UPDATE memories", "fkey", "memory_relationships",
                "invalid input syntax")


def _count(table="memories"):
    with _db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return cur.fetchone()[0]


def _assert_clean(message):
    """The message is for an agent: no SQL, no constraint names."""
    assert isinstance(message, str) and message
    for marker in _SQL_MARKERS:
        assert marker not in message, f"leaked {marker!r} in {message!r}"


def _no_embedding():
    """generate_embedding must not be reached for rejected input (zero Bedrock calls)."""
    return patch("src.mcp_server.generate_embedding", Mock(side_effect=AssertionError("embedding called")))


class TestValidationHelpers:
    """Pure helpers in src/validation.py — no database."""

    def test_uuid_or_error_canonicalises_and_rejects(self):
        u = uuid.uuid4()
        assert V.uuid_or_error(str(u), "memory_id") == str(u)
        assert V.uuid_or_error(str(u).upper(), "memory_id") == str(u)
        assert V.uuid_or_error(f"  {u}  ", "memory_id") == str(u)
        assert V.uuid_or_error(u, "memory_id") == str(u)
        for bad in ("abc", "", "   ", None, 42, "3dbd6505"):
            with pytest.raises(V.ValidationError) as exc:
                V.uuid_or_error(bad, "memory_id")
            assert "memory_id" in str(exc.value)
            _assert_clean(str(exc.value))

    def test_uuid_error_truncates_long_values(self):
        with pytest.raises(V.ValidationError) as exc:
            V.uuid_or_error("x" * 500, "target_id")
        assert len(str(exc.value)) < 120

    def test_clamp_limit(self):
        assert V.clamp_limit(0) == 1
        assert V.clamp_limit(-7) == 1
        assert V.clamp_limit(10_000) == 100
        assert V.clamp_limit(50) == 50
        assert V.clamp_limit("7") == 7
        assert V.clamp_limit(5, lo=1, hi=3) == 3
        for bad in ("abc", None, [], True):
            with pytest.raises(V.ValidationError):
                V.clamp_limit(bad)

    def test_clamp_days(self):
        assert V.clamp_days(None) is None
        assert V.clamp_days(-5) == 0
        assert V.clamp_days(0) == 0
        assert V.clamp_days(30) == 30
        assert V.clamp_days(1000) == 365
        with pytest.raises(V.ValidationError) as exc:
            V.clamp_days("soon", "since_days")
        assert "since_days" in str(exc.value)

    def test_clean_tags(self):
        assert V.clean_tags(None) is None
        assert V.clean_tags([]) == []
        assert V.clean_tags([" a ", "a", "", "  ", "b", "a"]) == ["a", "b"]
        assert V.clean_tags(("x", "y")) == ["x", "y"]
        assert len(V.clean_tags([f"t{i % 10}" for i in range(50)])) == 10  # dupes collapse below the cap
        for bad in ("tag", b"tag", 3, {"a": 1}, [1, 2], ["ok", None]):
            with pytest.raises(V.ValidationError) as exc:
                V.clean_tags(bad)
            assert "tags must be a list of strings" in str(exc.value)
        with pytest.raises(V.ValidationError) as exc:
            V.clean_tags([f"tag{i}" for i in range(V.MAX_TAGS + 1)])
        assert f"at most {V.MAX_TAGS}" in str(exc.value)

    def test_one_of_lists_choices_and_optional_blank_means_no_filter(self):
        assert V.one_of("idea", V.MEMORY_TYPES, "type") == "idea"
        with pytest.raises(V.ValidationError) as exc:
            V.one_of("bogus_type", V.MEMORY_TYPES, "type")
        msg = str(exc.value)
        assert msg.startswith("type must be one of: ") and "insight" in msg and "'bogus_type'" in msg
        with pytest.raises(V.ValidationError):
            V.one_of("Idea", V.MEMORY_TYPES, "type")  # exact match, no case folding
        assert V.optional_one_of(None, V.MEMORY_STATUSES, "status") is None
        assert V.optional_one_of("", V.MEMORY_STATUSES, "status") is None
        assert V.optional_one_of("  ", V.MEMORY_STATUSES, "status") is None
        assert V.optional_one_of("archived", V.MEMORY_STATUSES, "status") == "archived"
        with pytest.raises(V.ValidationError):
            V.optional_one_of("bogus", V.MEMORY_STATUSES, "status")

    def test_vocabularies(self):
        assert V.MEMORY_TYPES == {"idea", "synthesis", "research", "insight", "question",
                                  "decision", "priority", "project", "connection", "source"}
        assert V.FILTERABLE_TYPES == V.MEMORY_TYPES | {"schema"}
        assert V.MEMORY_STATUSES == {"active", "superseded", "archived"}
        assert V.RELATION_TYPES == {"supports", "contradicts", "extends", "inspires",
                                    "derived_from", "related_to", "superseded_by"}

    def test_require_text_and_mapping(self):
        assert V.require_text("  keep me  ", "title") == "  keep me  "  # original value, not stripped
        for bad in ("", "   ", None, 12):
            with pytest.raises(V.ValidationError) as exc:
                V.require_text(bad, "title")
            assert str(exc.value) == "title must be a non-empty string"
        assert V.require_mapping(None, "metadata") is None
        assert V.require_mapping({"a": 1}, "metadata") == {"a": 1}
        for bad in ([], "x", 3):
            with pytest.raises(V.ValidationError) as exc:
                V.require_mapping(bad, "metadata")
            assert "metadata must be a JSON object" in str(exc.value)


class TestMemoryCreateValidation:
    """memory_create rejects bad input before embedding anything and writes nothing."""

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"type": "bogus_type"}, "type must be one of"),
        ({"type": "Idea"}, "type must be one of"),
        ({"type": "schema"}, "type must be one of"),          # dream-cycle only, not via MCP
        ({"title": ""}, "title must be a non-empty string"),
        ({"title": "   "}, "title must be a non-empty string"),
        ({"content": "\n\t "}, "content must be a non-empty string"),
        ({"tags": "single-string"}, "tags must be a list of strings"),
        ({"tags": ["ok", 7]}, "tags must be a list of strings"),
        ({"tags": [f"t{i}" for i in range(21)]}, "at most 20"),
        ({"metadata": ["not", "a", "dict"]}, "metadata must be a JSON object"),
        ({"metadata": "depth_score=1"}, "metadata must be a JSON object"),
    ])
    def test_rejected_input_returns_error_and_writes_nothing(self, test_db, clean_tables, kwargs, fragment):
        from src.mcp_server import memory_create
        args = {"type": "idea", "title": "Valid title", "content": "Valid content because reasons."}
        args.update(kwargs)
        before = _count()
        with _no_embedding():
            result = memory_create(**args)
        assert isinstance(result, str) and result.startswith("Error: ")
        assert fragment in result
        _assert_clean(result)
        assert _count() == before

    def test_valid_call_cleans_tags_and_does_not_mutate_metadata(self, test_db, clean_tables):
        from src.mcp_server import memory_create
        caller_metadata = {"origin": "test"}
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            result = memory_create(type="decision", title="Tagged", content="We decided X because Y.",
                                   tags=[" alpha ", "alpha", "", "beta"], metadata=caller_metadata)
        assert result.startswith("Created memory ")
        mid = result.split()[2]
        mem = _db.get_memory(mid)
        assert mem["tags"] == ["alpha", "beta"]
        assert mem["metadata"]["origin"] == "test" and "depth_score" in mem["metadata"]
        assert caller_metadata == {"origin": "test"}, "caller's dict must not be mutated"

    def test_every_memory_type_is_accepted(self, test_db, clean_tables):
        from src.mcp_server import memory_create
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            for t in sorted(V.MEMORY_TYPES):
                assert memory_create(type=t, title=f"A {t}", content=f"content for {t}").startswith("Created memory ")
        assert _count() == len(V.MEMORY_TYPES)


class TestMemorySearchValidation:
    """memory_search validates query/filters and clamps limit/since_days."""

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"query": ""}, "query must be a non-empty string"),
        ({"query": "   "}, "query must be a non-empty string"),
        ({"query": "ok", "type": "bogus_type"}, "type must be one of"),
        ({"query": "ok", "status": "bogus_status"}, "status must be one of"),
        ({"query": "ok", "limit": "many"}, "limit must be an integer"),
    ])
    def test_rejected_input_returns_error_dict(self, test_db, clean_tables, kwargs, fragment):
        from src.mcp_server import memory_search
        with _no_embedding():
            out = memory_search(**kwargs)
        assert set(out) == {"error"}
        assert fragment in out["error"]
        _assert_clean(out["error"])

    def test_limit_and_since_days_are_clamped(self, test_db, clean_tables):
        from src.mcp_server import memory_search
        seen = []

        def recorder(query_text, query_embedding, **kwargs):
            seen.append(kwargs)
            return []

        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding), \
             patch("src.mcp_server.hybrid_search", side_effect=recorder):
            assert memory_search(query="q", limit=0)["results"] == []
            assert memory_search(query="q", limit=10_000)["results"] == []
            assert memory_search(query="q", limit=7, since_days=10_000)["results"] == []
            assert memory_search(query="q", since_days=-3)["results"] == []
            assert memory_search(query="q", type="", status="")["results"] == []  # blank = no filter
        assert [k["limit"] for k in seen] == [1, 100, 7, 10, 10]
        horizon = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
        assert abs((seen[2]["created_after"] - horizon).total_seconds()) < 60
        assert seen[3]["created_after"] is None
        assert seen[4]["type"] is None and seen[4]["status"] is None

    def test_schema_is_a_valid_filter(self, test_db, clean_tables):
        from src.mcp_server import memory_search
        with patch("src.mcp_server.generate_embedding", side_effect=_deterministic_embedding):
            out = memory_search(query="anything", type="schema")
        assert "error" not in out and out["results"] == []


class TestMemoryReadGraphValidation:
    """Malformed ids give a clean error dict, never a Postgres message."""

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "", "   ", "3dbd6505", "'; DROP TABLE memories; --"])
    def test_read_and_graph_reject_malformed_ids_cleanly(self, test_db, clean_tables, bad_id):
        from src.mcp_server import memory_graph, memory_read
        for tool in (memory_read, memory_graph):
            out = tool(bad_id)
            assert set(out) == {"error"}
            assert "memory_id" in out["error"]
            _assert_clean(out["error"])

    def test_read_accepts_uppercase_uuid(self, test_db, clean_tables):
        from src.mcp_server import memory_graph, memory_read
        mid = _db.create_memory(type="idea", title="Cased", content="c")
        assert memory_read(mid.upper())["id"] == mid
        assert memory_graph(mid.upper())["memory"]["id"] == mid


class TestMemoryUpdateValidation:
    """memory_update validates fields and the id, and changes nothing on rejection."""

    def _snapshot(self, mid):
        m = _db.get_memory(mid)
        return (m["title"], m["content"], m["status"], m["type"], m["tags"], m["updated_at"])

    @pytest.mark.parametrize("kwargs, fragment", [
        ({}, "nothing to update"),
        ({"status": "bogus_status"}, "status must be one of"),
        ({"status": "Active"}, "status must be one of"),
        ({"type": "bogus_type"}, "type must be one of"),
        ({"type": "schema"}, "type must be one of"),
        ({"title": ""}, "title must be a non-empty string"),
        ({"content": "  "}, "content must be a non-empty string"),
        ({"tags": "not-a-list"}, "tags must be a list of strings"),
        ({"tags": [f"t{i}" for i in range(21)]}, "at most 20"),
    ])
    def test_rejected_input_returns_error_and_changes_nothing(self, test_db, clean_tables, kwargs, fragment):
        from src.mcp_server import memory_update
        mid = _db.create_memory(type="idea", title="Original", content="original content", tags=["keep"])
        before = self._snapshot(mid)
        with _no_embedding():
            result = memory_update(mid, **kwargs)
        assert result.startswith("Error: ") and fragment in result
        _assert_clean(result)
        assert self._snapshot(mid) == before

    def test_unknown_and_malformed_ids(self, test_db, clean_tables):
        from src.mcp_server import memory_update
        with _no_embedding():
            assert memory_update(str(uuid.uuid4()), status="archived") == "Error: Not found"
            bad = memory_update("not-a-uuid", status="archived")
        assert bad.startswith("Error: ") and "memory_id" in bad
        _assert_clean(bad)

    def test_valid_updates_still_work(self, test_db, clean_tables):
        from src.mcp_server import memory_update
        mid = _db.create_memory(type="idea", title="Original", content="original content")
        with _no_embedding():  # no content change → no embedding
            assert memory_update(mid, status="superseded", type="decision",
                                 tags=[" a ", "a", "b"]) == f"Updated memory {mid}"
        mem = _db.get_memory(mid)
        assert (mem["status"], mem["type"], mem["tags"]) == ("superseded", "decision", ["a", "b"])
        with _no_embedding():
            assert memory_update(mid.upper(), summary="short") == f"Updated memory {mid}"
        assert _db.get_memory(mid)["summary"] == "short"


class TestMemoryRelateValidation:
    """memory_relate checks the relation vocabulary, both ids, and that they differ."""

    def test_bogus_relation_type_writes_nothing(self, test_db, clean_tables):
        from src.mcp_server import memory_relate
        a = _db.create_memory(type="idea", title="A", content="a")
        b = _db.create_memory(type="idea", title="B", content="b")
        result = memory_relate(a, b, "bogus_relation")
        assert result.startswith("Error: relation_type must be one of") and "superseded_by" in result
        _assert_clean(result)
        assert _count("memory_relationships") == 0

    def test_missing_target_is_a_clean_not_found(self, test_db, clean_tables):
        from src.mcp_server import memory_relate
        a = _db.create_memory(type="idea", title="A", content="a")
        result = memory_relate(a, str(uuid.uuid4()), "supports")
        assert result == "Error: Not found: target_id"
        _assert_clean(result)
        assert memory_relate(str(uuid.uuid4()), a, "supports") == "Error: Not found: source_id"
        assert memory_relate(str(uuid.uuid4()), str(uuid.uuid4()), "supports") == "Error: Not found: source_id, target_id"
        assert _count("memory_relationships") == 0

    def test_self_relation_and_malformed_ids(self, test_db, clean_tables):
        from src.mcp_server import memory_relate
        a = _db.create_memory(type="idea", title="A", content="a")
        assert memory_relate(a, a, "supports") == "Error: source_id and target_id must be different memories"
        assert memory_relate(a, a.upper(), "supports").startswith("Error: source_id and target_id must be different")
        for bad in ("not-a-uuid", "", "3dbd6505"):
            for args in ((bad, a), (a, bad)):
                result = memory_relate(*args, "supports")
                assert result.startswith("Error: ") and "_id" in result
                _assert_clean(result)
        assert _count("memory_relationships") == 0

    def test_valid_relation_including_superseded_by(self, test_db, clean_tables):
        from src.mcp_server import memory_relate
        a = _db.create_memory(type="decision", title="Old", content="old")
        b = _db.create_memory(type="decision", title="New", content="new")
        assert memory_relate(a, b, "superseded_by", "revised") == f"Related {a} → superseded_by → {b}"
        rels = _db.get_relationships(a)
        assert [(str(r["target_id"]), r["relation_type"], r["note"]) for r in rels] == [(b, "superseded_by", "revised")]


class TestMemoryListValidation:
    """memory_list clamps limit/since_days and raises a clean ValueError for bad filters
    (a list has no slot for an error object; FastMCP reports the message as isError)."""

    def test_limit_and_since_days_are_clamped(self, test_db, clean_tables):
        from src.mcp_server import memory_list
        with patch("src.mcp_server.list_memories", wraps=_db.list_memories) as spy:
            assert memory_list(limit=0) == []
            assert memory_list(limit=10_000) == []
            assert memory_list(limit=5, since_days=99_999) == []
        limits = [c.kwargs["limit"] for c in spy.call_args_list]
        assert limits == [1, 100, 5]
        horizon = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
        assert abs((spy.call_args_list[2].kwargs["created_after"] - horizon).total_seconds()) < 60

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"type": "bogus_type"}, "type must be one of"),
        ({"status": "bogus_status"}, "status must be one of"),
        ({"limit": "lots"}, "limit must be an integer"),
    ])
    def test_bad_filters_raise_clean_value_error(self, test_db, clean_tables, kwargs, fragment):
        from src.mcp_server import memory_list
        with pytest.raises(ValueError) as exc:
            memory_list(**kwargs)
        assert fragment in str(exc.value)
        _assert_clean(str(exc.value))

    def test_schema_filter_and_blank_filters_still_work(self, test_db, clean_tables):
        from src.mcp_server import memory_list
        sid = _db.create_memory(type="schema", title="S", content="schema body")
        _db.create_memory(type="idea", title="I", content="idea body")
        assert [r["id"] for r in memory_list(type="schema")] == [sid]
        assert len(memory_list(type="", status="")) == 2


class TestMemoryLearnValidation:
    """memory_learn requires content and at least one topic before searching."""

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"content": "", "topics": "caching"}, "content must be a non-empty string"),
        ({"content": "article", "topics": ""}, "topics must be a non-empty string"),
        ({"content": "article", "topics": " , ,, "}, "at least one non-empty"),
    ])
    def test_rejected_input(self, test_db, clean_tables, kwargs, fragment):
        from src.mcp_server import memory_learn
        with _no_embedding():
            result = memory_learn(**kwargs)
        assert result.startswith("Error: ") and fragment in result


class TestMemoryBriefWindowClamp:
    """memory_brief clamps window_days to 0–365 instead of passing odd values to SQL."""

    def test_out_of_range_window_still_returns_a_briefing(self, test_db, clean_tables):
        from src.mcp_server import memory_brief
        with patch("src.mcp_server.express.compose_briefing", wraps=express.compose_briefing) as spy:
            for value in (-1, 0, 10_000):
                assert isinstance(memory_brief(window_days=value, use_llm=False), str)
        assert [c.kwargs["window_days"] for c in spy.call_args_list] == [0, 0, 365]


class TestRawDatabaseErrorsAreTranslated:
    """The _guarded helper: the two data-shape errors Postgres can raise become a clean
    ValidationError; everything else propagates unchanged."""

    def test_invalid_uuid_literal_never_shows_sql(self, test_db, clean_tables):
        from src.mcp_server import _guarded as guard
        with pytest.raises(V.ValidationError) as exc:
            guard(_db.get_memory, "not-a-uuid")  # bypasses the boundary check on purpose
        msg = str(exc.value)
        assert msg.startswith("Invalid value: ")
        assert "LINE 1" not in msg and "SELECT" not in msg
        assert isinstance(exc.value.__cause__, psycopg2.errors.InvalidTextRepresentation)

    def test_foreign_key_violation_never_shows_constraint(self, test_db, clean_tables):
        from src.mcp_server import _guarded as guard
        a = _db.create_memory(type="idea", title="A", content="a")
        with pytest.raises(V.ValidationError) as exc:
            guard(_db.create_relationship, a, str(uuid.uuid4()), "supports")
        assert str(exc.value) == "A referenced memory does not exist"
        assert _count("memory_relationships") == 0
        # the pooled connection was rolled back and is still usable
        assert _db.get_memory(a)["title"] == "A"

    def test_other_errors_propagate(self, test_db, clean_tables):
        from src.mcp_server import _guarded as guard

        def bad_column():
            with _db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT no_such_column FROM memories")

        with pytest.raises(psycopg2.errors.UndefinedColumn):
            guard(bad_column)
        with pytest.raises(ZeroDivisionError):
            guard(lambda: 1 / 0)


class TestErrorsOverTheProtocol:
    """End to end through FastMCP's in-memory transport: what an agent actually sees."""

    @staticmethod
    def _call(name, arguments):
        from mcp.shared.memory import create_connected_server_and_client_session
        from src.mcp_server import mcp

        async def go():
            async with create_connected_server_and_client_session(mcp) as client:
                return await client.call_tool(name, arguments)

        return asyncio.run(go())

    def test_read_with_malformed_id_is_a_structured_error(self, test_db, clean_tables):
        import json
        result = self._call("memory_read", {"memory_id": "not-a-uuid"})
        assert result.isError is False
        # bare `dict` return type → no output schema, so the dict travels as JSON text
        payload = json.loads(" ".join(c.text for c in result.content if hasattr(c, "text")))
        assert set(payload) == {"error"}
        _assert_clean(payload["error"])

    def test_list_with_bad_filter_is_reported_as_is_error_without_sql(self, test_db, clean_tables):
        result = self._call("memory_list", {"type": "bogus_type"})
        assert result.isError is True
        text = " ".join(c.text for c in result.content if hasattr(c, "text"))
        assert "type must be one of" in text
        _assert_clean(text)

    def test_relate_to_missing_target_over_protocol(self, test_db, clean_tables):
        a = _db.create_memory(type="idea", title="A", content="a")
        result = self._call("memory_relate", {"source_id": a, "target_id": str(uuid.uuid4()),
                                              "relation_type": "supports"})
        assert result.isError is False
        text = " ".join(c.text for c in result.content if hasattr(c, "text"))
        assert text == "Error: Not found: target_id"
        assert _count("memory_relationships") == 0
