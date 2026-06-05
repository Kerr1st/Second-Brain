"""Baseline search/ranking regression tests for src/search.py.

Complements the existing test_search_properties.py structural tests
with behavioral baselines.

Validates: Requirements 1.5
"""

import re
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

import src.db as db
from src.search import hybrid_search, rerank, increment_access_count


class TestHybridSearchReturnsResults:
    """Insert memory with embedding, hybrid_search finds it."""

    def test_hybrid_search_returns_results(self, test_db, clean_tables, mock_embedding):
        content = "Quantum computing applications in cryptography"
        embedding = mock_embedding(content)

        db.create_memory(
            type="idea",
            title="Quantum Cryptography",
            content=content,
            embedding=str(embedding),
        )

        results = hybrid_search(content, embedding, limit=5)
        assert len(results) >= 1
        assert any(r["title"] == "Quantum Cryptography" for r in results)


class TestRerankPreservesOrderForExactMatch:
    """Exact title match ranks first after reranking."""

    def test_rerank_preserves_order_for_exact_match(self, test_db, clean_tables, mock_embedding):
        # Create two memories — one with an exact title match, one without
        target_content = "Machine learning model evaluation techniques"
        other_content = "Database indexing strategies for performance"

        db.create_memory(
            type="idea",
            title="Machine learning model evaluation techniques",
            content=target_content,
            embedding=str(mock_embedding(target_content)),
        )
        db.create_memory(
            type="idea",
            title="Database indexing strategies",
            content=other_content,
            embedding=str(mock_embedding(other_content)),
        )

        query = "Machine learning model evaluation techniques"
        results = hybrid_search(query, mock_embedding(query), limit=10)
        ranked = rerank(results, query)

        assert len(ranked) >= 1
        assert ranked[0]["title"] == "Machine learning model evaluation techniques"


class TestIncrementAccessCount:
    """increment_access_count bumps access_count by 1."""

    def test_increment_access_count(self, test_db, clean_tables):
        memory_id = db.create_memory(
            type="idea",
            title="Access Count Test",
            content="Testing retrieval reinforcement",
        )

        before = db.get_memory(memory_id)
        assert (before["access_count"] or 0) == 0

        increment_access_count([memory_id])

        after = db.get_memory(memory_id)
        assert after["access_count"] == 1


