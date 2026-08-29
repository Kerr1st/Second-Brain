"""Behavior tests for the task-ready Memory Context Broker."""

from unittest.mock import patch

import pytest

from src.db import create_memory


VECTOR = [0.01] * 1024


def test_codex_receives_approved_guidance_before_relevant_evidence(
    test_db, clean_tables
):
    rule_id = create_memory(
        type="steering_rule",
        title="Prove one vertical slice first",
        content=(
            "Take one capability through Codex capture, processing, delivery, and "
            "outcome evaluation before generalizing it to other agent integrations."
        ),
        embedding=VECTOR,
        source_type="steering_governance",
        mem_class="procedural",
        project="second-brain",
        metadata={
            "authority": "approved",
            "authority_scope": "project",
            "applicability": {
                "integrations": ["codex"],
                "semantic_projects": ["second-brain"],
            },
            "rule_version": 1,
        },
    )
    evidence_id = create_memory(
        type="decision",
        title="Use Codex as the reference integration",
        content="Codex is the first integration used to prove the complete learning loop.",
        embedding=VECTOR,
        source_type="distilled_agent_task",
        mem_class="semantic",
        project="second-brain",
        metadata={"supporting_turn_ids": ["turn-1"]},
    )

    from src.context_broker import ContextRequest, build_context, get_context_receipt

    with patch("src.context_broker.generate_embedding", return_value=VECTOR):
        pack = build_context(
            ContextRequest(
                objective="Build the Codex vertical slice for Second Brain",
                project_hint="second-brain",
                source_system="codex",
                budget_tokens=400,
            )
        )

    assert [item.memory_id for item in pack.items[:2]] == [rule_id, evidence_id]
    assert [item.authority for item in pack.items[:2]] == ["approved", "inferred"]
    assert pack.token_count <= 400
    receipt = get_context_receipt(pack.receipt_id)
    assert receipt["returned_memory_ids"][:2] == [rule_id, evidence_id]
    assert receipt["outcome"] == "pending"


def test_follow_up_records_which_guidance_was_used(test_db, clean_tables):
    memory_id = create_memory(
        type="steering_rule",
        title="Keep the proof vertical",
        content="Prove one integration end to end before expanding.",
        embedding=VECTOR,
        status="active",
        metadata={"authority": "approved", "applicability": {}},
    )
    from src.context_broker import (
        ContextRequest,
        build_context,
        get_context_receipt,
        record_context_outcome,
    )

    with patch("src.context_broker.generate_embedding", return_value=VECTOR):
        pack = build_context(ContextRequest(objective="Plan an agent integration"))

    record_context_outcome(
        pack.receipt_id,
        used_memory_ids=[memory_id],
        outcome="followed",
        note="The task selected Codex and completed the full slice before expansion.",
    )

    receipt = get_context_receipt(pack.receipt_id)
    assert receipt["used_memory_ids"] == [memory_id]
    assert receipt["outcome"] == "followed"
    assert receipt["evaluated_at"] is not None


def test_corrected_outcome_requires_an_actual_correction_episode(
    test_db, clean_tables
):
    memory_id = create_memory(
        type="decision",
        title="Returned guidance",
        content="Use the reference integration first.",
        embedding=VECTOR,
    )
    from src.context_broker import (
        ContextRequest,
        build_context,
        record_context_outcome,
    )

    with patch("src.context_broker.generate_embedding", return_value=VECTOR):
        pack = build_context(ContextRequest(objective="Use the reference integration"))

    with pytest.raises(ValueError, match="must identify a Correction Episode"):
        record_context_outcome(
            pack.receipt_id,
            used_memory_ids=[memory_id],
            outcome="corrected",
            correction_episode_id=memory_id,
        )
