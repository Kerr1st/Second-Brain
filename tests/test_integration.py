"""Native discovery integration with scripted providers and real isolated SQL.

The suite executes allocation, prompts, grounding, metering, consensus, atomic
storage, provenance, feedback and digest generation. Synthetic votes qualify the
pipeline contract; they are not evidence of model quality or human benefit.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from psycopg2.extras import RealDictCursor

from src import db, dream_cycle_db
from src.dream_cycle import DreamCycleOrchestrator
from tests.conftest import _deterministic_embedding

ACCEPTED_TITLE = "Reuse workshop readiness checks"
REJECTED_TITLE = "Unsupported adoption guarantee"
ROLES = ("skeptic", "advocate", "epistemologist", "methodologist")


def sql_rows(sql, params=()):
    with db.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


@pytest.fixture
def pipeline(test_schema, monkeypatch, tmp_path):
    from src.backends.bedrock import BedrockInvoker
    from src.backends.claude_code import ClaudeCodeInvoker
    monkeypatch.setenv("SECOND_BRAIN_PROFILE", "controlled")
    monkeypatch.setenv("SECOND_BRAIN_RUN_COST_CAP_USD", "9")
    monkeypatch.setenv("SECOND_BRAIN_DIGEST_DIR", str(tmp_path / "digests"))
    monkeypatch.setattr("src.skim.store.get_connection", db.get_connection)
    monkeypatch.setattr("src.embeddings.generate_embedding", _deterministic_embedding)
    monkeypatch.setattr("src.dream_cycle.storage.generate_embedding", _deterministic_embedding)
    sources = []
    for project, content in [
        ("workshop", "The workshop validates prerequisites before implementation and checks results afterward."),
        ("adoption", "The adoption team needs a readiness check before starting its pilot."),
    ]:
        mid = db.create_memory("source", project.title(), content,
            embedding=_deterministic_embedding(content), source_type="article",
            source_url=f"test://{project}", project=project)
        sources.append(db.get_memory(mid))
    ids = [str(row["id"]) for row in sources]
    calls = []

    def invoke(self, system_prompt, user_message, **kwargs):
        stage = kwargs["stage"]
        calls.append({"stage": stage, "system": system_prompt, "user": user_message})
        if stage == "skim":
            output = {"summary": "A project discusses pilot readiness.", "people": [], "entities": [],
                "topics": ["pilot"], "decisions": [], "open_questions": [], "novelty": 1,
                "salience": 2, "contradiction_hint": None, "doc_kind": "note"}
        elif stage == "explorer":
            output = [{"name": "Cross-project readiness", "strategy": "cross_project_collision",
                "memory_ids": ids, "memory_titles": [row["title"] for row in sources],
                "hypothesis": "The adoption pilot could adapt workshop readiness checks."}]
        elif stage == "thinker":
            packet = json.loads(user_message)["memory_slice"]
            assert set(packet["memory_ids"]) == set(ids)
            output = []
            for title, content in [
                (ACCEPTED_TITLE, "The adoption team could adapt the workshop prerequisite and outcome checks for pilot readiness."),
                (REJECTED_TITLE, "Reusing the workshop checks guarantees every adopter will double productivity."),
            ]:
                output.append({"title": title, "type": "insight", "operation": "CREATE",
                    "content": content, "source_memories": ids, "confidence": "medium",
                    "confidence_reasoning": "Synthetic candidate for pipeline qualification.",
                    "schema_operation": "assimilation", "schema_note": "Cross-project reuse",
                    "relationships": [{"target_id": ids[0], "relation_type": "supports", "note": "Readiness mechanism"}],
                    "strategy_that_found_it": "cross_project_collision"})
        elif stage == "grounding_audit" or stage.startswith("evaluator:"):
            rejected = REJECTED_TITLE in system_prompt and stage in {"evaluator:epistemologist", "evaluator:methodologist"}
            output = {"verdict": "REJECT" if rejected else "ACCEPT", "score": 30 if rejected else 85,
                "reasoning": "Synthetic rejection of unsupported guarantee." if rejected else "Synthetic contract vote.",
                "blocking_issues": [], "advisory_issues": []}
        else:
            raise AssertionError(f"Unexpected provider stage: {stage}")
        return {"output": output, "raw": json.dumps(output), "usage_source": "real",
                "usage": {"inputTokens": 100, "outputTokens": 50}}

    monkeypatch.setattr(BedrockInvoker, "invoke", invoke)
    monkeypatch.setattr(ClaudeCodeInvoker, "invoke", invoke)
    # Observe real transaction participants; do not replace persistence with mocks.
    original_create = db.create_memory
    create = Mock(wraps=original_create)
    monkeypatch.setattr("src.dream_cycle.storage.create_memory", create)
    return {"sources": sources, "ids": ids, "calls": calls, "create": create,
            "run": lambda: DreamCycleOrchestrator().run("user_triggered", scope={"task": "Improve pilot readiness"})}


def run_rows(result):
    runs = sql_rows("SELECT * FROM dream_cycle_runs WHERE id=%s", (result.run_id,))
    candidates = sql_rows("SELECT * FROM dream_cycle_candidates WHERE run_id=%s ORDER BY created_at,id", (result.run_id,))
    assert len(runs) == 1
    return runs[0], {row["candidate_json"]["title"]: row for row in candidates}


def assert_votes(row, expected):
    for seat, vote in zip("abcd", expected):
        assert row[f"evaluator_{seat}_verdict"] == vote
        assert row[f"evaluator_{seat}_reasoning"]


def test_native_pipeline_persists_real_votes_memory_provenance_and_digest(pipeline):
    result = pipeline["run"]()
    assert not result.aborted_early
    assert (result.candidates_generated, result.candidates_accepted, result.candidates_rejected) == (2, 1, 1)
    assert result.run_type == "user_triggered" and result.cost_usd > 0 and not result.spend_unknown
    run, candidates = run_rows(result)
    assert run["completed_at"] and run["cost_usd"] > 0
    assert run["candidates_generated"] == 2
    assert run["candidates_accepted"] == run["candidates_rejected"] == 1
    assert run["backend_provenance"]["skeptic"]["backend"] == "bedrock"  # grounding audit runs on the skeptic seat (R1)
    accepted, rejected = candidates[ACCEPTED_TITLE], candidates[REJECTED_TITLE]
    assert accepted["final_verdict"] == "ACCEPTED" and accepted["created_memory_id"]
    assert rejected["final_verdict"] == "REJECTED" and rejected["created_memory_id"] is None
    assert_votes(accepted, ["ACCEPT"] * 4)
    assert_votes(rejected, ["ACCEPT", "ACCEPT", "REJECT", "REJECT"])

    memory = db.get_memory(str(accepted["created_memory_id"]))
    assert memory["title"] == ACCEPTED_TITLE and memory["metadata"]["dream_cycle"] is True
    assert "dream-cycle" in memory["tags"]
    assert set(memory["metadata"]["source_memories"]) == set(pipeline["ids"])
    assert memory["metadata"]["strategy"] == "cross_project_collision"
    assert not sql_rows("SELECT id FROM memories WHERE title=%s", (REJECTED_TITLE,))
    edges = sql_rows("SELECT * FROM memory_relationships WHERE source_id=%s", (memory["id"],))
    assert {(str(row["target_id"]), row["relation_type"]) for row in edges} == {
        (mid, "derived_from") for mid in pipeline["ids"]} | {(pipeline["ids"][0], "supports")}
    pipeline["create"].assert_called_once()
    assert pipeline["create"].call_args.kwargs["conn"] is not None
    for original in pipeline["sources"]:
        assert db.get_memory(str(original["id"]))["content"] == original["content"]

    stages = [call["stage"] for call in pipeline["calls"]]
    assert stages.count("skim") == 2
    assert stages.count("explorer") == stages.count("thinker") == 1
    assert stages.count("grounding_audit") == 2
    assert all(stages.count(f"evaluator:{role}") == 2 for role in ROLES)
    thinker_input = next(call["user"] for call in pipeline["calls"] if call["stage"] == "thinker")
    assert all(source["content"] in thinker_input for source in pipeline["sources"])
    digest = Path(run["digest"]).read_text()
    assert ACCEPTED_TITLE in digest and REJECTED_TITLE not in digest
    assert "- Rejected: 1" in digest
    assert len(dream_cycle_db.get_recent_rejections()) == 1
    assert dream_cycle_db.get_recent_rejections()[0]["candidate_id"] == rejected["id"]


def test_failure_after_accepted_ledger_insert_rolls_back_every_discovery_write(pipeline, monkeypatch):
    original_store = dream_cycle_db.store_candidate
    entered_accepted = []
    def fail_after_insert(*args, **kwargs):
        cid = original_store(*args, **kwargs)
        if args[3] == "ACCEPTED":
            assert kwargs.get("conn") is not None
            entered_accepted.append(cid)
            raise ValueError("Synthetic failure after the accepted ledger insert")
        return cid
    monkeypatch.setattr(dream_cycle_db, "store_candidate", fail_after_insert)
    result = pipeline["run"]()
    assert not result.aborted_early
    assert len(entered_accepted) == 1
    assert (result.candidates_generated, result.candidates_accepted, result.candidates_rejected) == (2, 0, 1)
    run, candidates = run_rows(result)
    assert run["completed_at"] and run["stats"]["candidates_storage_failed"] == 1
    blocked = candidates[ACCEPTED_TITLE]
    assert blocked["final_verdict"] == "BLOCKED" and blocked["created_memory_id"] is None
    assert blocked["candidate_json"]["storage_error"] == "ValueError"
    assert_votes(blocked, ["ACCEPT"] * 4)
    assert not sql_rows("SELECT id FROM memories WHERE metadata->>'dream_cycle'='true'")
    assert not sql_rows("SELECT * FROM memory_relationships")
    assert not sql_rows("SELECT id FROM dream_cycle_candidates WHERE id=%s", (entered_accepted[0],))
    assert len(db.list_memories()) == len(pipeline["sources"])


def test_repeated_accepted_discovery_reuses_authoritative_memory_with_new_ledger(pipeline):
    first = pipeline["run"]()
    second = pipeline["run"]()
    assert not first.aborted_early and not second.aborted_early
    assert first.candidates_accepted == second.candidates_accepted == 1
    _, first_candidates = run_rows(first)
    _, second_candidates = run_rows(second)
    assert first_candidates[ACCEPTED_TITLE]["created_memory_id"] == second_candidates[ACCEPTED_TITLE]["created_memory_id"]
    assert first_candidates[ACCEPTED_TITLE]["id"] != second_candidates[ACCEPTED_TITLE]["id"]
    assert len(sql_rows("SELECT id FROM memories WHERE metadata->>'dream_cycle'='true'")) == 1
    assert len(sql_rows("SELECT * FROM memory_relationships")) == 3
    pipeline["create"].assert_called_once()
    second_run, _ = run_rows(second)
    assert second_run["explorer_feedback_injected"]
