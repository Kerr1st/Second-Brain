"""Baseline CRUD regression tests for src/db.py (data-access layer).

Validates: Requirements 1.5
"""

import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

import src.db as db


class TestCreateAndGetMemory:
    """create_memory returns valid UUID, get_memory retrieves it."""

    def test_create_and_get_memory(self, test_db, clean_tables):
        memory_id = db.create_memory(
            type="idea",
            title="Test Idea",
            content="This is a test idea for regression testing.",
        )

        # create_memory returns a valid UUID string
        parsed = uuid.UUID(memory_id)
        assert str(parsed) == memory_id

        # get_memory retrieves the same record
        row = db.get_memory(memory_id)
        assert row is not None
        assert row["title"] == "Test Idea"
        assert row["content"] == "This is a test idea for regression testing."
        assert row["type"] == "idea"


class TestSearchSimilarReturnsResults:
    """Insert memory with embedding, search_similar finds it."""

    def test_search_similar_returns_results(self, test_db, clean_tables, mock_embedding):
        content = "Unique content for vector search baseline test"
        embedding = mock_embedding(content)

        db.create_memory(
            type="idea",
            title="Vector Search Target",
            content=content,
            embedding=str(embedding),
        )

        results = db.search_similar(embedding, limit=5)
        assert len(results) >= 1
        assert any(r["title"] == "Vector Search Target" for r in results)


class TestCreateAndGetRelationship:
    """create_relationship persists, get_relationships retrieves."""

    def test_create_and_get_relationship(self, test_db, clean_tables):
        id_a = db.create_memory(type="idea", title="Memory A", content="First memory")
        id_b = db.create_memory(type="idea", title="Memory B", content="Second memory")

        db.create_relationship(id_a, id_b, "related_to", note="test link")

        rels = db.get_relationships(id_a)
        assert len(rels) >= 1
        assert any(
            {str(r["source_id"]), str(r["target_id"])} == {id_a, id_b} and str(r["neighbor_id"]) == id_b
            for r in rels
        )


def _truncate_tables():
    """Inline cleanup helper for Hypothesis tests that can't use function-scoped fixtures."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_relationships")
            cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
            cur.execute("DELETE FROM memories")
        conn.commit()


class TestCreateMemoryV2FieldsRoundTrip:
    """Feature: retrieval-quality, Property 2: create_memory V2 Fields Round Trip

    Validates: Requirements 2.5
    """

    @given(
        mem_class=st.sampled_from(["semantic", "episodic", "procedural"]),
        project=st.text(min_size=1, max_size=100, alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00")),
    )
    @settings(max_examples=25, deadline=None)
    def test_create_memory_v2_fields_round_trip(self, test_db, mem_class: str, project: str):
        """Feature: retrieval-quality, Property 2: create_memory V2 Fields Round Trip

        For any valid mem_class in {semantic, episodic, procedural} and any non-empty
        project string, create_memory + get_memory round-trips the values.

        **Validates: Requirements 2.5**
        """
        try:
            memory_id = db.create_memory(
                type="idea",
                title="V2 Round Trip Test",
                content="Testing V2 field persistence.",
                mem_class=mem_class,
                project=project,
            )

            row = db.get_memory(memory_id)
            assert row is not None
            assert row["mem_class"] == mem_class
            assert row["project"] == project
        finally:
            _truncate_tables()


class TestFindSchemasForMemories:
    """find_schemas_for_memories batches find_schemas_for_memory: one query for many
    constituents, grouped per constituent, newest schema first, and byte-for-byte the
    same rows the single-id lookup returns."""

    def test_groups_schemas_by_constituent_newest_first(self, test_db, clean_tables):
        a = db.create_memory(type="idea", title="A", content="constituent a")
        b = db.create_memory(type="idea", title="B", content="constituent b")
        orphan = db.create_memory(type="idea", title="Orphan", content="belongs to no schema")

        older = db.create_memory(type="schema", title="Older schema", content="s1")
        newer = db.create_memory(type="schema", title="Newer schema", content="s2")
        not_a_schema = db.create_memory(type="synthesis", title="Synthesis", content="x")

        # Schemas point at their constituents via derived_from (schema -> constituent).
        db.create_relationship(older, a, "derived_from")
        db.create_relationship(newer, a, "derived_from")
        db.create_relationship(newer, b, "derived_from")
        # Wrong relation type / wrong source type must be ignored, as in the single lookup.
        db.create_relationship(not_a_schema, a, "derived_from")
        db.create_relationship(older, b, "supports")

        # Force a strict created_at ordering between the two schemas.
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = now() - interval '1 day' WHERE id = %s", (older,))
            conn.commit()

        grouped = db.find_schemas_for_memories([a, b, orphan])

        assert set(grouped) == {a, b}, "only constituents with at least one schema appear"
        assert [s["title"] for s in grouped[a]] == ["Newer schema", "Older schema"]
        assert [s["title"] for s in grouped[b]] == ["Newer schema"]
        assert "constituent_id" not in grouped[a][0], "grouping key is not leaked into rows"

        assert [str(s["id"]) for s in grouped[a]] == [newer, older]
        assert [str(s["id"]) for s in grouped[b]] == [newer]

    def test_empty_input_short_circuits(self, test_db):
        assert db.find_schemas_for_memories([]) == {}


class TestGetMemoriesByIds:
    """Batched fetch for context packets: one query, requested order, no vectors."""

    def test_preserves_order_skips_missing_and_omits_vectors(self, test_db, clean_tables):
        a = db.create_memory(type="idea", title="A", content="alpha", embedding=str([0.1] * 1024))
        b = db.create_memory(type="idea", title="B", content="beta")
        missing = str(uuid.uuid4())

        rows = db.get_memories_by_ids([b, missing, a])

        assert [r["title"] for r in rows] == ["B", "A"]
        assert all("embedding" not in r and "search_vector" not in r for r in rows)
        assert rows[1]["content"] == "alpha"

    def test_empty_input(self, test_db):
        assert db.get_memories_by_ids([]) == []
