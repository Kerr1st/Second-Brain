"""Property tests for relationship discovery in the ingest pipeline.

Feature: retrieval-quality
Properties 12, 13, 14 — relationship discovery caps, chunk skip, temporal neighbors.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hypothesis import given, settings, assume
from hypothesis import strategies as st

import src.db as db
from src.ingest import _discover_relationships, ingest_content
from tests.conftest import _deterministic_embedding


# ---------------------------------------------------------------------------
# Inline cleanup helper (for Hypothesis tests that can't use function fixtures)
# ---------------------------------------------------------------------------

def _truncate_tables():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_relationships")
            cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
            cur.execute("DELETE FROM memories")
        conn.commit()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_safe_text = st.text(
    min_size=10, max_size=200,
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
)


# ---------------------------------------------------------------------------
# Property 12: Relationship Discovery Caps
# ---------------------------------------------------------------------------

class TestRelationshipDiscoveryCaps:
    """Feature: retrieval-quality, Property 12: Relationship Discovery Caps

    For any newly ingested parent memory, the relationship discovery process
    shall create at most 3 semantic relationships and at most 3 temporal
    relationships (6 total maximum).

    **Validates: Requirements 8.5**
    """

    @given(
        num_existing=st.integers(min_value=0, max_value=8),
        body=_safe_text,
    )
    @settings(max_examples=25, deadline=None)
    def test_relationship_discovery_caps(self, test_db, num_existing: int, body: str):
        """Feature: retrieval-quality, Property 12: Relationship Discovery Caps

        Mock generate_embedding to avoid Bedrock calls. Pre-populate the DB
        with `num_existing` memories (some with embeddings, some within ±24h),
        then call _discover_relationships and verify at most 3 semantic + 3
        temporal relationships are created.

        **Validates: Requirements 8.5**
        """
        try:
            now = datetime.now(timezone.utc)

            # Create the parent memory
            parent_id = db.create_memory(
                type="idea",
                title="Parent Memory",
                content=body,
                embedding=str(_deterministic_embedding(body)),
            )

            # Create existing memories — some with embeddings (semantic candidates),
            # all within ±24h (temporal candidates)
            for i in range(num_existing):
                content_i = f"Existing memory number {i} about {body[:20]}"
                emb = _deterministic_embedding(content_i)
                mem_id = db.create_memory(
                    type="idea",
                    title=f"Existing {i}",
                    content=content_i,
                    embedding=str(emb),
                )
                # Shift created_at to be within ±24h of parent
                offset_hours = (i % 24) - 12  # range: -12 to +11
                with db.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE memories SET created_at = %s WHERE id = %s",
                            (now + timedelta(hours=offset_hours), mem_id),
                        )
                    conn.commit()

            # Mock generate_embedding to return deterministic embedding
            with patch("src.ingest.generate_embedding", side_effect=_deterministic_embedding):
                _discover_relationships(parent_id, body)

            # Count relationships by type (semantic vs temporal)
            rels = db.get_relationships(parent_id)
            semantic_count = sum(
                1 for r in rels
                if r.get("note") and "semantic_neighbor" in str(r["note"])
            )
            temporal_count = sum(
                1 for r in rels
                if r.get("note") and r["note"] == "temporal_neighbor"
            )

            assert semantic_count <= 3, f"Semantic relationships {semantic_count} > 3"
            assert temporal_count <= 3, f"Temporal relationships {temporal_count} > 3"
            assert semantic_count + temporal_count <= 6, (
                f"Total relationships {semantic_count + temporal_count} > 6"
            )
        finally:
            _truncate_tables()


# ---------------------------------------------------------------------------
# Property 13: Chunks Skip Relationship Discovery
# ---------------------------------------------------------------------------

class TestChunksSkipRelationshipDiscovery:
    """Feature: retrieval-quality, Property 13: Chunks Skip Relationship Discovery

    Memories with non-NULL parent_id do not trigger relationship discovery.

    **Validates: Requirements 8.6**
    """

    @given(body=_safe_text)
    @settings(max_examples=25, deadline=None)
    def test_chunks_skip_relationship_discovery(self, test_db, body: str):
        """Feature: retrieval-quality, Property 13: Chunks Skip Relationship Discovery

        Ingest content that produces chunks. Verify that _discover_relationships
        is only called for the parent, not for any chunk. We mock
        _discover_relationships and verify it's called exactly once (for the
        parent), and that chunk records have no relationships.

        **Validates: Requirements 8.6**
        """
        try:
            # Create a few existing memories so relationship discovery has candidates
            for i in range(3):
                content_i = f"Pre-existing memory {i} for chunk test"
                db.create_memory(
                    type="idea",
                    title=f"Pre-existing {i}",
                    content=content_i,
                    embedding=str(_deterministic_embedding(content_i)),
                )

            # Build content with a metadata header
            full_content = f"# Test Title\n\nSource: http://example.com\nType: article\n\n---\n\n{body}"

            with patch("src.ingest.generate_embedding", side_effect=_deterministic_embedding), \
                 patch("src.ingest._discover_relationships", wraps=_discover_relationships) as mock_discover:

                # Also patch generate_embedding inside _discover_relationships
                with patch("src.ingest.generate_embedding", side_effect=_deterministic_embedding):
                    parent_id = ingest_content(full_content, "article")

                assert parent_id is not None

                # _discover_relationships should have been called exactly once (for the parent)
                assert mock_discover.call_count == 1
                call_args = mock_discover.call_args
                assert call_args[0][0] == parent_id  # first positional arg is parent_id

            # Verify chunk records (parent_id IS NOT NULL) have no relationships
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM memories WHERE parent_id = %s",
                        (parent_id,),
                    )
                    chunk_ids = [str(row[0]) for row in cur.fetchall()]

            for chunk_id in chunk_ids:
                rels = db.get_relationships(chunk_id)
                # Filter to only relationships where the chunk is the source
                chunk_source_rels = [
                    r for r in rels if str(r["source_id"]) == chunk_id
                ]
                assert len(chunk_source_rels) == 0, (
                    f"Chunk {chunk_id} has {len(chunk_source_rels)} relationships as source"
                )
        finally:
            _truncate_tables()


# ---------------------------------------------------------------------------
# Property 14: find_temporal_neighbors Correctness
# ---------------------------------------------------------------------------

class TestFindTemporalNeighborsCorrectness:
    """Feature: retrieval-quality, Property 14: find_temporal_neighbors Correctness

    All returned memories have created_at within ±24h; specified memory_id
    never in results.

    **Validates: Requirements 8.7**
    """

    @given(
        num_neighbors=st.integers(min_value=1, max_value=10),
        num_far=st.integers(min_value=0, max_value=5),
        offset_hours=st.lists(
            st.floats(min_value=-23.9, max_value=23.9, allow_nan=False, allow_infinity=False),
            min_size=1, max_size=10,
        ),
    )
    @settings(max_examples=25, deadline=None)
    def test_find_temporal_neighbors_correctness(
        self, test_db, num_neighbors: int, num_far: int, offset_hours: list[float],
    ):
        """Feature: retrieval-quality, Property 14: find_temporal_neighbors Correctness

        Insert memories at various timestamps around a reference point. Call
        find_temporal_neighbors and assert:
        1. All results have created_at within ±24h of the reference
        2. The query memory_id is never in the results

        **Validates: Requirements 8.7**
        """
        try:
            now = datetime.now(timezone.utc)

            # Create the reference memory
            ref_id = db.create_memory(
                type="idea",
                title="Reference Memory",
                content="Reference memory for temporal neighbor test",
            )
            # Set its created_at to `now`
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE memories SET created_at = %s WHERE id = %s",
                        (now, ref_id),
                    )
                conn.commit()

            # Create near neighbors (within ±24h)
            for i, hours in enumerate(offset_hours[:num_neighbors]):
                mem_id = db.create_memory(
                    type="idea",
                    title=f"Near neighbor {i}",
                    content=f"Near neighbor content {i}",
                )
                ts = now + timedelta(hours=hours)
                with db.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE memories SET created_at = %s WHERE id = %s",
                            (ts, mem_id),
                        )
                    conn.commit()

            # Create far memories (outside ±24h)
            for i in range(num_far):
                mem_id = db.create_memory(
                    type="idea",
                    title=f"Far memory {i}",
                    content=f"Far memory content {i}",
                )
                far_offset = 48 + (i * 24)  # 48h, 72h, 96h, ...
                ts = now + timedelta(hours=far_offset)
                with db.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE memories SET created_at = %s WHERE id = %s",
                            (ts, mem_id),
                        )
                    conn.commit()

            # Call find_temporal_neighbors
            results = db.find_temporal_neighbors(ref_id, now, limit=10)

            # Assert: query memory_id never in results
            result_ids = {r["id"] for r in results}
            assert ref_id not in result_ids, "Reference memory_id found in results"

            # Assert: all results within ±24h
            for r in results:
                delta = abs((r["created_at"] - now).total_seconds())
                assert delta <= 24 * 3600, (
                    f"Result {r['id']} created_at delta {delta}s exceeds 24h"
                )
        finally:
            _truncate_tables()
