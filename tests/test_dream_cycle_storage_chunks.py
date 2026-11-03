"""Accepted discoveries respect the corpus chunk bound (triage 2026-10-29 D5-5).

A discovery longer than 4,000 characters is stored like ingested long content: a
parent row without its own vector plus active chunk children with vectors. The
parent keeps the identity (ledger ``created_memory_id``, provenance and proposed
edges, duplicate detection). Short discoveries are stored exactly as before.
Embeddings come from the suite's deterministic fake; no provider is called.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from psycopg2.extras import RealDictCursor

from src.db import create_memory, get_connection, get_memory
from src.db.maintenance import oversize_vector_rows, plan_rechunk
from src.dream_cycle.storage import MAX_CHUNK_CHARS, store_accepted
from src.dream_cycle_db import create_run, store_candidate
from src.models import CandidateInsight

VOTES = {f"evaluator_{seat}_verdict": "ACCEPT" for seat in "abcd"}
LONG = "\n\n".join(f"## Point {i}\n\n" + f"Evidence line {i} links the sources. " * 12 for i in range(25))


@pytest.fixture
def corpus(test_schema, monkeypatch, mock_embedding):
    calls = []

    def embed(text):
        calls.append(text)
        return mock_embedding(text)

    monkeypatch.setattr("src.dream_cycle.storage.generate_embedding", embed)
    source = create_memory("source", "Captured", "Private work evidence", source_type="file")
    derived = create_memory("insight", "Previous discovery", "An earlier synthesis", metadata={"dream_cycle": True})
    snapshots = {mid: dict(get_memory(mid)) for mid in (source, derived)}
    run = create_run("user_triggered")
    store_candidate(run, {"title": "Previous discovery", "operation": "CREATE"}, VOTES, "ACCEPTED", derived)
    candidate = CandidateInsight("Long discovery", "insight", "CREATE", content=LONG, source_memories=[source],
                                 relationships=[{"target_id": derived, "relation_type": "supports", "note": "n"}])
    return source, derived, snapshots, run, candidate, calls


def _row(cur, mid):
    cur.execute("SELECT id, parent_id, status, content, embedding IS NOT NULL AS embedded, metadata, tags "
                "FROM memories WHERE id=%s", (mid,))
    return cur.fetchone()


def _children(cur, mid, status="active"):
    cur.execute("""SELECT id::text, content, embedding IS NOT NULL AS embedded, metadata, tags, source_type
                   FROM memories WHERE parent_id=%s AND status=%s
                   ORDER BY (metadata->>'chunk_index')::int""", (mid, status))
    return cur.fetchall()


def test_long_discovery_is_a_vectorless_parent_with_bounded_embedded_chunks(corpus):
    source, derived, context, run, c, calls = corpus
    assert len(LONG) > 2 * MAX_CHUNK_CHARS
    mid = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        parent = _row(cur, mid)
        assert parent["parent_id"] is None and parent["status"] == "active"
        assert parent["content"] == LONG and not parent["embedded"]
        assert parent["metadata"]["dream_cycle"] is True
        chunks = _children(cur, mid)
        assert len(chunks) >= 3
        assert "".join(ch["content"] for ch in chunks) == LONG
        for i, ch in enumerate(chunks):
            assert len(ch["content"]) <= MAX_CHUNK_CHARS and ch["embedded"]
            assert ch["metadata"]["chunk_index"] == i and ch["metadata"]["total_chunks"] == len(chunks)
            assert ch["source_type"] is None and "dream-cycle" in ch["tags"]
        # created_memory_id is the parent; edges attach to the parent, never to a chunk.
        cur.execute("SELECT created_memory_id::text FROM dream_cycle_candidates "
                    "WHERE run_id=%s AND candidate_json->>'title'='Long discovery'", (run,))
        assert cur.fetchone()["created_memory_id"] == mid
        cur.execute("SELECT source_id::text, target_id::text, relation_type FROM memory_relationships "
                    "WHERE source_id = ANY(%s::uuid[]) ORDER BY relation_type",
                    ([mid] + [ch["id"] for ch in chunks],))
        assert [tuple(r.values()) for r in cur.fetchall()] == [(mid, source, "derived_from"), (mid, derived, "supports")]
    # Only bounded texts were embedded, and no stored vector exceeds the bound.
    assert calls and all(len(text) <= MAX_CHUNK_CHARS for text in calls)
    assert oversize_vector_rows() == []
    # Same shape the rechunk applier writes: the planner has nothing to repair here.
    # (The unembedded fixture rows are planned; the discovery and its chunks are not.)
    planned = {op["memory_id"] for op in plan_rechunk()["operations"]}
    assert planned and not planned & ({mid} | {ch["id"] for ch in chunks})


def test_short_discovery_is_stored_exactly_as_before(corpus, mock_embedding):
    _, _, context, run, c, calls = corpus
    c = replace(c, content="A grounded connection")
    mid = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert calls == ["A grounded connection"]
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT embedding::text AS v, parent_id FROM memories WHERE id=%s", (mid,))
        row = cur.fetchone()
        assert row["parent_id"] is None
        assert [float(x) for x in row["v"].strip("[]").split(",")] == pytest.approx(mock_embedding(c.content), abs=1e-6)
        assert _children(cur, mid) == []


def test_repeated_long_discovery_is_deduplicated_against_its_chunked_parent(corpus):
    _, _, context, run, c, _ = corpus
    first = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context), range(2)))
    assert ids == [first, first]
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE parent_id IS NULL AND title='Long discovery'")
        assert cur.fetchone()[0] == 1


def test_update_and_supersede_of_a_chunked_discovery_leave_no_stale_active_chunks(corpus):
    source, _, context, run, c, _ = corpus
    mid = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    context = {**context, mid: dict(get_memory(mid))}
    # UPDATE to short text: the parent gets its own vector back, the chunk set retires.
    upd = replace(c, operation="UPDATE", target_memory_id=mid, content="Now a short synthesis", relationships=[])
    assert store_accepted(upd, run_id=run, verdicts=VOTES, authorized_memories=context) == mid
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert _row(cur, mid)["embedded"] and _children(cur, mid) == []
    # UPDATE back to long text, then SUPERSEDE: the superseded parent keeps no active chunks.
    context = {**context, mid: dict(get_memory(mid))}
    store_accepted(replace(upd, content=LONG + "\n\nRevised."), run_id=run, verdicts=VOTES, authorized_memories=context)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert not _row(cur, mid)["embedded"] and len(_children(cur, mid)) >= 3
    context = {**context, mid: dict(get_memory(mid))}
    sup = replace(upd, operation="SUPERSEDE", content=LONG + "\n\nSuperseding.", supersedes_reason="Better")
    new = store_accepted(sup, run_id=run, verdicts=VOTES, authorized_memories=context)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert _row(cur, mid)["status"] == "superseded" and _children(cur, mid) == []
        assert not _row(cur, new)["embedded"] and len(_children(cur, new)) >= 3
    assert oversize_vector_rows() == []


@pytest.mark.parametrize("kind", ["insight", "question"])
def test_express_brief_lists_a_chunked_discovery_once_as_its_parent(corpus, kind):
    """Chunks copy the parent's type and dream-cycle tag; readers that select by
    either must not list "(chunk i/n)" rows next to their parent."""
    from src.db.express_queries import _gather_insights, _gather_questions

    source, derived, context, run, c, calls = corpus
    mid = store_accepted(replace(c, type=kind), run_id=run, verdicts=VOTES, authorized_memories=context)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        chunk_ids = {ch["id"] for ch in _children(cur, mid)}
        assert len(chunk_ids) >= 3
        items = _gather_insights(cur, 7) if kind == "insight" else _gather_questions(cur, 10)
    ids = [i["id"] for i in items]
    assert ids.count(mid) == 1 and not chunk_ids & set(ids)
    assert all(i == mid or i == derived for i in ids)


def test_resurfacing_skips_untagged_chunks_of_a_dream_cycle_discovery(test_schema):
    """Maintenance-written chunks of dream-cycle discoveries may carry no tags; the
    tag filter alone let them resurface (review N5)."""
    from src.db.express_queries import _gather_resurfaced

    parent = create_memory("insight", "Discovery", "Parent text", tags=["dream-cycle"])
    chunk = create_memory("insight", "Discovery (chunk 1/2)", "Chunk text", tags=[], parent_id=parent)
    other_parent = create_memory("insight", "Captured note", "Note text", tags=["work"])
    other_chunk = create_memory("insight", "Captured note (chunk 1/2)", "Chunk", tags=[], parent_id=other_parent)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("UPDATE memories SET created_at = now() - interval '60 days'")
        ids = {i["id"] for i in _gather_resurfaced(cur, 10, 30, 14)}
    assert chunk not in ids and parent not in ids
    assert {other_parent, other_chunk} <= ids  # other chunked memories resurface as before


def test_citation_rate_credits_a_chunked_discovery_with_its_chunks_accesses(corpus):
    """Search returns a chunked discovery's chunks, so reinforcement lands on them,
    not on the vectorless parent (review S1)."""
    from src.dream_cycle_db import complete_run, get_tier2_metrics

    source, derived, context, run, c, _ = corpus
    long_id = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    short_id = store_accepted(replace(c, title="Short", content="A grounded connection",
                                      source_memories=[source, derived]),
                              run_id=run, verdicts=VOTES, authorized_memories=context)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        chunks = _children(cur, long_id)
        cur.execute("UPDATE memories SET access_count = 2 WHERE id = ANY(%s::uuid[])", ([ch["id"] for ch in chunks[:2]],))
        cur.execute("UPDATE memories SET access_count = 1 WHERE id = %s", (short_id,))
        conn.commit()
    complete_run(run, {"candidates_generated": 2, "candidates_accepted": 2, "candidates_rejected": 0}, "digest")
    # Per discovery: long = 0 + 2 + 2, short = 1. Chunks are not discoveries of their own.
    assert get_tier2_metrics(3)["insight_citation_rate"] == pytest.approx(2.5)


def _provider_error():
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "InvokeModel")


@pytest.mark.parametrize("error", ["client", "connection"])
def test_embedding_provider_error_is_a_discovery_embedding_error(monkeypatch, error):
    from botocore.exceptions import EndpointConnectionError
    from src.dream_cycle import storage

    exc = _provider_error() if error == "client" else EndpointConnectionError(endpoint_url="https://bedrock")
    monkeypatch.setattr(storage, "generate_embedding", lambda text: (_ for _ in ()).throw(exc))
    with pytest.raises(storage.DiscoveryEmbeddingError, match="chunk 1/") as raised:
        storage._prepare_vectors(LONG)
    assert raised.value.__cause__ is exc


def test_embedding_provider_error_on_a_chunk_blocks_only_that_candidate(corpus, monkeypatch, mock_embedding):
    """A provider failure while embedding chunk 2 of an accepted long discovery is
    that candidate's storage failure: nothing written, the run goes on (review S3)."""
    from src.dream_cycle import DreamCycleOrchestrator
    from src.ingest import chunk_by_section
    from src.models import EvaluatorVerdict, MemorySlice

    source, derived, context, _, long_c, _ = corpus
    short_c = replace(long_c, title="Short discovery", content="A grounded connection")
    second_chunk = [t for t in chunk_by_section(LONG, MAX_CHUNK_CHARS) if t.strip()][1]

    def embed(text):
        if text == second_chunk:
            raise _provider_error()
        return mock_embedding(text)

    monkeypatch.setattr("src.dream_cycle.storage.generate_embedding", embed)
    orch = DreamCycleOrchestrator()

    def think(_):
        orch._slice_memory_cache = context.copy()
        return [long_c, short_c]

    monkeypatch.setattr(orch, "_skim_arrivals", lambda *args: None)
    monkeypatch.setattr(orch, "invoke_explorer",
                        lambda *args: [MemorySlice("Supplied evidence", "cross_project_collision", [source, derived])])
    monkeypatch.setattr(orch, "_allocate_attention", lambda run, kind, at, slices: slices)
    monkeypatch.setattr(orch, "invoke_thinker", think)
    monkeypatch.setattr(orch, "_ground_candidate", lambda c: c)
    monkeypatch.setattr(orch, "invoke_evaluator", lambda c, role: EvaluatorVerdict(role, "ACCEPT"))
    monkeypatch.setattr("src.dream_cycle.orchestrator.generate_digest", lambda *a, **kw: "fixture.md")
    result = orch.run("user_triggered")
    assert result.candidates_generated == 2 and result.candidates_accepted == 1
    assert not result.aborted_early and result.candidates_unjudged == 0
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT final_verdict, created_memory_id, candidate_json FROM dream_cycle_candidates "
                    "WHERE run_id=%s ORDER BY created_at", (result.run_id,))
        blocked, accepted = cur.fetchall()
        assert (blocked["final_verdict"], accepted["final_verdict"]) == ("BLOCKED", "ACCEPTED")
        assert blocked["created_memory_id"] is None and accepted["created_memory_id"]
        assert blocked["candidate_json"]["storage_error"] == "DiscoveryEmbeddingError"
        assert "chunk 2/" in blocked["candidate_json"]["block_reason"]
        assert "ThrottlingException" in blocked["candidate_json"]["block_reason"]
        cur.execute("SELECT count(*) AS n FROM memories WHERE title LIKE 'Long discovery%%'")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT stats FROM dream_cycle_runs WHERE id=%s", (result.run_id,))
        assert cur.fetchone()["stats"]["candidates_storage_failed"] == 1
