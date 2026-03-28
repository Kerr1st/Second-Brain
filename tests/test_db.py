"""Baseline CRUD regression tests for src/db.py (data-access layer).

Validates: Requirements 1.5
"""

import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

import src.db as db
from tests.conftest import _deterministic_embedding


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
            str(r["source_id"]) == id_a and str(r["target_id"]) == id_b
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


class TestEmbeddingMockDeterminism:
    """Feature: retrieval-quality, Property 1: Embedding Mock Determinism

    Validates: Requirements 1.3
    """

    @given(text=st.text(min_size=0, max_size=500))
    @settings(max_examples=25)
    def test_embedding_mock_determinism(self, text: str):
        """Feature: retrieval-quality, Property 1: Embedding Mock Determinism

        For any input string, the mock returns a 1024-dim list of floats,
        and calling it twice with the same input produces the same output.

        **Validates: Requirements 1.3**
        """
        result = _deterministic_embedding(text)

        # Output is a list of exactly 1024 elements
        assert isinstance(result, list)
        assert len(result) == 1024

        # Every element is a float
        for val in result:
            assert isinstance(val, float)

        # Same input produces identical output (determinism)
        result2 = _deterministic_embedding(text)
        assert result == result2
