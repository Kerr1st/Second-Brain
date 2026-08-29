"""Build provenance-rich, task-ready context packs for connected agents.

``build_context`` is the module's small read interface. It hides retrieval,
authority ordering, applicability checks, token packing, conflict discovery,
and receipt persistence from callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any

from psycopg2.extras import RealDictCursor

from src.db import get_connection, get_memory, get_relationships, list_memories
from src.embeddings import generate_embedding
from src.project import normalize_project_tag
from src.search import hybrid_search, increment_access_count, rerank


_AUTHORITIES = ("approved", "inferred", "evidence")
_EVIDENCE_TYPES = {"source", "correction_episode", "research"}


@dataclass(frozen=True, slots=True)
class ContextRequest:
    objective: str
    project_hint: str | None = None
    source_system: str = "codex"
    repository: str | None = None
    budget_tokens: int = 1800
    limit: int = 7

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("a context request needs an objective")
        if self.budget_tokens < 100:
            raise ValueError("budget_tokens must be at least 100")
        if not 1 <= self.limit <= 20:
            raise ValueError("limit must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class ContextItem:
    memory_id: str
    authority: str
    memory_type: str
    title: str
    content: str
    semantic_project: str | None
    source_system: str | None
    source_task_id: str | None
    supporting_turn_ids: tuple[str, ...]
    observed_at: str | None
    supersedes: str | None
    retrieval_reason: str


@dataclass(frozen=True, slots=True)
class ContextPack:
    receipt_id: str
    objective: str
    items: tuple[ContextItem, ...]
    conflicts: tuple[dict[str, str], ...]
    token_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def _matches(values: Any, expected: str | None) -> bool:
    if not values:
        return True
    if not isinstance(values, list) or expected is None:
        return False
    normalized = expected.casefold()
    return any(isinstance(value, str) and value.casefold() == normalized for value in values)


def _rule_applies(memory: dict, request: ContextRequest, project: str | None) -> bool:
    metadata = memory.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    applicability = metadata.get("applicability") or {}
    if not isinstance(applicability, dict):
        return False
    if not _matches(applicability.get("integrations"), request.source_system):
        return False
    if not _matches(applicability.get("semantic_projects"), project):
        return False
    if not _matches(applicability.get("repositories"), request.repository):
        return False
    topics = applicability.get("topics") or []
    if topics:
        objective_tokens = _tokens(request.objective)
        if not any(
            isinstance(topic, str) and _tokens(topic) & objective_tokens
            for topic in topics
        ):
            return False
    return True


def _authority(memory: dict) -> str:
    metadata = memory.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    declared = metadata.get("authority")
    if declared in _AUTHORITIES:
        return declared
    if memory.get("type") == "steering_rule":
        return "approved"
    if memory.get("type") in _EVIDENCE_TYPES:
        return "evidence"
    return "inferred"


def _context_item(memory: dict, authority: str, reason: str) -> ContextItem:
    metadata = memory.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    observed = metadata.get("observed_at") or memory.get("created_at")
    if hasattr(observed, "isoformat"):
        observed = observed.isoformat()
    source_task_id = metadata.get("native_task_id") or metadata.get("task_source_url")
    return ContextItem(
        memory_id=str(memory["id"]),
        authority=authority,
        memory_type=memory.get("type") or "",
        title=memory.get("title") or "",
        content=memory.get("content") or "",
        semantic_project=memory.get("project"),
        source_system=metadata.get("source_system") or memory.get("source_type"),
        source_task_id=source_task_id,
        supporting_turn_ids=tuple(metadata.get("supporting_turn_ids") or ()),
        observed_at=str(observed) if observed else None,
        supersedes=metadata.get("supersedes_rule_id"),
        retrieval_reason=reason,
    )


def _estimate_item_tokens(item: ContextItem) -> int:
    rendered = f"{item.authority} {item.memory_type} {item.title} {item.content} {item.retrieval_reason}"
    return max(1, (len(rendered) + 3) // 4)


def _discover_conflicts(items: list[ContextItem]) -> tuple[dict[str, str], ...]:
    selected = {item.memory_id for item in items}
    seen: set[tuple[str, str]] = set()
    conflicts: list[dict[str, str]] = []
    for item in items:
        for relationship in get_relationships(item.memory_id):
            if relationship.get("relation_type") != "contradicts":
                continue
            other = str(
                relationship["target_id"]
                if str(relationship["source_id"]) == item.memory_id
                else relationship["source_id"]
            )
            if other not in selected:
                continue
            pair = tuple(sorted((item.memory_id, other)))
            if pair in seen:
                continue
            seen.add(pair)
            conflicts.append(
                {
                    "first_memory_id": pair[0],
                    "second_memory_id": pair[1],
                    "note": relationship.get("note") or "",
                }
            )
    return tuple(conflicts)


def _store_receipt(request: ContextRequest, project: str | None, items, conflicts, token_count) -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_receipts (
                    objective, semantic_project, source_system, repository,
                    returned_memory_ids, token_count, conflicts
                ) VALUES (%s, %s, %s, %s, %s::uuid[], %s, %s)
                RETURNING id
                """,
                (
                    request.objective,
                    project,
                    request.source_system,
                    request.repository,
                    [item.memory_id for item in items],
                    token_count,
                    json.dumps(conflicts),
                ),
            )
            receipt_id = cur.fetchone()[0]
        conn.commit()
    return str(receipt_id)


