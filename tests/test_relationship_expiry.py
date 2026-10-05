"""Relationship decay (dream-cycle Step 9) against the disposable database.

Live damage this pins (2026-10-05 audit of memory_bank): the access-only rule
expired 829 of the 872 edges created since 2026-09-26 on their first night, and
97 provenance (``derived_from``/``superseded_by``) edges along with them. The
rule is now: expire only when the edge is older than the threshold AND neither
endpoint was accessed within the threshold, and never for provenance types.
"""

from __future__ import annotations

import pytest

import src.db as db
import src.dream_cycle_db as dream_cycle_db

pytestmark = pytest.mark.usefixtures("test_schema")


def _edge(a: str, b: str, relation_type: str = "related_to") -> None:
    db.create_relationship(a, b, relation_type)


def _age_edges(days: int, *, relation_type: str | None = None) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        if relation_type is None:
            cur.execute("UPDATE memory_relationships SET created_at = now() - make_interval(days => %s)", (days,))
        else:
            cur.execute(
                "UPDATE memory_relationships SET created_at = now() - make_interval(days => %s) WHERE relation_type = %s",
                (days, relation_type),
            )
        conn.commit()


def _touch(memory_id: str, days_ago: int) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE memories SET last_accessed_at = now() - make_interval(days => %s) WHERE id = %s",
            (days_ago, memory_id),
        )
        conn.commit()


def _live_edges() -> set[tuple[str, str, str]]:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_id::text, target_id::text, relation_type FROM memory_relationships WHERE expired_at IS NULL"
        )
        return set(cur.fetchall())


def _memories(n: int) -> list[str]:
    return [db.create_memory("idea", f"m{i}", f"content {i}") for i in range(n)]


def test_new_edge_between_never_accessed_memories_survives(test_db, clean_tables):
    """The first-night shredder: a fresh edge whose endpoints have NULL
    last_accessed_at must not be expired. (Old rule: expired immediately.)"""
    a, b = _memories(2)
    _edge(a, b)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 0
    assert _live_edges() == {(a, b, "related_to")}


def test_old_edge_between_cold_memories_is_expired(test_db, clean_tables):
    a, b = _memories(2)
    _edge(a, b)
    _age_edges(100)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 1
    assert _live_edges() == set()


def test_edge_exactly_at_threshold_is_not_yet_stale(test_db, clean_tables):
    """Strict ``created_at < now() - threshold``: an 89-day edge is kept, a 91-day one goes."""
    a, b, c = _memories(3)
    _edge(a, b)
    _edge(b, c)
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE memory_relationships SET created_at = now() - interval '89 days' WHERE source_id = %s", (a,))
        cur.execute("UPDATE memory_relationships SET created_at = now() - interval '91 days' WHERE source_id = %s", (b,))
        conn.commit()

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 1
    assert _live_edges() == {(a, b, "related_to")}


@pytest.mark.parametrize("relation_type", ["derived_from", "superseded_by"])
def test_provenance_edges_never_expire(test_db, clean_tables, relation_type):
    """Lineage is permanent even when ancient and both endpoints are cold."""
    a, b, c = _memories(3)
    _edge(a, b, relation_type)
    _edge(a, c, "related_to")  # control: same age, same cold endpoints
    _age_edges(1000)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 1
    assert _live_edges() == {(a, b, relation_type)}


def test_recently_accessed_endpoint_protects_an_old_edge(test_db, clean_tables):
    """Access grace, pinned from both sides and for either endpoint.

    Two old edges share nothing: on one the *target* was read 5 days ago, on the
    other the *source* was read 95 days ago. Only the second is stale. Dropping
    the NOT EXISTS clause expires both (count 2); flipping its direction keeps
    both (count 0); checking only one endpoint column misses the protected
    target — every variant changes the asserted count or set.
    """
    a, b, c, d = _memories(4)
    _edge(a, b)  # protected: b (target) accessed inside the window
    _edge(c, d)  # stale: c (source) accessed, but outside the window
    _age_edges(120)
    _touch(b, 5)
    _touch(c, 95)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 1
    assert _live_edges() == {(a, b, "related_to")}


def test_access_on_the_source_endpoint_also_protects(test_db, clean_tables):
    a, b = _memories(2)
    _edge(a, b)
    _age_edges(120)
    _touch(a, 1)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 0
    assert _live_edges() == {(a, b, "related_to")}


def test_access_exactly_at_threshold_boundary_still_protects(test_db, clean_tables):
    """``last_accessed_at >= now() - threshold``: an access 90 days ago (to the
    second) is inside the grace window; the mutation to ``>`` is not visible
    here only if timing is unlucky, so the test uses 89 vs 91 days."""
    a, b, c, d = _memories(4)
    _edge(a, b)
    _edge(c, d)
    _age_edges(200)
    _touch(a, 89)
    _touch(c, 91)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 1
    assert _live_edges() == {(a, b, "related_to")}


def test_already_expired_edges_are_not_counted_again(test_db, clean_tables):
    a, b = _memories(2)
    _edge(a, b)
    _age_edges(120)

    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 1
    assert dream_cycle_db.expire_stale_relationships(days_threshold=90) == 0


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "90", None])
def test_threshold_must_be_a_positive_integer(test_db, clean_tables, bad):
    with pytest.raises(ValueError):
        dream_cycle_db.expire_stale_relationships(days_threshold=bad)
