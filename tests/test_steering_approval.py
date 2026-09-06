"""Approval transaction guarantees against the disposable PostgreSQL test DB."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
import time
from unittest.mock import patch

import psycopg2
from psycopg2 import sql
import pytest

from src.db import create_memory, get_connection, get_memory, get_relationships, list_memories
from src.steering import approve_steering_candidate


VECTOR = [0.02] * 1024
APPROVAL = dict(
    wording="Keep reviewed guidance consistent.",
    authority_scope="project",
    applicability={"semantic_projects": ["second-brain"]},
)


def _candidate(supersedes=None):
    return create_memory(
        type="steering_candidate",
        title="Approval transaction fixture",
        content=APPROVAL["wording"],
        metadata={
            "authority": "inferred",
            "lifecycle": "proposed",
            "source_memory_ids": [],
            "supersedes_rule_id": supersedes,
        },
    )


def _approve(candidate_id):
    with patch("src.steering.generate_embedding", return_value=VECTOR):
        return approve_steering_candidate(candidate_id, **APPROVAL)


@pytest.mark.parametrize(
    "table,condition",
    [
        ("memories", "type <> 'steering_rule'"),
        ("memory_relationships", "relation_type <> 'derived_from'"),
        ("memories", "status <> 'superseded'"),
        ("memory_relationships", "relation_type <> 'superseded_by'"),
        ("memories", "type <> 'steering_candidate' OR metadata->>'lifecycle' <> 'approved'"),
    ],
    ids=["rule-insert", "provenance-link", "previous-rule", "supersession-link", "candidate"],
)
def test_approval_failure_rolls_back_every_write(test_db, clean_tables, table, condition):
    previous = _approve(_candidate())
    candidate_id = _candidate(previous.rule_id)
    before_candidate = get_memory(candidate_id)
    before_previous = get_memory(previous.rule_id)
    before_links = get_relationships(previous.rule_id)

    # NOT VALID preserves existing fixtures but rejects the selected new write.
    # These constraints exist only in the disposable test database.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL(
                "ALTER TABLE {} ADD CONSTRAINT approval_failure CHECK ({}) NOT VALID"
            ).format(sql.Identifier(table), sql.SQL(condition)))
        conn.commit()
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _approve(candidate_id)

        assert get_memory(candidate_id) == before_candidate
        assert get_memory(previous.rule_id) == before_previous
        assert get_relationships(previous.rule_id) == before_links
        assert {str(row["id"]) for row in list_memories(type="steering_rule")} == {
            previous.rule_id
        }
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT approval_failure").format(
                    sql.Identifier(table)
                ))
            conn.commit()

    # A failed approval remains retryable, with no skipped version or orphan rule.
    result = _approve(candidate_id)
    assert result.version == 2
    assert get_memory(previous.rule_id)["status"] == "superseded"
    assert get_memory(candidate_id)["metadata"]["approved_rule_id"] == result.rule_id
    assert {str(row["id"]) for row in list_memories(type="steering_rule", status="active")} == {
        result.rule_id
    }
    assert {(str(row["source_id"]), str(row["target_id"]), row["relation_type"])
            for row in get_relationships(result.rule_id)} == {
        (result.rule_id, candidate_id, "derived_from"),
        (previous.rule_id, result.rule_id, "superseded_by"),
    }


def test_embedding_failure_changes_no_governance_state(test_db, clean_tables):
    candidate_id = _candidate()
    before = get_memory(candidate_id)
    with patch("src.steering.generate_embedding", side_effect=RuntimeError("embedding unavailable")):
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            approve_steering_candidate(candidate_id, **APPROVAL)
    assert get_memory(candidate_id) == before
    assert list_memories(type="steering_rule") == []


@pytest.mark.parametrize("competing_successors", [False, True], ids=["same-candidate", "same-predecessor"])
def test_competing_approvals_have_one_winner(test_db, clean_tables, competing_successors):
    previous = _approve(_candidate()) if competing_successors else None
    first_id = _candidate(previous.rule_id if previous else None)
    second_id = _candidate(previous.rule_id) if previous else first_id
    lock_id = previous.rule_id if previous else first_id
    embedding_barrier = Barrier(2)

    @contextmanager
    def independent_connection():
        # Each caller represents a separate CLI process. Do not share the
        # application's single-threaded connection pool between test workers.
        conn = psycopg2.connect(**test_db, application_name="steering-approval-test")
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '10s'")
                yield conn
        finally:
            conn.close()

    def embed(_wording):
        embedding_barrier.wait(timeout=10)
        return VECTOR

    def approve(candidate_id):
        try:
            return approve_steering_candidate(candidate_id, **APPROVAL)
        except ValueError as exc:
            return exc

    blocker = psycopg2.connect(**test_db)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT id FROM memories WHERE id = %s FOR UPDATE", (lock_id,))
        with patch("src.db.get_connection", independent_connection), patch(
            "src.steering.get_connection", independent_connection, create=True
        ), patch("src.steering.generate_embedding", side_effect=embed):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(approve, candidate_id) for candidate_id in (first_id, second_id)]
                try:
                    # Wait for actual PostgreSQL lock contention, not a timing guess.
                    deadline = time.monotonic() + 10
                    while True:
                        with blocker.cursor() as cur:
                            cur.execute("SELECT pg_stat_clear_snapshot()")
                            cur.execute("""
                                SELECT count(*) FROM pg_stat_activity
                                WHERE application_name = 'steering-approval-test'
                                  AND wait_event_type = 'Lock'
                            """)
                            waiting = cur.fetchone()[0]
                        if waiting == 2:
                            break
                        assert time.monotonic() < deadline, "approval workers did not contend on row locks"
                        time.sleep(0.01)
                    with blocker.cursor() as cur:
                        cur.execute("SELECT count(*) FROM memories WHERE type = 'steering_rule'")
                        assert cur.fetchone()[0] == int(previous is not None)
                finally:
                    blocker.rollback()
                outcomes = [future.result(timeout=15) for future in futures]
    finally:
        blocker.close()

    winners = [outcome for outcome in outcomes if not isinstance(outcome, ValueError)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, ValueError)]
    assert len(winners) == len(losers) == 1
    winner = winners[0]
    assert winner.version == (2 if previous else 1)
    assert get_memory(winner.candidate_id)["metadata"]["approved_rule_id"] == winner.rule_id
    assert {str(row["id"]) for row in list_memories(type="steering_rule", status="active")} == {
        winner.rule_id
    }
    if previous:
        assert get_memory(previous.rule_id)["status"] == "superseded"
        losing_id = second_id if winner.candidate_id == first_id else first_id
        assert get_memory(losing_id)["metadata"]["lifecycle"] == "proposed"
        assert "supersession requires an active Steering Rule" in str(losers[0])
    else:
        assert "only a proposed Steering Candidate can be approved" in str(losers[0])

    with pytest.raises(ValueError, match="only a proposed Steering Candidate can be approved"):
        _approve(winner.candidate_id)
