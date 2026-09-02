"""Behavior tests for consensus-gated steering governance."""

from unittest.mock import patch

from src.db import create_memory
from src.models import EvaluatorVerdict


VECTOR = [0.02] * 1024


def _verdict(role: str, verdict: str) -> EvaluatorVerdict:
    return EvaluatorVerdict(role=role, verdict=verdict, reasoning=f"{role} review")


def test_dream_cycle_quorum_retains_candidate_without_activating_it(
    test_db, clean_tables
):
    episode_id = create_memory(
        type="correction_episode",
        title="Generalized before proving the path",
        content=(
            "Misalignment: The implementation expanded across integrations before a complete proof.\n\n"
            "Corrected expectation: The user indicated one Codex path should be proven end to end first."
        ),
        embedding=VECTOR,
        source_type="distilled_agent_task",
        mem_class="episodic",
        metadata={"supporting_turn_ids": ["answer-1", "prompt-2"]},
    )

    from src.steering import SteeringProposal, review_steering_candidate

    proposal = SteeringProposal(
        title="Prove one vertical slice before horizontal rollout",
        wording=(
            "Prove a capability through one reference integration's complete lifecycle "
            "before generalizing it to other integrations."
        ),
        source_memory_ids=(episode_id,),
        proposed_authority_scope="project",
        proposed_applicability={"integrations": ["codex"]},
    )
    verdicts = (
        _verdict("skeptic", "ACCEPT"),
        _verdict("advocate", "ACCEPT"),
        _verdict("epistemologist", "ACCEPT"),
        _verdict("methodologist", "REJECT"),
    )

    with patch("src.steering.generate_embedding", return_value=VECTOR):
        result = review_steering_candidate(proposal, verdicts)

    assert result.final_verdict == "ACCEPTED"
    assert result.candidate_id is not None
    assert result.active_rule_id is None
    assert result.lifecycle == "proposed"


def test_user_approval_versions_and_supersedes_rules(test_db, clean_tables):
    evidence_id = create_memory(
        type="decision",
        title="Vertical proof policy",
        content="The user made the vertical proof the standard delivery policy.",
        embedding=VECTOR,
        mem_class="semantic",
    )
    from src.steering import (
        SteeringProposal,
        approve_steering_candidate,
        get_steering_rule,
        review_steering_candidate,
    )

    accepts = tuple(
        _verdict(role, "ACCEPT")
        for role in ("skeptic", "advocate", "epistemologist", "methodologist")
    )
    with patch("src.steering.generate_embedding", return_value=VECTOR):
        first_review = review_steering_candidate(
            SteeringProposal(
                title="Prove vertically",
                wording="Prove one complete Codex slice before generalizing.",
                source_memory_ids=(evidence_id,),
            ),
            accepts,
        )
        first_rule = approve_steering_candidate(
            first_review.candidate_id,
            wording="Prove one complete Codex slice before generalizing.",
            authority_scope="project",
            applicability={
                "integrations": ["codex"],
                "semantic_projects": ["second-brain"],
            },
        )
        second_review = review_steering_candidate(
            SteeringProposal(
                title="Prove through outcome evaluation",
                wording=(
                    "Prove one complete Codex slice through recorded outcome evaluation "
                    "before generalizing."
                ),
                source_memory_ids=(evidence_id,),
                supersedes_rule_id=first_rule.rule_id,
            ),
            accepts,
        )
        second_rule = approve_steering_candidate(
            second_review.candidate_id,
            wording=(
                "Prove one complete Codex slice through recorded outcome evaluation "
                "before generalizing."
            ),
            authority_scope="project",
            applicability={
                "integrations": ["codex"],
                "semantic_projects": ["second-brain"],
            },
        )

    assert first_rule.version == 1
    assert second_rule.version == 2
    assert second_rule.supersedes_rule_id == first_rule.rule_id
    assert get_steering_rule(second_rule.rule_id).wording.endswith("before generalizing.")


def test_dream_cycle_evaluates_steering_with_four_independent_roles(
    test_db, clean_tables
):
    evidence_id = create_memory(
        type="decision",
        title="Delivery policy",
        content="The user explicitly made vertical proof the project standard.",
        embedding=VECTOR,
    )
    calls = []

    class FakeInvoker:
        def __init__(self, role):
            self.role = role

        def invoke(self, **kwargs):
            calls.append((self.role, kwargs["system_prompt"]))
            verdict = "REJECT" if self.role == "methodologist" else "ACCEPT"
            return {"output": {"verdict": verdict, "reasoning": f"{self.role} evidence review"}}

    class FakeResolver:
        def invoker_for(self, role):
            return FakeInvoker(role)

    from src.steering import SteeringProposal, evaluate_steering_proposal

    with patch("src.steering.generate_embedding", return_value=VECTOR):
        result = evaluate_steering_proposal(
            SteeringProposal(
                title="Vertical proof",
                wording="Prove the Codex path end to end before generalizing.",
                source_memory_ids=(evidence_id,),
            ),
            resolver=FakeResolver(),
        )

    assert result.final_verdict == "ACCEPTED"
    assert [role for role, _ in calls] == [
        "skeptic",
        "advocate",
        "epistemologist",
        "methodologist",
    ]
    assert all("Steering Candidate" in prompt for _, prompt in calls)
