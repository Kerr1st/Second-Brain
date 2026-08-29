"""Consensus-gated Steering Candidate and approved Steering Rule governance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import re
from typing import Any

from src import dream_cycle_db
from src.db import (
    create_memory,
    create_relationship,
    get_memory,
    list_memories,
    update_memory,
)
from src.dream_cycle.consensus import tally_consensus
from src.embeddings import generate_embedding
from src.models import EvaluatorVerdict
from src.backends.resolver import default_resolver
from src.project import normalize_project_tag


_SCOPES = {"project", "personal", "system"}


@dataclass(frozen=True, slots=True)
class SteeringProposal:
    title: str
    wording: str
    source_memory_ids: tuple[str, ...]
    proposed_authority_scope: str | None = None
    proposed_applicability: dict[str, list[str]] | None = None
    supersedes_rule_id: str | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.wording.strip():
            raise ValueError("a steering proposal needs a title and wording")
        if not self.source_memory_ids:
            raise ValueError("a steering proposal needs source evidence")
        if (
            self.proposed_authority_scope is not None
            and self.proposed_authority_scope not in _SCOPES
        ):
            raise ValueError("proposed authority scope must be project, personal, or system")


@dataclass(frozen=True, slots=True)
class SteeringReviewResult:
    run_id: str
    final_verdict: str
    candidate_id: str | None
    active_rule_id: str | None
    lifecycle: str
    duplicate_of: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovedSteeringRule:
    rule_id: str
    candidate_id: str
    wording: str
    authority_scope: str
    applicability: dict[str, list[str]]
    version: int
    supersedes_rule_id: str | None


def _normalized_wording(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def find_matching_steering(wording: str) -> str | None:
    """Suppress exact settled or pending duplicates before panel work."""
    normalized = _normalized_wording(wording)
    for kind in ("steering_rule", "steering_candidate"):
        for memory in list_memories(type=kind, limit=500):
            if _normalized_wording(memory.get("content") or "") == normalized:
                return str(memory["id"])
    return None


def _candidate_metadata(proposal: SteeringProposal, verdicts) -> dict[str, Any]:
    return {
        "authority": "inferred",
        "lifecycle": "proposed",
        "source_memory_ids": list(proposal.source_memory_ids),
        "proposed_authority_scope": proposal.proposed_authority_scope,
        "proposed_applicability": proposal.proposed_applicability or {},
        "supersedes_rule_id": proposal.supersedes_rule_id,
        "panel_verdicts": [asdict(verdict) for verdict in verdicts],
    }


def review_steering_candidate(
    proposal: SteeringProposal,
    verdicts: tuple[EvaluatorVerdict, ...],
) -> SteeringReviewResult:
    """Apply Dream Cycle quorum and retain an accepted recommendation as inactive."""
    for memory_id in proposal.source_memory_ids:
        if get_memory(memory_id) is None:
            raise ValueError(f"source memory {memory_id} does not exist")
    if proposal.supersedes_rule_id is not None:
        target = get_memory(proposal.supersedes_rule_id)
        if target is None or target.get("type") != "steering_rule":
            raise ValueError("a supersession proposal needs an existing Steering Rule")

    duplicate = find_matching_steering(proposal.wording)
    if duplicate is not None:
        return SteeringReviewResult(
            run_id="",
            final_verdict="DUPLICATE",
            candidate_id=None,
            active_rule_id=None,
            lifecycle="suppressed",
            duplicate_of=duplicate,
        )

    final = tally_consensus(list(verdicts))
    run_id = dream_cycle_db.create_run("steering_review")
    candidate_id = None
    if final == "ACCEPTED":
        candidate_id = create_memory(
            type="steering_candidate",
            title=proposal.title,
            content=proposal.wording,
            embedding=generate_embedding(proposal.wording),
            tags=["dream-cycle", "steering"],
            source_type="steering_governance",
            status="active",
            mem_class="procedural",
            metadata=_candidate_metadata(proposal, verdicts),
        )
        for source_id in proposal.source_memory_ids:
            create_relationship(candidate_id, source_id, "derived_from")

    verdict_fields: dict[str, str] = {}
    slots = ("a", "b", "c", "d")
    for slot, verdict in zip(slots, verdicts, strict=True):
        verdict_fields[f"evaluator_{slot}_verdict"] = verdict.verdict
        verdict_fields[f"evaluator_{slot}_reasoning"] = verdict.reasoning
    dream_cycle_db.store_candidate(
        run_id,
        {
            "title": proposal.title,
            "type": "steering_candidate",
            "operation": "SUPERSEDE" if proposal.supersedes_rule_id else "CREATE",
            "target_memory_id": proposal.supersedes_rule_id,
            "content": proposal.wording,
            "source_memories": list(proposal.source_memory_ids),
            "proposed_authority_scope": proposal.proposed_authority_scope,
            "proposed_applicability": proposal.proposed_applicability or {},
        },
        verdict_fields,
        final,
        candidate_id,
    )
    dream_cycle_db.complete_run(
        run_id,
        stats={
            "candidates_generated": 1,
            "candidates_accepted": int(final == "ACCEPTED"),
            "candidates_rejected": int(final == "REJECTED"),
        },
        digest=f"Steering review: {proposal.title} — {final}",
    )
    return SteeringReviewResult(
        run_id=run_id,
        final_verdict=final,
        candidate_id=candidate_id,
        active_rule_id=None,
        lifecycle="proposed" if final == "ACCEPTED" else "rejected",
    )


_STEERING_CRITERIA = {
    "skeptic": (
        "Test source fidelity, overgeneralization, conflicts with current approved rules, "
        "and whether the proposed wording claims more than the user established."
    ),
    "advocate": (
        "Test whether the rule will help the user, whether its burden is proportionate, "
        "and whether its proposed scope and applicability are useful rather than intrusive."
    ),
    "epistemologist": (
        "Test evidence sufficiency, attribution to the user, durability, and whether the "
        "candidate distinguishes observed evidence from inferred guidance."
    ),
    "methodologist": (
        "Test Exact Provenance, reproducibility, scope precision, exceptions, and whether "
        "the rule can be evaluated through observable future behavior."
    ),
}


def _steering_evaluator_prompt(role: str, proposal: SteeringProposal, evidence: list[dict]) -> str:
    return f"""You are the {role} on Second Brain's four-member Dream Cycle panel.

