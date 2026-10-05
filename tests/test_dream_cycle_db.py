"""Database round trips for the discovery ledger, using the disposable cluster."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from src import dream_cycle_db as db
from src.db import get_connection, create_memory, transaction
from src.models import CandidateInsight, EvaluatorVerdict

pytestmark = pytest.mark.usefixtures("test_schema")


def candidate(content="Grounded synthesis"):
    return asdict(CandidateInsight("A discovery", "insight", "CREATE", content=content))


def votes(pattern="AAAA"):
    roles = ("skeptic", "advocate", "epistemologist", "methodologist")
    return db.verdict_columns([EvaluatorVerdict(role, "ACCEPT" if vote == "A" else "REJECT", f"{role} reason", score=80)
                              for role, vote in zip(roles, pattern)])


def run_row(run):
    from psycopg2.extras import RealDictCursor
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM dream_cycle_runs WHERE id=%s", (run,))
        return dict(cur.fetchone())


def test_run_provenance_counts_feedback_and_known_cost_round_trip():
    provenance = {"grounding_auditor": {"backend": "bedrock", "model": "fixture"}}
    run = db.create_run("scheduled", provenance)
    db.complete_run(run, {"candidates_generated": 3, "candidates_accepted": 1, "candidates_rejected": 1,
                          "candidates_unjudged": 1, "cost_usd": .45, "aborted": True}, "digest-path",
                    explorer_output='[{"name":"slice"}]', explorer_feedback_injected="prior feedback")
    row = run_row(run)
    assert row["backend_provenance"] == provenance
    assert (row["candidates_generated"], row["candidates_accepted"], row["candidates_rejected"]) == (3, 1, 1)
    assert row["cost_usd"] == pytest.approx(.45)
    assert row["stats"]["candidates_unjudged"] == 1 and row["stats"]["aborted"]
    assert db.was_feedback_injected(run) and row["completed_at"]


def test_unknown_spend_does_not_publish_lower_bound_as_exact_cost():
    run = db.create_run("scheduled")
    db.complete_run(run, {"cost_usd": .2, "spend_unknown": True}, "")
    row = run_row(run)
    assert row["cost_usd"] is None and row["stats"]["cost_usd"] == .2


def test_candidate_four_votes_and_details_round_trip():
    run = db.create_run("scheduled")
    memory = create_memory("insight", "Accepted", "content")
    cid = db.store_candidate(run, candidate(), votes("AAAR"), "ACCEPTED", memory)
    assert str(uuid4()) != cid
    actual = db.get_evaluator_verdicts_for_run(run)["A discovery"]
    assert actual["methodologist"]["verdict"] == "REJECT"
    assert actual["skeptic"]["reasoning"] == "skeptic reason"
    db.complete_run(run, {"candidates_generated": 1, "candidates_accepted": 1}, "")
    dissent = db.get_accepted_dissents()
    assert len(dissent) == 1 and str(dissent[0]["candidate_id"]) == cid


def test_candidate_transaction_participates_without_early_commit():
    run = db.create_run("scheduled")
    with pytest.raises(RuntimeError):
        with transaction() as conn:
            db.store_candidate(run, candidate(), votes("RRRR"), "REJECTED", conn=conn)
            raise RuntimeError("rollback")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dream_cycle_candidates WHERE run_id=%s", (run,))
        assert cur.fetchone()[0] == 0


def test_rejection_feedback_and_user_rejection_are_distinct():
    run = db.create_run("scheduled")
    rejected = db.store_candidate(run, candidate(), votes("ARRR"), "REJECTED")
    accepted = db.store_candidate(run, candidate("other"), votes(), "ACCEPTED")
    db.complete_run(run, {"candidates_generated": 2, "candidates_rejected": 1, "candidates_accepted": 1}, "")
    db.mark_user_rejected(accepted, "Already known")
    assert any(str(row["candidate_id"]) == rejected for row in db.get_recent_rejections())
    assert any(row["user_rejection_reason"] == "Already known" for row in db.get_user_rejections())


def test_briefing_policy_requires_elapsed_day_and_activity():
    assert db.should_run_briefing()
    run = db.create_run("session_start")
    db.complete_run(run, {}, "")
    assert not db.should_run_briefing()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE dream_cycle_runs SET completed_at=now()-interval '2 days' WHERE id=%s", (run,))
        conn.commit()
    assert not db.should_run_briefing()
    create_memory("source", "New arrival", "text")
    assert db.should_run_briefing()


def test_empty_metrics_and_memory_stats_have_stable_shapes():
    assert db.get_memory_stats()["total_count"] == 0
    assert isinstance(db.get_tier1_metrics(), dict)
    assert isinstance(db.get_tier2_metrics(), dict)
    assert db.get_strategy_usage() == {}


def test_invalid_model_target_is_preserved_in_json_but_not_sql_uuid():
    run = db.create_run("scheduled")
    c = candidate() | {"operation": "UPDATE", "target_memory_id": "hallucinated"}
    cid = db.store_candidate(run, c, votes(), "BLOCKED")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT candidate_json, target_memory_id FROM dream_cycle_candidates WHERE id=%s", (cid,))
        raw, target = cur.fetchone()
    assert raw["target_memory_id"] == "hallucinated" and target is None