def _truncate_tables():
    """Inline cleanup helper for Hypothesis tests that can't use function-scoped fixtures."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_relationships")
            cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
            cur.execute("DELETE FROM memories")
        conn.commit()


class TestIncrementAccessCountUpdatesLastAccessedAt:
    """Feature: retrieval-quality, Property 16: increment_access_count Updates last_accessed_at

    After calling increment_access_count, each memory's last_accessed_at is non-NULL and recent.

    **Validates: Requirements 3.1**
    """

    @given(num_memories=st.integers(min_value=1, max_value=5))
    @settings(max_examples=25, deadline=None)
    def test_last_accessed_at_is_set_and_recent(self, num_memories, test_db):
        """Feature: retrieval-quality, Property 16: increment_access_count Updates last_accessed_at"""
        try:
            # Create N memories
            memory_ids = []
            for i in range(num_memories):
                mid = db.create_memory(
                    type="idea",
                    title=f"Memory {i}",
                    content=f"Content for memory {i}",
                )
                memory_ids.append(mid)

            # Call increment_access_count
            increment_access_count(memory_ids)

            # Verify each memory's last_accessed_at is non-NULL and recent
            for mid in memory_ids:
                mem = db.get_memory(mid)
                last_accessed = mem["last_accessed_at"]
                assert last_accessed is not None, f"last_accessed_at should be non-NULL for memory {mid}"

                # Ensure timezone-aware for comparison
                if last_accessed.tzinfo is None:
                    last_accessed = last_accessed.replace(tzinfo=timezone.utc)

                now = datetime.now(timezone.utc)
                delta = (now - last_accessed).total_seconds()
                assert delta < 10, (
                    f"last_accessed_at should be within 10 seconds of now, "
                    f"but was {delta:.2f}s ago for memory {mid}"
                )
        finally:
            _truncate_tables()


class TestHybridSearchProjectFiltering:
    """Feature: retrieval-quality, Property 11: hybrid_search Project Filtering

    All results have matching project or NULL project; no mismatched non-NULL projects.

    **Validates: Requirements 6.4**
    """

    @given(
        project_name=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(max_examples=25, deadline=None)
    def test_no_mismatched_projects_in_results(self, project_name, test_db):
        """Feature: retrieval-quality, Property 11: hybrid_search Project Filtering"""
        from tests.conftest import _deterministic_embedding

        # Use a distinct "other" project that differs from the generated one
        other_project = project_name + "_other"
        content_base = "Neural network architecture design patterns"

        try:
            # Insert a memory with the target project
            emb_target = _deterministic_embedding(content_base + " target " + project_name)
            db.create_memory(
                type="idea",
                title="Target project memory",
                content=content_base + " target " + project_name,
                embedding=str(emb_target),
                project=project_name,
            )

            # Insert a memory with a different project
            emb_other = _deterministic_embedding(content_base + " other " + other_project)
            db.create_memory(
                type="idea",
                title="Other project memory",
                content=content_base + " other " + other_project,
                embedding=str(emb_other),
                project=other_project,
            )

            # Insert a memory with NULL project (universal knowledge)
            emb_null = _deterministic_embedding(content_base + " universal")
            db.create_memory(
                type="idea",
                title="Universal memory",
                content=content_base + " universal",
                embedding=str(emb_null),
                project=None,
            )

            # Search with the target project filter
            query_emb = _deterministic_embedding(content_base)
            results = hybrid_search(content_base, query_emb, limit=10, project=project_name)

            # All results must have matching project or NULL project
            for r in results:
                mem_project = r.get("project")
                assert mem_project is None or mem_project == project_name, (
                    f"Result has project={mem_project!r}, expected {project_name!r} or NULL"
                )
        finally:
            _truncate_tables()


class TestHybridSearchSourceTypeFiltering:
    """hybrid_search with source_type returns only memories from that channel."""

    def test_source_type_filters_results(self, test_db, clean_tables, mock_embedding):
        content = "Distributed consensus algorithms and quorum design"
        db.create_memory(
            type="insight", title="Consensus (distilled)", content=content,
            embedding=str(mock_embedding(content)), source_type="distilled_chat",
        )
        other = content + " summarized from an external article"
        db.create_memory(
            type="insight", title="Consensus (article)", content=other,
            embedding=str(mock_embedding(other)), source_type="article",
        )

        results = hybrid_search(content, mock_embedding(content), limit=10,
                                source_type="distilled_chat")

        assert len(results) >= 1
        assert all(r["source_type"] == "distilled_chat" for r in results)
        assert any(r["title"] == "Consensus (distilled)" for r in results)


class TestHybridSearchCreatedAfterFiltering:
    """hybrid_search with created_after excludes memories older than the cutoff."""

    def test_created_after_excludes_old(self, test_db, clean_tables, mock_embedding):
        content = "Rate limiting strategies for API gateways"
        recent_id = db.create_memory(
            type="insight", title="Recent rate limiting", content=content,
            embedding=str(mock_embedding(content)),
        )
        old_content = content + " legacy notes from last quarter"
        old_id = db.create_memory(
            type="insight", title="Old rate limiting", content=old_content,
            embedding=str(mock_embedding(old_content)),
        )
        # Backdate the "old" memory well past the cutoff window.
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at = %s WHERE id = %s",
                            (datetime.now(timezone.utc) - timedelta(days=90), old_id))
            conn.commit()

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        results = hybrid_search(content, mock_embedding(content), limit=10, created_after=cutoff)

        ids = {str(r["id"]) for r in results}
        assert recent_id in ids, "recent memory should pass the created_after filter"
        assert old_id not in ids, "memory older than cutoff should be excluded"


class TestHybridSearchSourceTypeProperty:
    """Feature: agentic-retrieval, Property: hybrid_search source_type filtering.

    For any requested source_type, every result has that source_type — no other
    channel leaks through the filter.
    """

    @given(source_type=st.sampled_from(["distilled_chat", "cli_chat", "kiro_ide_chat", "article"]))
    @settings(max_examples=20, deadline=None)
    def test_only_matching_source_type_in_results(self, source_type, test_db):
        from tests.conftest import _deterministic_embedding

        other_type = "other_" + source_type
        base = "Event sourcing and CQRS tradeoffs in distributed systems"
        try:
            tgt = base + " target " + source_type
            db.create_memory(type="insight", title="Target", content=tgt,
                             embedding=str(_deterministic_embedding(tgt)), source_type=source_type)
            oth = base + " other " + other_type
            db.create_memory(type="insight", title="Other", content=oth,
                             embedding=str(_deterministic_embedding(oth)), source_type=other_type)

            results = hybrid_search(base, _deterministic_embedding(base), limit=10,
                                    source_type=source_type)

            for r in results:
                assert r["source_type"] == source_type, (
                    f"result has source_type={r['source_type']!r}, expected {source_type!r}"
                )
        finally:
            _truncate_tables()


class TestHybridSearchCreatedAfterProperty:
    """Feature: agentic-retrieval, Property: hybrid_search created_after filtering.

    For any cutoff, no returned memory was created before the cutoff.
    """

    @given(age_days=st.integers(min_value=31, max_value=365))
    @settings(max_examples=20, deadline=None)
    def test_no_result_older_than_cutoff(self, age_days, test_db):
        from tests.conftest import _deterministic_embedding

        content = "Backpressure handling in streaming data pipelines"
        try:
            recent_id = db.create_memory(type="insight", title="Recent", content=content,
                                         embedding=str(_deterministic_embedding(content)))
            old_c = content + " older variant"
            old_id = db.create_memory(type="insight", title="Old", content=old_c,
                                      embedding=str(_deterministic_embedding(old_c)))
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE memories SET created_at = %s WHERE id = %s",
                                (datetime.now(timezone.utc) - timedelta(days=age_days), old_id))
                conn.commit()

            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            results = hybrid_search(content, _deterministic_embedding(content), limit=10,
                                    created_after=cutoff)

            ids = {str(r["id"]) for r in results}
            assert old_id not in ids
            for r in results:
                ca = r["created_at"]
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                assert ca >= cutoff, f"result created_at {ca} is older than cutoff {cutoff}"
        finally:
            _truncate_tables()


class TestHybridSearchDedup:
    """hybrid_search collapses near-duplicate content and caps results per parent (P2a)."""

    def test_near_duplicate_content_collapsed(self, test_db, clean_tables, mock_embedding):
        content = "Idempotency keys prevent duplicate payment processing"
        for i in range(3):
            db.create_memory(type="insight", title=f"Dup {i}", content=content,
                             embedding=str(mock_embedding(content + f" {i}")))

        results = hybrid_search(content, mock_embedding(content), limit=10)

        keys = [re.sub(r"\s+", " ", r["content"]).strip().lower()[:300] for r in results]
        assert len(keys) == len(set(keys)), "identical content should collapse to one result"

    def test_per_parent_cap(self, test_db, clean_tables, mock_embedding):
        parent_id = db.create_memory(
            type="insight", title="Parent", content="Parent doc on rate limiting algorithms",
            embedding=str(mock_embedding("parent rate limiting")),
        )
        for i in range(4):
            c = f"Child {i}: token bucket versus leaky bucket rate limiting tradeoffs"
            db.create_memory(type="insight", title=f"Child {i}", content=c,
                             embedding=str(mock_embedding(c)), parent_id=parent_id)

        q = "rate limiting token bucket leaky bucket"
        results = hybrid_search(q, mock_embedding(q), limit=10)

        from collections import Counter
        counts = Counter(str(r.get("parent_id") or r["id"]) for r in results)
        assert all(c <= 2 for c in counts.values()), f"a parent exceeded the cap of 2: {counts}"


class TestHybridSearchDedupProperty:
    """Feature: agentic-retrieval, Property: dedup invariants always hold.

    No two results share content[:300], and no parent contributes >2 results.
    """

    @given(n_dups=st.integers(min_value=2, max_value=5),
           n_children=st.integers(min_value=3, max_value=6))
    @settings(max_examples=15, deadline=None)
    def test_dedup_invariants(self, n_dups, n_children, test_db):
        from tests.conftest import _deterministic_embedding
        from collections import Counter

        try:
            dup = "Shared identical insight about cache invalidation strategy"
            for i in range(n_dups):
                db.create_memory(type="insight", title=f"Dup {i}", content=dup,
                                 embedding=str(_deterministic_embedding(dup + f" {i}")))
            parent_id = db.create_memory(
                type="insight", title="Parent", content="Parent on cache invalidation",
                embedding=str(_deterministic_embedding("parent cache invalidation")),
            )
            for i in range(n_children):
                c = f"Child {i}: cache invalidation TTL versus write-through tradeoffs"
                db.create_memory(type="insight", title=f"Child {i}", content=c,
                                 embedding=str(_deterministic_embedding(c)), parent_id=parent_id)

            q = "cache invalidation strategy TTL write-through"
            results = hybrid_search(q, _deterministic_embedding(q), limit=20)

            keys = [re.sub(r"\s+", " ", r["content"]).strip().lower()[:300] for r in results]
            assert len(keys) == len(set(keys)), "near-duplicate content not collapsed"
            counts = Counter(str(r.get("parent_id") or r["id"]) for r in results)
            assert all(c <= 2 for c in counts.values()), f"parent cap exceeded: {counts}"
        finally:
            _truncate_tables()


class TestHybridSearchEmptyFtsQuery:
    """An all-punctuation query yields an empty ts_query — search must not crash (vector-only)."""

    def test_punctuation_only_query_returns_without_error(self, test_db, clean_tables, mock_embedding):
        content = "Distributed locks and lease-based coordination"
        db.create_memory(type="insight", title="Locks", content=content,
                         embedding=str(mock_embedding(content)))
        # "!!! ??? ..." strips to an empty FTS query -> exercises the vector-only branch
        results = hybrid_search("!!! ??? ...", mock_embedding(content), limit=5)
        assert isinstance(results, list)  # no crash; vector side still returns candidates