Evaluate this Steering Candidate, not a general insight:
{json.dumps(asdict(proposal), sort_keys=True)}

Source evidence:
{json.dumps(evidence, sort_keys=True)}

{_STEERING_CRITERIA[role]}

Panel acceptance retains a review candidate only. It does not activate guidance; the user must
approve final wording, Authority Scope, and Applicability separately.

Return exactly one JSON object with `verdict` set to `ACCEPT` or `REJECT` and non-empty `reasoning`.
"""


def evaluate_steering_proposal(
    proposal: SteeringProposal,
    *,
    resolver=None,
) -> SteeringReviewResult:
    """Run four independent model reviews, then apply the existing 3-of-4 quorum."""
    duplicate = find_matching_steering(proposal.wording)
    if duplicate is not None:
        return SteeringReviewResult(
            run_id="",
            final_verdict="DUPLICATE",
            candidate_id=None,
            active_rule_id=None,
            lifecycle="suppressed",
            duplicate_of=duplicate,
        )
    evidence = []
    for memory_id in proposal.source_memory_ids:
        memory = get_memory(memory_id)
        if memory is None:
            raise ValueError(f"source memory {memory_id} does not exist")
        evidence.append(
            {
                "memory_id": memory_id,
                "type": memory.get("type"),
                "title": memory.get("title"),
                "content": memory.get("content"),
                "source_url": memory.get("source_url"),
            }
        )

    active_resolver = resolver or default_resolver()
    verdicts = []
    for role in ("skeptic", "advocate", "epistemologist", "methodologist"):
        prompt = _steering_evaluator_prompt(role, proposal, evidence)
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                result = active_resolver.invoker_for(role).invoke(
                    system_prompt=prompt,
                    user_message="Evaluate the Steering Candidate and return the required JSON.",
                    tools=False,
                    timeout=600,
                    stage=f"steering_evaluator:{role}",
                )
                output = result.get("output")
                if not isinstance(output, dict):
                    raise ValueError("steering evaluator output must be an object")
                verdict = output.get("verdict")
                reasoning = output.get("reasoning")
                if verdict not in {"ACCEPT", "REJECT"} or not isinstance(reasoning, str) or not reasoning.strip():
                    raise ValueError("steering evaluator returned an invalid verdict")
                verdicts.append(EvaluatorVerdict(role=role, verdict=verdict, reasoning=reasoning))
                break
            except (TimeoutError, RuntimeError, ValueError) as exc:
                last_error = exc
        else:
            raise RuntimeError(f"steering evaluator {role} failed after 3 attempts") from last_error
    return review_steering_candidate(proposal, tuple(verdicts))


def _metadata(memory: dict) -> dict:
    value = memory.get("metadata") or {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def approve_steering_candidate(
    candidate_id: str,
    *,
    wording: str,
    authority_scope: str,
    applicability: dict[str, list[str]],
    approved_by: str = "user",
) -> ApprovedSteeringRule:
    """Create a versioned active rule from one accepted, inactive candidate."""
    if authority_scope not in _SCOPES:
        raise ValueError("authority_scope must be project, personal, or system")
    candidate = get_memory(candidate_id)
    if candidate is None or candidate.get("type") != "steering_candidate":
        raise ValueError("approval requires a Steering Candidate")
    candidate_metadata = _metadata(candidate)
    if candidate_metadata.get("lifecycle") != "proposed":
        raise ValueError("only a proposed Steering Candidate can be approved")
    if not wording.strip():
        raise ValueError("an approved rule needs wording")

    supersedes = candidate_metadata.get("supersedes_rule_id")
    version = 1
    if supersedes:
        previous = get_memory(supersedes)
        if (
            previous is None
            or previous.get("type") != "steering_rule"
            or previous.get("status") != "active"
        ):
            raise ValueError("supersession requires an active Steering Rule")
        previous_metadata = _metadata(previous)
        version = int(previous_metadata.get("rule_version", 1)) + 1

    now = datetime.now(UTC).isoformat()
    rule_metadata = {
        "authority": "approved",
        "lifecycle": "active",
        "authority_scope": authority_scope,
        "applicability": applicability,
        "rule_version": version,
        "approved_by": approved_by,
        "approved_at": now,
        "candidate_id": candidate_id,
        "source_memory_ids": candidate_metadata.get("source_memory_ids", []),
        "supersedes_rule_id": supersedes,
    }
    rule_id = create_memory(
        type="steering_rule",
        title=candidate.get("title") or "Approved Steering Rule",
        content=wording,
        embedding=generate_embedding(wording),
        tags=["steering", "approved"],
        source_type="steering_governance",
        status="active",
        mem_class="procedural",
        project=normalize_project_tag(
            (applicability.get("semantic_projects") or [None])[0]
        ),
        metadata=rule_metadata,
    )
    create_relationship(rule_id, candidate_id, "derived_from")
    if supersedes:
        update_memory(supersedes, status="superseded")
        create_relationship(supersedes, rule_id, "superseded_by")

    candidate_metadata.update(
        {"lifecycle": "approved", "approved_rule_id": rule_id, "approved_at": now}
    )
    update_memory(candidate_id, status="explored", metadata=candidate_metadata)
    return ApprovedSteeringRule(
        rule_id=rule_id,
        candidate_id=candidate_id,
        wording=wording,
        authority_scope=authority_scope,
        applicability=applicability,
        version=version,
        supersedes_rule_id=supersedes,
    )


def get_steering_rule(rule_id: str) -> ApprovedSteeringRule:
    memory = get_memory(rule_id)
    if memory is None or memory.get("type") != "steering_rule":
        raise KeyError(f"Steering Rule {rule_id} was not found")
    metadata = _metadata(memory)
    return ApprovedSteeringRule(
        rule_id=rule_id,
        candidate_id=metadata["candidate_id"],
        wording=memory["content"],
        authority_scope=metadata["authority_scope"],
        applicability=metadata.get("applicability") or {},
        version=int(metadata.get("rule_version", 1)),
        supersedes_rule_id=metadata.get("supersedes_rule_id"),
    )
