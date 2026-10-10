"""Oversize Codex Topic Segments and derived task memories get bounded chunk vectors.

Two writers must agree on one layout: the Codex capture writer (new rows and
extensions) and the rechunk planner/applier (repair of rows written before the
bound). Re-planning after either must find nothing to do.
"""
import json
from types import SimpleNamespace

import pytest

from src import db
from src.capture import codex as codex_capture
from src.capture.agent_tasks import DerivedMemory, TaskSemanticResult, TopicSegment
from src.db.maintenance import apply_rechunk, digest, oversize_vector_rows, plan_rechunk
from src.ingest import chunk_by_section
from src.search import hybrid_search
from tests.test_codex_capture import (
    CORRECTION_TASK_ID,
    NOW,
    _SemanticScript,
    _install_services,
    _real_correction_codex_home,
    _services,
    _write_correction_rollout,
)

BOUND = 4000


def vector(text=''):
    return [1.0] + [0.0] * 1023


def other_vector(text=''):
    return [0.0, 1.0] + [0.0] * 1022


class RecordingEmbed:
    def __init__(self, fn=vector):
        self.lengths = []
        self.fn = fn

    def __call__(self, text):
        self.lengths.append(len(text))
        return self.fn(text)


def _segment_text(marker='', turns=3, size=3000):
    body = [f"# Topic {marker}".rstrip()]
    for n in range(1, turns + 1):
        body.append(f"## Agent Turn {n}\n\n" + ("evidence %d " % n) * (size // 11))
    return "\n\n".join(body)


def _codex_corpus(segment_content):
    task = db.create_memory('source', 'Codex task', 'whole task ' * 2000, source_type='codex_task',
                            source_url='codex://task-1', metadata={'record_kind': 'captured_task'})
    segment_meta = {'record_kind': 'topic_segment', 'segment_index': 0, 'turn_ids': ['t1', 't2', 't3'],
                    'task_source_url': 'codex://task-1'}
    segment = db.create_memory('source', 'Topic', segment_content, embedding=vector(), source_type='codex_task',
                               source_url='codex://task-1#segment-0', parent_id=task, metadata=segment_meta,
                               tags=['codex', 'topic-segment'], mem_class='source')
    small_meta = {**segment_meta, 'segment_index': 1, 'turn_ids': ['t4']}
    small = db.create_memory('source', 'Small topic', 'short segment', embedding=vector(), source_type='codex_task',
                             source_url='codex://task-1#segment-1', parent_id=task, metadata=small_meta)
    memory = db.create_memory('insight', 'Derived', 'derived insight. ' * 254, embedding=vector(),
                              source_type='distilled_agent_task', source_url='codex://task-1',
                              metadata={'record_kind': 'task_memory', 'kind': 'insight',
                                        'supporting_turn_ids': ['t1'], 'task_source_url': 'codex://task-1'})
    db.create_relationship(memory, segment, 'derived_from', note='Supporting Agent Turn IDs: t1')
    return task, segment, small, memory, segment_meta


def _active_chunks(parent):
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id::text, content, metadata, embedding IS NOT NULL FROM memories
                       WHERE parent_id=%s AND status='active' ORDER BY (metadata->>'chunk_index')::int""", (parent,))
        return cur.fetchall()


def _embedded(memory_id):
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT embedding IS NOT NULL FROM memories WHERE id=%s", (memory_id,))
        return cur.fetchone()[0]


def test_chunk_by_section_hard_splits_text_without_newlines():
    chunks = chunk_by_section("x" * 10_001, 4000)
    assert [len(c) for c in chunks] == [4000, 4000, 2001]
    assert "".join(chunks) == "x" * 10_001


def test_rechunk_repairs_oversize_codex_segment_and_task_memory(test_db, clean_tables, tmp_path):
    content = _segment_text(turns=3)  # ~9 k characters
    assert 8000 < len(content) < 12000
    task, segment, small, memory, segment_meta = _codex_corpus(content)
    assert {r['id'] for r in oversize_vector_rows(BOUND)} == {segment, memory}

    plan = plan_rechunk(BOUND)
    ops = {op['memory_id']: op for op in plan['operations']}
    # The Captured Task (represented by its segments) and the small segment are untouched.
    assert set(ops) == {segment, memory}
    assert all(op['mode'] == 'chunks' for op in ops.values())

    embed = RecordingEmbed()
    assert apply_rechunk(plan, digest(plan), embed=embed, cache_dir=tmp_path) == 2
    assert embed.lengths and max(embed.lengths) <= BOUND

    row = db.get_memory(segment)
    assert (str(row['id']), row['status'], row['content'], str(row['parent_id'])) == (segment, 'active', content, task)
    assert row['metadata'] == segment_meta  # segment_index / turn_ids unchanged
    assert not _embedded(segment) and _embedded(small)
    chunks = _active_chunks(segment)
    assert len(chunks) >= 3 and all(len(text) <= BOUND and embedded for _, text, _, embedded in chunks)
    assert "".join(text for _, text, _, _ in chunks) == content
    assert [m['chunk_index'] for _, _, m, _ in chunks] == list(range(len(chunks)))
    assert {m['record_kind'] for _, _, m, _ in chunks} == {'topic_segment_chunk'}
    assert all(m['segment_index'] == 0 and 'turn_ids' not in m for _, _, m, _ in chunks)

    memory_chunks = _active_chunks(memory)
    assert "".join(text for _, text, _, _ in memory_chunks) == db.get_memory(memory)['content']
    assert {m['record_kind'] for _, _, m, _ in memory_chunks} == {'task_memory_chunk'}
    assert not _embedded(memory)
    # Provenance still targets the segment row itself.
    assert [str(r['neighbor_id']) for r in db.get_relationships(memory)] == [segment]

    # The capture writer still finds the newest segment under its task, not a chunk.
    last = codex_capture._last_segment(SimpleNamespace(memory_id=task), db.get_connection)
    assert (str(last.memory_id), last.index) == (small, 1)

    assert oversize_vector_rows(BOUND) == []
    assert plan_rechunk(BOUND)['operations'] == []


def test_chunked_segment_is_one_hit_not_a_duplicate(test_db, clean_tables, tmp_path):
    content = _segment_text(turns=3) + "\n\nzanzibarquokka appears once in the final turn."
    _, segment, _, _, _ = _codex_corpus(content)
    plan = plan_rechunk(BOUND)
    apply_rechunk(plan, digest(plan), embed=other_vector, cache_dir=tmp_path)
    chunk_ids = {cid for cid, *_ in _active_chunks(segment)}
    tail = next(cid for cid, text, _, _ in _active_chunks(segment) if 'zanzibarquokka' in text)

    results = hybrid_search('zanzibarquokka', other_vector(), limit=10)
    ids = [str(r['id']) for r in results]
    assert len(ids) == len(set(ids))
    assert segment not in ids
    assert [i for i in ids if 'zanzibarquokka' in next(r['content'] for r in results if str(r['id']) == i)] == [tail]
    assert ids[0] == tail
    assert len([i for i in ids if i in chunk_ids]) <= 2  # per-parent cap

    # Why the segment's own vector is cleared: dedup groups the segment under its
    # task and its chunks under the segment, so both would be returned.
    with db.transaction() as conn, conn.cursor() as cur:
        cur.execute("UPDATE memories SET embedding=%s WHERE id=%s", (str(other_vector()), segment))
    ids = [str(r['id']) for r in hybrid_search('zanzibarquokka', other_vector(), limit=10)]
    assert segment in ids and tail in ids


def _render_large(segment, turn_lookup):
    return "\n\n".join([f"# {segment.title}"] + [
        f"## Agent Turn {n}\n\n" + ("evidence %d " % n) * 450
        for n, _ in enumerate(segment.turn_ids, start=1)])


def test_codex_writer_chunks_oversize_segments_and_replaces_chunks_on_extension(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_correction_codex_home(tmp_path, turn_indexes=(0,))
    decision = "Decision detail with rationale. " * 160  # > 4 k characters

    def initial(previous, turns):
        return TaskSemanticResult(
            segments=(TopicSegment(title="Large topic", turn_ids=(turns[0].turn_id,)),),
            memories=(DerivedMemory(segment=0, kind="decision", title="Large decision", content=decision,
                                    supporting_turn_ids=(turns[0].turn_id,)),),
        )

    def resumed(previous, turns):
        return TaskSemanticResult(
            segments=(TopicSegment(title="Large topic, extended",
                                   turn_ids=previous.turn_ids + (turns[0].turn_id,)),),
            memories=(),
        )

    embed = RecordingEmbed()
    monkeypatch.setattr(codex_capture, "_render_segment", _render_large)
    _install_services(monkeypatch, _services(home, _SemanticScript(initial, resumed),
                                             tmp_path / "capture.lock", embed=embed))

    assert codex_capture.run_codex_capture(NOW, task_id=CORRECTION_TASK_ID).failed == 0
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id::text, content, metadata FROM memories WHERE source_type='codex_task'
                       AND metadata->>'record_kind'='topic_segment'""")
        segment, first_content, first_meta = cur.fetchone()
        cur.execute("SELECT id::text FROM memories WHERE source_type='distilled_agent_task' AND parent_id IS NULL")
        memory = cur.fetchone()[0]
    assert len(first_content) > BOUND and not _embedded(segment) and not _embedded(memory)
    first_chunks = _active_chunks(segment)
    assert "".join(t for _, t, _, _ in first_chunks) == first_content
    assert "".join(t for _, t, _, _ in _active_chunks(memory)) == decision
    assert plan_rechunk(BOUND)['operations'] == []  # writer layout == planner layout

    _write_correction_rollout(home)
    assert codex_capture.run_codex_capture(NOW, task_id=CORRECTION_TASK_ID).semantic_processed == 1
    row = db.get_memory(segment)
    assert len(row['content']) > len(first_content)
    assert row['metadata']['segment_index'] == first_meta['segment_index'] == 0
    assert len(row['metadata']['turn_ids']) == 2
    second_chunks = _active_chunks(segment)
    assert "".join(t for _, t, _, _ in second_chunks) == row['content']
    assert len(second_chunks) > len(first_chunks)
    # Uncited old chunks are deleted, not left as superseded duplicates.
    assert all(db.get_memory(cid) is None for cid, *_ in first_chunks)
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE parent_id=%s AND status<>'active'", (segment,))
        assert cur.fetchone()[0] == 0

    assert embed.lengths and max(embed.lengths) <= BOUND
    assert oversize_vector_rows(BOUND) == []
    assert plan_rechunk(BOUND)['operations'] == []


def test_corpus_invariant_holds_across_source_types_after_rechunk(test_db, clean_tables, tmp_path, monkeypatch):
    """No active, embedded, childless row exceeds the bound, whatever its source type."""
    import src.ingest as ingest
    monkeypatch.setattr(ingest, 'generate_embedding', vector)
    monkeypatch.setattr(ingest, '_discover_relationships', lambda *a: None)
    ingest.ingest_content('# Article\n\n' + 'paragraph text\n\n' * 900, 'article', 'test://article')
    db.create_memory('insight', 'Agent memory', 'long agent insight ' * 300, embedding=vector())
    db.create_memory('source', 'QD event', 'small event', embedding=vector(), source_type='quick_desktop_feed',
                     source_url='qd://1', metadata={'capture_identity': True})
    _codex_corpus(_segment_text(turns=3))
    assert len(oversize_vector_rows(BOUND)) == 3

    plan = plan_rechunk(BOUND)
    apply_rechunk(plan, digest(plan), embed=vector, cache_dir=tmp_path)
    assert oversize_vector_rows(BOUND) == []
    assert plan_rechunk(BOUND)['operations'] == []
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(length(content)) FROM memories WHERE embedding IS NOT NULL AND status='active'")
        assert cur.fetchone()[0] <= BOUND
