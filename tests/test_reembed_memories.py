"""Integration tests for resumable local corpus re-embedding."""

from src.db import create_memory, get_connection
from scripts.reembed_memories import reembed_memories


VECTOR_A = [0.1] * 1024
VECTOR_B = [0.2] * 1024


def _legacy_only_memory(title: str, content: str, type: str = "insight") -> str:
    memory_id = create_memory(type=type, title=title, content=content)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET legacy_embedding = %s WHERE id = %s",
                (str(VECTOR_A), memory_id),
            )
        conn.commit()
    return memory_id


def test_reembedding_is_batched_and_resumes_from_active_nulls(test_db, clean_tables):
    first_id = _legacy_only_memory("First", "first content")
    second_id = _legacy_only_memory("Second", "second content")
    calls = []

    def embed(texts):
        calls.append(texts)
        return [VECTOR_B for _ in texts]

    first_run = reembed_memories(limit=1, batch_size=1, embed_batch=embed)
    second_run = reembed_memories(limit=1, batch_size=1, embed_batch=embed)
    third_run = reembed_memories(limit=1, batch_size=1, embed_batch=embed)

    assert first_run == {"eligible": 2, "processed": 1, "remaining": 1}
    assert second_run == {"eligible": 1, "processed": 1, "remaining": 0}
    assert third_run == {"eligible": 0, "processed": 0, "remaining": 0}
    assert calls == [["first content"], ["second content"]]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, embedding IS NOT NULL, embedding_space,
                       legacy_embedding IS NOT NULL
                FROM memories
                WHERE id IN (%s, %s)
                ORDER BY title
                """,
                (first_id, second_id),
            )
            rows = cur.fetchall()

    assert rows == [
        (first_id, True, "ollama:bge-m3:1024", True),
        (second_id, True, "ollama:bge-m3:1024", True),
    ]


def test_reembedding_prioritizes_derived_memories_before_raw_sources(
    test_db, clean_tables
):
    source_id = _legacy_only_memory("Older source", "raw transcript", type="source")
    decision_id = _legacy_only_memory("Newer decision", "durable choice", type="decision")

    reembed_memories(
        limit=1,
        batch_size=1,
        embed_batch=lambda texts: [VECTOR_B for _ in texts],
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, embedding IS NOT NULL FROM memories WHERE id IN (%s, %s)",
                (source_id, decision_id),
            )
            embedded = dict(cur.fetchall())

    assert embedded == {source_id: False, decision_id: True}
