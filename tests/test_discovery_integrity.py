"""Synthetic transactional proofs for model mutation authorization."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest
from src.db import create_memory, get_memory, get_connection, update_memory
from src.dream_cycle_db import create_run, run_lock, store_candidate
from src.dream_cycle.storage import CandidateAuthorizationError, authorize_candidate, store_accepted
from src.models import CandidateInsight

VOTES = {f"evaluator_{seat}_verdict": "ACCEPT" for seat in "abcd"}


@pytest.fixture
def corpus(test_schema, monkeypatch, mock_embedding):
    monkeypatch.setattr("src.dream_cycle.storage.generate_embedding", mock_embedding)
    source = create_memory("source", "Captured", "Private work evidence", source_type="file")
    derived = create_memory("insight", "Previous discovery", "An earlier synthesis", metadata={"dream_cycle": True})
    snapshots = {mid: dict(get_memory(mid)) for mid in (source, derived)}
    run = create_run("user_triggered")
    store_candidate(run, {"title": "Previous discovery", "operation": "CREATE"}, VOTES, "ACCEPTED", derived)
    candidate = CandidateInsight("New discovery", "insight", "CREATE", content="A grounded connection",
                                  source_memories=[source])
    return source, derived, snapshots, run, candidate


def _counts():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM memories), (SELECT count(*) FROM memory_relationships), (SELECT count(*) FROM dream_cycle_candidates)")
        return cur.fetchone()


@pytest.mark.parametrize("operation", ["UPDATE", "SUPERSEDE"])
def test_captured_source_cannot_be_mutated_even_with_unanimous_votes(corpus, operation):
    source, _, context, run, c = corpus
    c = replace(c, operation=operation, target_memory_id=source, supersedes_reason="Panel agreed")
    before = _counts()
    with pytest.raises(CandidateAuthorizationError, match="immutable"):
        store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert get_memory(source)["content"] == "Private work evidence"
    assert _counts() == before


def test_ledger_failure_rolls_back_memory_and_edges(corpus):
    _, _, context, run, c = corpus
    before = _counts()
    with patch("src.dream_cycle_db.store_candidate", side_effect=RuntimeError("ledger unavailable")):
        with pytest.raises(RuntimeError):
            store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert _counts() == before


def test_late_relationship_failure_rolls_back_update_and_provenance(corpus):
    _, derived, context, run, c = corpus
    c = replace(c, operation="UPDATE", target_memory_id=derived,
                relationships=[{"target_id": c.source_memories[0], "relation_type": "supports"}])
    before = _counts()
    with patch("src.dream_cycle.storage.create_relationship", side_effect=RuntimeError("edge unavailable")):
        with pytest.raises(RuntimeError):
            store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert get_memory(derived)["content"] == context[derived]["content"]
    assert _counts() == before


@pytest.mark.parametrize("operation", ["UPDATE", "SUPERSEDE"])
def test_mutable_discovery_and_ledger_commit_together(corpus, operation):
    source, derived, context, run, c = corpus
    c = replace(c, operation=operation, target_memory_id=derived, supersedes_reason="Better evidence")
    mid = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert get_memory(mid)["content"] == c.content
    assert get_memory(derived)["status"] == ("superseded" if operation == "SUPERSEDE" else "active")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT created_memory_id FROM dream_cycle_candidates WHERE run_id=%s AND candidate_json->>'title'='New discovery'", (run,))
        assert str(cur.fetchone()[0]) == mid
        cur.execute("SELECT count(*) FROM memory_relationships WHERE source_id=%s AND target_id=%s AND relation_type='derived_from'", (mid, source))
        assert cur.fetchone()[0] == 1


def test_stale_target_is_rejected_instead_of_last_writer_wins(corpus):
    _, derived, context, run, c = corpus
    c = replace(c, operation="UPDATE", target_memory_id=derived)
    update_memory(derived, content="Another writer's newer judgment")
    with pytest.raises(CandidateAuthorizationError, match="changed"):
        store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert get_memory(derived)["content"] == "Another writer's newer judgment"


@pytest.mark.parametrize("change", ["guidance", "inactive", "foreign_source", "invalid_relation"])
def test_authorization_fails_before_any_partial_write(corpus, change):
    source, derived, context, run, c = corpus
    if change == "guidance":
        update_memory(derived, tags=["governing-guidance"])
        context[derived] = dict(get_memory(derived))
        c = replace(c, operation="UPDATE", target_memory_id=derived)
    elif change == "inactive":
        update_memory(source, status="archived")
    elif change == "foreign_source":
        c = replace(c, source_memories=["00000000-0000-0000-0000-000000000001"])
    else:
        c = replace(c, relationships=[{"target_id": source, "relation_type": "superseded_by"}])
    before = _counts()
    with pytest.raises(CandidateAuthorizationError):
        store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert _counts() == before


def test_concurrent_same_source_duplicate_has_one_memory_and_two_ledgers(corpus):
    _, _, context, run, c = corpus
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context), range(2)))
    assert ids[0] == ids[1]
    assert _counts() == (3, 1, 3)


def test_same_content_different_evidence_is_a_semantic_duplicate(corpus):
    """Production semantics restored in R3 (fix 5): the same discovery re-derived from
    other evidence is accepted as a duplicate of the first, not stored twice. The
    second ledger row points at the first memory (an accepted discovery) and keeps
    its own source_memories, so provenance of the re-derivation is still auditable."""
    _, derived, context, run, c = corpus
    a = store_accepted(c, run_id=run, verdicts=VOTES, authorized_memories=context)
    before = _counts()
    b = store_accepted(replace(c, source_memories=[derived]), run_id=run, verdicts=VOTES, authorized_memories=context)
    assert a == b
    after = _counts()
    assert after[0] == before[0] and after[2] == before[2] + 1
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT created_memory_id, candidate_json->>'duplicate_of', candidate_json->'source_memories' FROM dream_cycle_candidates WHERE run_id=%s ORDER BY created_at DESC LIMIT 1", (run,))
        created_memory_id, duplicate_of, sources = cur.fetchone()
    assert created_memory_id is None and duplicate_of == a and sources == [derived]


def test_run_lock_excludes_overlap_and_releases_after_exception(test_schema):
    with pytest.raises(RuntimeError):
        with run_lock() as first:
            assert first
            with run_lock() as second:
                assert not second
            raise RuntimeError("crash")
    with run_lock() as third:
        assert third


def test_prior_candidate_survives_next_candidate_authorization_failure(corpus, monkeypatch):
    from src.dream_cycle import DreamCycleOrchestrator
    from src.models import MemorySlice, EvaluatorVerdict
    source, derived, context, _, candidate = corpus
    orch = DreamCycleOrchestrator()
    memory_slice = MemorySlice("Actual supplied evidence", "cross_project_collision", [source, derived])
    forbidden = replace(candidate, title="Forbidden rewrite", operation="UPDATE", target_memory_id=source)
    def think(_):
        orch._slice_memory_cache = context.copy()
        return [candidate, forbidden]
    monkeypatch.setattr(orch, "_skim_arrivals", lambda *args: None)
    monkeypatch.setattr(orch, "invoke_explorer", lambda *args: [memory_slice])
    monkeypatch.setattr(orch, "_allocate_attention", lambda run, kind, at, slices: slices)
    monkeypatch.setattr(orch, "invoke_thinker", think)
    monkeypatch.setattr(orch, "_ground_candidate", lambda c: c)
    monkeypatch.setattr(orch, "invoke_evaluator", lambda c, role: EvaluatorVerdict(role, "ACCEPT"))
    monkeypatch.setattr("src.dream_cycle.orchestrator.generate_digest", lambda *a, **kw: "fixture.md")
    result = orch.run("user_triggered")
    assert result.candidates_generated == 2 and result.candidates_accepted == 1
    assert not result.aborted_early and result.candidates_unjudged == 0
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT final_verdict, created_memory_id FROM dream_cycle_candidates WHERE run_id=%s ORDER BY created_at", (result.run_id,))
        rows = cur.fetchall()
        assert [r[0] for r in rows] == ["ACCEPTED", "BLOCKED"]
        assert rows[0][1] and rows[1][1] is None
        cur.execute("SELECT stats FROM dream_cycle_runs WHERE id=%s", (result.run_id,))
        stats = cur.fetchone()[0]
        # R3: the forbidden rewrite is blocked before any paid judging.
        assert stats["candidates_blocked_unjudged"] == 1 and "candidates_storage_failed" not in stats
    assert get_memory(source)["content"] == context[source]["content"]


def test_dedup_does_not_turn_metadata_marker_into_discovery_authority(corpus, mock_embedding):
    """A forged marker that restates the candidate is a semantic duplicate (no new
    memory, as in production), but it gains no accepted-discovery provenance and
    stays immutable to the Thinker."""
    source, _, context, run, candidate = corpus
    forged = create_memory("insight", "Imported marker", candidate.content,
        embedding=mock_embedding(candidate.content), metadata={"dream_cycle": True, "source_memories": [source]})
    before = _counts()
    returned = store_accepted(candidate, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert returned == forged
    assert _counts() == (before[0], before[1], before[2] + 1)  # ledger row only
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT created_memory_id, candidate_json->>'duplicate_of' FROM dream_cycle_candidates WHERE run_id=%s ORDER BY created_at DESC LIMIT 1", (run,))
        created_memory_id, duplicate_of = cur.fetchone()
    assert created_memory_id is None and duplicate_of == forged
    assert get_memory(forged)["title"] == "Imported marker"
    rewrite = replace(candidate, operation="UPDATE", target_memory_id=forged, title="Rewrite the marker")
    with pytest.raises(CandidateAuthorizationError, match="provenance"):
        store_accepted(rewrite, run_id=run, verdicts=VOTES, authorized_memories={**context, forged: dict(get_memory(forged))})


def test_semantic_duplicate_of_any_active_parent_creates_no_new_memory(corpus, mock_embedding):
    """Fix 5 (review C S3 2.4): production's check_duplicate semantics -- a candidate
    restating ANY active embedded parent (here a captured QD fact, different source
    set, not a discovery) is accepted as a duplicate, not stored twice."""
    source, _, context, run, candidate = corpus
    fact = create_memory("decision", "Captured fact", candidate.content, embedding=mock_embedding(candidate.content),
                         source_type="quick_desktop_decision", source_url="qd://entity/1")
    before = _counts()
    assert store_accepted(candidate, run_id=run, verdicts=VOTES, authorized_memories=context) == fact
    after = _counts()
    assert after[0] == before[0] and after[1] == before[1] and after[2] == before[2] + 1
    # A genuinely new candidate (different content) is still created.
    novel = replace(candidate, content="Something entirely different and new", title="Novel")
    created = store_accepted(novel, run_id=run, verdicts=VOTES, authorized_memories=context)
    assert created not in (fact, source) and _counts()[0] == before[0] + 1


def test_update_target_without_accepted_provenance_is_refused(corpus):
    """Review C M4 survivor: the provenance EXISTS guard in store_accepted is pinned."""
    source, _, context, run, candidate = corpus
    orphan = create_memory("insight", "Unledgered discovery", "Looks like a discovery but no ACCEPTED row",
                           metadata={"dream_cycle": True})
    context = {**context, orphan: dict(get_memory(orphan))}
    rewrite = replace(candidate, operation="UPDATE", target_memory_id=orphan, title="Rewrite")
    before = _counts()
    with pytest.raises(CandidateAuthorizationError, match="no accepted discovery provenance"):
        store_accepted(rewrite, run_id=run, verdicts=VOTES, authorized_memories=context)
    with pytest.raises(CandidateAuthorizationError, match="no accepted discovery provenance"):
        authorize_candidate(rewrite, context)
    assert _counts() == before
    assert get_memory(orphan)["content"] == "Looks like a discovery but no ACCEPTED row"


@pytest.mark.parametrize("change", [
    {"relationships": [{"target_id": "11111111-1111-1111-1111-111111111111", "relation_type": "supports"}]},
    {"relationships": [{"target_id": None, "relation_type": "derived_from"}]},
    {"source_memories": ["33333333-3333-3333-3333-333333333333"]},
    {"operation": "UPDATE", "target_memory_id": None},
    {"operation": "SUPERSEDE", "supersedes_reason": ""},
])
def test_unauthorized_candidate_is_blocked_before_any_evaluator_call(corpus, monkeypatch, change):
    """Fix 4 (review C S2 2.3): authorization runs before grounding and the panel, so an
    invalid candidate costs zero evaluator calls instead of ~$0.70."""
    from src.dream_cycle import DreamCycleOrchestrator
    from src.models import MemorySlice, EvaluatorVerdict
    source, derived, context, _, candidate = corpus
    bad = replace(candidate, title="Invalid", **({**change, "target_memory_id": source} if change.get("operation") else change))
    if change.get("operation") == "UPDATE":
        bad = replace(bad, target_memory_id=source)  # captured source: immutable
    orch = DreamCycleOrchestrator()
    memory_slice = MemorySlice("Supplied evidence", "cross_project_collision", [source, derived])
    calls = {"evaluator": 0, "grounding": 0}
    def think(_):
        orch._slice_memory_cache = context.copy()
        return [bad, candidate]
    def evaluate(c, role, stage=None):
        calls["evaluator"] += 1
        return EvaluatorVerdict(role, "ACCEPT")
    def ground(c):
        calls["grounding"] += 1
        return c
    monkeypatch.setattr(orch, "_skim_arrivals", lambda *args: None)
    monkeypatch.setattr(orch, "invoke_explorer", lambda *args: [memory_slice])
    monkeypatch.setattr(orch, "_allocate_attention", lambda run, kind, at, slices: slices)
    monkeypatch.setattr(orch, "invoke_thinker", think)
    monkeypatch.setattr(orch, "_ground_candidate", ground)
    monkeypatch.setattr(orch, "invoke_evaluator", evaluate)
    monkeypatch.setattr("src.dream_cycle.orchestrator.generate_digest", lambda *a, **kw: "fixture.md")
    result = orch.run("user_triggered")
    assert not result.aborted_early
    assert result.candidates_generated == 2 and result.candidates_accepted == 1 and result.candidates_unjudged == 0
    assert calls == {"evaluator": 4, "grounding": 1}  # only the valid candidate was judged
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT final_verdict, evaluator_a_verdict, candidate_json->>'block_reason' FROM dream_cycle_candidates WHERE run_id=%s ORDER BY created_at", (result.run_id,))
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["BLOCKED", "ACCEPTED"]
    assert rows[0][1] is None and rows[0][2]


def test_slice_ids_in_non_canonical_form_still_authorize_the_candidate(corpus, monkeypatch):
    """Review C S3 2.5: the Explorer may spell ids upper-case/braced; the authorized
    context is keyed canonically so the candidate is not BLOCKED for a spelling."""
    from src.dream_cycle import DreamCycleOrchestrator
    from src.models import MemorySlice, EvaluatorVerdict
    source, derived, context, _, candidate = corpus
    orch = DreamCycleOrchestrator()
    odd_ids = ["{" + source.upper() + "}", derived.upper()]
    memory_slice = MemorySlice("Odd spellings", "cross_project_collision", odd_ids)
    def think(_):
        orch._slice_memory_cache = context.copy()
        return [candidate]
    monkeypatch.setattr(orch, "_skim_arrivals", lambda *args: None)
    monkeypatch.setattr(orch, "invoke_explorer", lambda *args: [memory_slice])
    monkeypatch.setattr(orch, "_allocate_attention", lambda run, kind, at, slices: slices)
    monkeypatch.setattr(orch, "invoke_thinker", think)
    monkeypatch.setattr(orch, "_ground_candidate", lambda c: c)
    monkeypatch.setattr(orch, "invoke_evaluator", lambda c, role, stage=None: EvaluatorVerdict(role, "ACCEPT"))
    monkeypatch.setattr("src.dream_cycle.orchestrator.generate_digest", lambda *a, **kw: "fixture.md")
    result = orch.run("user_triggered")
    assert result.candidates_accepted == 1 and not result.aborted_early
