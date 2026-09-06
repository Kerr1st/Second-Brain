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
from src.search import DEFAULT_CANDIDATE_LIMIT, retrieve_memories, increment_access_count, rerank


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
    observed = metadata.get("observed_at")
    # Codex rows are created during capture/distillation, potentially long after
    # their supporting turns. Do not present processing time as source recency.
    if observed is None and memory.get("source_type") not in {
        "codex_task", "distilled_agent_task"
    }:
        observed = memory.get("created_at")
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


def _topic_groups(relevant: list[dict], project: str | None) -> dict[str, list[dict]]:
    """Follow exact derivation links, never workspace or whole-task similarity.

    A search hit on an older task memory must not hide later evidence from its
    Topic Segment. Order by supporting Agent Turns, not database insertion time.
    This is evidence delivery; it does not decide contradictions or supersede
    records. Only the three task-distillation kinds can enter these groups.
    """
    ids = [str(memory["id"]) for memory in relevant]
    if not ids:
        return {}
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            WITH segments AS (
                SELECT s.id, s.metadata
                FROM memories s
                WHERE s.status = 'active' AND s.source_type = 'codex_task'
                  AND s.metadata->>'record_kind' = 'topic_segment'
                  AND (s.id = ANY(%s::uuid[]) OR EXISTS (
                      SELECT 1 FROM memory_relationships r
                      WHERE r.target_id = s.id AND r.relation_type = 'derived_from'
                        AND r.source_id = ANY(%s::uuid[])))
            )
            SELECT m.*, s.id AS context_segment_id,
                   s.metadata->'turn_ids' AS context_turn_ids
            FROM segments s
            JOIN memory_relationships r ON r.target_id = s.id
                 AND r.relation_type = 'derived_from'
            JOIN memories m ON m.id = r.source_id
            WHERE m.status = 'active' AND m.source_type = 'distilled_agent_task'
              AND m.metadata->>'record_kind' = 'task_memory'
              AND m.type IN ('decision', 'insight', 'correction_episode')
              AND m.metadata->>'task_source_url' = s.metadata->>'task_source_url'
              AND (%s IS NULL OR m.project IS NULL OR m.project = %s)
            """,
            (ids, ids, project, project),
        )
        rows = [dict(row) for row in cur.fetchall()]
    by_segment: dict[str, list[dict]] = {}
    for row in rows:
        positions = {turn: index for index, turn in enumerate(row['context_turn_ids'] or [])}
        support = (row.get('metadata') or {}).get('supporting_turn_ids') or []
        if not support or not set(support) <= positions.keys():
            continue
        row['_source_position'] = max(positions[turn] for turn in support)
        by_segment.setdefault(str(row['context_segment_id']), []).append(row)
    groups: dict[str, list[dict]] = {}
    for segment, memories in by_segment.items():
        memories.sort(key=lambda m: (-m['_source_position'], str(m['id'])))
        groups[segment] = memories
        for memory in memories:
            groups[str(memory['id'])] = memories
    return groups


def build_context(request: ContextRequest) -> ContextPack:
    """Pack approved rules, then topic evidence with its later source updates."""
    project = normalize_project_tag(request.project_hint)
    query_embedding = generate_embedding(request.objective)

    approved = [
        memory
        for memory in list_memories(type="steering_rule", status="active", limit=200)
        if _rule_applies(memory, request, project)
    ]
    approved = rerank(approved, request.objective, query_project=project)

    relevant = retrieve_memories(
        request.objective,
        query_embedding,
        limit=DEFAULT_CANDIDATE_LIMIT,
        status="active",
        project=project,
    )
    relevant = rerank(relevant, request.objective, query_project=project)

    # Approved governing rules retain priority. Among ordinary memories, a
    # topic's later evidence travels ahead of its older inferred claims.
    groups = _topic_groups(relevant, project)
    ordered = sorted(relevant, key=lambda m: _AUTHORITIES.index(_authority(m)))
    seen: set[str] = set()
    packed: list[ContextItem] = []
    token_count = 0
    approved_ids = {str(memory["id"]) for memory in approved}
    packed_ids: set[str] = set()
    for memory in [*approved, *ordered]:
        if len(packed) >= request.limit:
            break
        mid = str(memory['id'])
        if mid in seen:
            continue
        is_rule = memory.get('type') == 'steering_rule'
        if memory.get('type') == 'steering_candidate' or (is_rule and mid not in approved_ids):
            continue
        group = [memory] if is_rule else groups.get(mid, [memory])
        # Mark the whole group even if its newer evidence cannot fit. Otherwise
        # another search hit could reintroduce an older claim by itself.
        seen.add(mid)
        seen.update(str(member['id']) for member in group)
        for index, member in enumerate(group):
            if str(member["id"]) in packed_ids:
                continue
            if len(packed) >= request.limit:
                break
            reason = 'hybrid task relevance'
            if is_rule:
                reason = f'approved rule applicable to {request.source_system}'
            elif mid in groups:
                reason = ('Later evidence from the same Topic Segment; read together, '
                          'source order does not establish approval or supersession')
                if index:
                    reason = ('Historical task memory; later same-topic evidence is included. '
                              'Read together, not as an independent current-state claim')
            item = _context_item(member, 'approved' if is_rule else _authority(member), reason)
            item_tokens = _estimate_item_tokens(item)
            if token_count + item_tokens > request.budget_tokens:
                break
            packed.append(item)
            packed_ids.add(item.memory_id)
            token_count += item_tokens

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