def build_context(request: ContextRequest) -> ContextPack:
    """Return the highest-authority applicable context within one token budget."""
    project = normalize_project_tag(request.project_hint)
    query_embedding = generate_embedding(request.objective)

    approved = [
        memory
        for memory in list_memories(type="steering_rule", status="active", limit=200)
        if _rule_applies(memory, request, project)
    ]
    approved = rerank(approved, request.objective, query_project=project)

    relevant = hybrid_search(
        request.objective,
        query_embedding,
        limit=max(request.limit * 3, 20),
        status="active",
        project=project,
    )
    relevant = rerank(relevant, request.objective, query_project=project)

    ordered: list[ContextItem] = []
    seen: set[str] = set()
    for memory in approved:
        mid = str(memory["id"])
        if mid in seen:
            continue
        seen.add(mid)
        ordered.append(
            _context_item(
                memory,
                "approved",
                f"approved rule applicable to {request.source_system}",
            )
        )
    for memory in relevant:
        mid = str(memory["id"])
        if mid in seen or memory.get("type") in {"steering_candidate", "steering_rule"}:
            continue
        seen.add(mid)
        ordered.append(_context_item(memory, _authority(memory), "hybrid task relevance"))

    ordered.sort(key=lambda item: _AUTHORITIES.index(item.authority))
    packed: list[ContextItem] = []
    token_count = 0
    for item in ordered:
        item_tokens = _estimate_item_tokens(item)
        if token_count + item_tokens > request.budget_tokens:
            continue
        packed.append(item)
        token_count += item_tokens
        if len(packed) >= request.limit:
            break

    conflicts = _discover_conflicts(packed)
    receipt_id = _store_receipt(request, project, packed, conflicts, token_count)
    increment_access_count([item.memory_id for item in packed])
    return ContextPack(
        receipt_id=receipt_id,
        objective=request.objective,
        items=tuple(packed),
        conflicts=conflicts,
        token_count=token_count,
    )


def get_context_receipt(receipt_id: str) -> dict[str, Any]:
    """Read one context receipt for audit or outcome evaluation."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM context_receipts WHERE id = %s", (receipt_id,))
            row = cur.fetchone()
    if row is None:
        raise KeyError(f"context receipt {receipt_id} was not found")
    result = dict(row)
    result["id"] = str(result["id"])
    for field in ("returned_memory_ids", "used_memory_ids"):
        values = result[field]
        if isinstance(values, str):
            values = [value for value in values.strip("{}").split(",") if value]
        result[field] = [str(value) for value in values]
    if result.get("correction_episode_id"):
        result["correction_episode_id"] = str(result["correction_episode_id"])
    return result


def record_context_outcome(
    receipt_id: str,
    *,
    used_memory_ids: list[str],
    outcome: str,
    note: str | None = None,
    correction_episode_id: str | None = None,
) -> None:
    """Close the recall loop for one previously emitted context pack."""
    if outcome not in {"followed", "corrected", "not_used", "unknown"}:
        raise ValueError("outcome must be followed, corrected, not_used, or unknown")
    receipt = get_context_receipt(receipt_id)
    returned = set(receipt["returned_memory_ids"])
    if not set(used_memory_ids) <= returned:
        raise ValueError("used memories must have been returned in the context pack")
    if outcome == "corrected" and correction_episode_id is None:
        raise ValueError("a corrected outcome needs a Correction Episode")
    if correction_episode_id is not None:
        episode = get_memory(correction_episode_id)
        if episode is None or episode.get("type") != "correction_episode":
            raise ValueError("correction_episode_id must identify a Correction Episode")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE context_receipts
                SET used_memory_ids = %s::uuid[], outcome = %s, outcome_note = %s,
                    correction_episode_id = %s, evaluated_at = now()
                WHERE id = %s
                """,
                (used_memory_ids, outcome, note, correction_episode_id, receipt_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"context receipt {receipt_id} was not found")
        conn.commit()
