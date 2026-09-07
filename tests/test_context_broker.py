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


def _topic_history(*, later_content='The user removed the earlier restriction.'):
    """Small database case for the observed older-decision/later-correction pattern."""
    from src.db import create_relationship
    segment = create_memory(type='source', title='One topic', content='Original ordered turns',
        source_type='codex_task', metadata={'record_kind':'topic_segment','turn_ids':['first','second','third'], 'task_source_url':'codex://one'})
    ids = []
    for kind, title, content, turn in (
        ('decision','Earlier plan','Keep the original restriction.', 'first'),
        ('correction_episode','User correction',later_content, 'second'),
        ('insight','Status update','The user completed the action.', 'third'),
    ):
        mid = create_memory(type=kind,title=title,content=content,embedding=VECTOR,
            source_type='distilled_agent_task', metadata={'record_kind':'task_memory',
                'task_source_url':'codex://one','supporting_turn_ids':[turn]})
        create_relationship(mid,segment,'derived_from')
        ids.append(mid)
    return segment,ids


def test_old_search_hit_delivers_later_topic_evidence_first(test_db, clean_tables):
    from src.context_broker import ContextRequest, build_context
    from src.db import get_memory
    _, (old, correction, completed) = _topic_history()
    # Force the same retrieval boundary as production: only the old summary hit.
    with patch('src.context_broker.generate_embedding',return_value=VECTOR), \
         patch('src.context_broker.retrieve_memories',return_value=[get_memory(old)]):
        pack=build_context(ContextRequest('Continue the earlier plan',budget_tokens=700))
    assert [i.memory_id for i in pack.items] == [completed,correction,old]
    assert [i.authority for i in pack.items] == ['inferred','evidence','inferred']
    assert 'historical' in pack.items[-1].retrieval_reason.lower()


def test_cannot_pack_old_claim_after_skipping_large_later_correction(test_db, clean_tables):
    from src.context_broker import ContextRequest, build_context
    from src.db import get_memory
    _, (old, correction, completed) = _topic_history(later_content='Correction evidence. '*200)
    with patch('src.context_broker.generate_embedding',return_value=VECTOR), \
         patch('src.context_broker.retrieve_memories',return_value=[get_memory(old)]):
        pack=build_context(ContextRequest('Continue the earlier plan',budget_tokens=150))
    assert old not in [i.memory_id for i in pack.items]
    assert pack.token_count <= 150


def test_topic_expansion_respects_status_project_and_exact_segment(test_db, clean_tables):
    from src.context_broker import ContextRequest, build_context
    from src.db import create_relationship, get_memory, update_memory
    segment, (old, correction, completed) = _topic_history()
    update_memory(correction,status='superseded')
    update_memory(completed,project='different-project')
    unrelated=create_memory(type='insight',title='Another topic in the same task',
        content='A distinct topic must not be pulled in through workspace or task identity.',
        source_type='distilled_agent_task',metadata={'record_kind':'task_memory',
        'task_source_url':'codex://one','supporting_turn_ids':['another']})
    another_segment=create_memory(type='source',title='Other topic',content='Other topic',
        source_type='codex_task',metadata={'record_kind':'topic_segment',
        'turn_ids':['another'],'task_source_url':'codex://one'})
    create_relationship(unrelated,another_segment,'derived_from')
    with patch('src.context_broker.generate_embedding',return_value=VECTOR), \
         patch('src.context_broker.retrieve_memories',return_value=[get_memory(old)]):
        pack=build_context(ContextRequest('Continue the plan',project_hint='second-brain'))
    assert [i.memory_id for i in pack.items] == [old]


def test_processing_time_is_not_presented_as_source_observation_time(test_db, clean_tables):
    from src.context_broker import ContextRequest, build_context
    from src.db import get_memory
    _, (old, _, _) = _topic_history()
    with patch('src.context_broker.generate_embedding',return_value=VECTOR), \
         patch('src.context_broker.retrieve_memories',return_value=[get_memory(old)]):
        pack=build_context(ContextRequest('Continue the earlier plan'))
    # The database just created these rows, but their source turns have no date.
    assert all(item.observed_at is None for item in pack.items)


@pytest.mark.parametrize("applicability,repository,expected", [
    ({}, None, True),
    ({"integrations": ["codex"], "semantic_projects": ["second-brain"]}, None, True),
    ({"integrations": ["kiro"]}, None, False),
    ({"semantic_projects": ["job-search"]}, None, False),
    ({"repositories": ["/approved/repo"]}, "/other/repo", False),
    ({"repositories": ["/approved/repo"]}, None, False),
    ({"repositories": ["/approved/repo"]}, "/approved/repo", True),
    ({"topics": ["unrelated-topic"]}, None, False),
    ({"topics": ["capture"]}, None, True),
    ("malformed-scope", None, False),
], ids=["unrestricted", "matching", "other-integration", "other-project", "other-repo",
        "unknown-repo", "matching-repo", "other-topic", "matching-topic", "malformed"])
def test_guidance_delivery_respects_approved_applicability(
    test_db, clean_tables, applicability, repository, expected,
):
    from src.context_broker import ContextRequest, build_context

    rule = create_memory(
        type="steering_rule", title="Capture guidance", content="Preserve exact provenance.",
        embedding=VECTOR, project="second-brain",
        metadata={"authority": "approved", "applicability": applicability},
    )
    with patch("src.context_broker.generate_embedding", return_value=VECTOR):
        pack = build_context(ContextRequest(
            objective="Plan capture", project_hint="second-brain", repository=repository,
        ))
    assert (rule in [item.memory_id for item in pack.items]) is expected
