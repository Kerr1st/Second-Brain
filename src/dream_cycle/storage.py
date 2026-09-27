"""Authorize and atomically persist accepted discoveries.

Panel agreement measures judgment. It grants no authority to rewrite captured
sources or governing guidance. Every write below is one database transaction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

from psycopg2.extras import RealDictCursor
from src.classify import classify_memory
from src.db import create_memory, create_relationship, search_similar, update_memory, transaction
from src.depth import compute_depth_score
from src.embeddings import generate_embedding
from src.models import CandidateInsight
from src.validation import RELATION_TYPES

DISCOVERY_TYPES = frozenset({"insight", "connection", "question", "synthesis"})
# Lifecycle and provenance edges are derived by code, never delegated to a model.
PROPOSED_RELATIONS = RELATION_TYPES - {"superseded_by", "derived_from"}
PROTECTED_MARKERS = frozenset({"source", "capture", "governing", "guidance", "steering", "correction", "correction_episode"})


class CandidateAuthorizationError(ValueError):
    """A suggested mutation is outside the discovery writer's authority."""


def _uuid(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise CandidateAuthorizationError("Candidate references must be valid memory UUIDs") from None


def validate_candidate(candidate: CandidateInsight, authorized_memories: dict[str, dict]) -> set[str]:
    """Validate every identifier and vocabulary before embedding or mutation."""
    if candidate.operation not in {"CREATE", "UPDATE", "SUPERSEDE"}:
        raise CandidateAuthorizationError("Unsupported candidate operation")
    if candidate.type not in DISCOVERY_TYPES:
        raise CandidateAuthorizationError("Only discovery memory types may be written")
    if not isinstance(candidate.title, str) or not candidate.title.strip() or not isinstance(candidate.content, str) or not candidate.content.strip():
        raise CandidateAuthorizationError("Candidate title and content must be nonempty text")
    if candidate.schema_operation not in {"assimilation", "accommodation"}:
        raise CandidateAuthorizationError("Invalid schema operation")
    if not isinstance(candidate.source_memories, list) or not candidate.source_memories:
        raise CandidateAuthorizationError("A discovery must cite at least one source from its context")
    ids = {_uuid(mid) for mid in candidate.source_memories}
    if not ids <= set(authorized_memories):
        raise CandidateAuthorizationError("Candidate cites a source outside its supplied context")
    if candidate.operation == "CREATE":
        if candidate.target_memory_id is not None:
            raise CandidateAuthorizationError("CREATE cannot specify a mutation target")
    else:
        target = _uuid(candidate.target_memory_id)
        if target not in authorized_memories:
            raise CandidateAuthorizationError("Mutation target was not in the supplied context")
        ids.add(target)
        if candidate.operation == "SUPERSEDE" and not str(candidate.supersedes_reason or "").strip():
            raise CandidateAuthorizationError("SUPERSEDE requires a reason")
    if not isinstance(candidate.relationships, list):
        raise CandidateAuthorizationError("Relationships must be a list")
    for rel in candidate.relationships:
        if not isinstance(rel, dict) or rel.get("relation_type") not in PROPOSED_RELATIONS:
            raise CandidateAuthorizationError("Invalid proposed relationship type")
        target = _uuid(rel.get("target_id"))
        if target not in authorized_memories:
            raise CandidateAuthorizationError("Relationship target was not in the supplied context")
        if rel.get("note") is not None and not isinstance(rel["note"], str):
            raise CandidateAuthorizationError("Relationship note must be text")
        ids.add(target)
    return ids


def _metadata(row: dict) -> dict:
    value = row.get("metadata") or {}
    return json.loads(value) if isinstance(value, str) else value


def _assert_target_mutable(row: dict, snapshot: dict) -> None:
    metadata = _metadata(row)
    markers = {str(t).lower().replace("-", "_") for t in row.get("tags") or []}
    protected = any(any(word in marker for word in PROTECTED_MARKERS) for marker in markers)
    protected |= any(metadata.get(key) for key in ("governing", "guidance", "steering", "correction_episode", "capture_id", "source_type"))
    if (metadata.get("dream_cycle") is not True or row.get("type") not in DISCOVERY_TYPES
            or row.get("source_type") or row.get("parent_id") or protected):
        raise CandidateAuthorizationError("Captured evidence and governing guidance are immutable to discovery agents")
    # A second writer must not apply a judgment to a target changed since reading.
    if row.get("content") != snapshot.get("content") or str(row.get("updated_at")) != str(snapshot.get("updated_at")):
        raise CandidateAuthorizationError("Mutation target changed since evaluation; regenerate the candidate")


def store_accepted(candidate: CandidateInsight, *, run_id: str, verdicts: dict,
                   authorized_memories: dict[str, dict]) -> str:
    """Commit memory, provenance/relationship edges and accepted ledger together.

    The context mapping must be the actual slice snapshots supplied for reasoning,
    never an allow-list made from model citations. Row locks are acquired in UUID
    order. A source-set advisory lock serializes CREATE dedup across concurrent
    callers, including the no-existing-row case.
    """
    from src.dream_cycle_db import store_candidate
    ids = validate_candidate(candidate, authorized_memories)
    source_ids = sorted({_uuid(mid) for mid in candidate.source_memories})
    source_key = json.dumps(source_ids, separators=(",", ":"))
    lock_key = int.from_bytes(hashlib.sha256(source_key.encode()).digest()[:8], "big", signed=True)
    embedding = generate_embedding(candidate.content)
    metadata = {"dream_cycle": True, "strategy": candidate.strategy_that_found_it,
                "source_memories": source_ids, "source_key": source_key,
                "confidence": candidate.confidence, "depth_score": compute_depth_score(candidate.content)}
    with transaction() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            duplicate_sql = """SELECT id FROM memories
                WHERE status = 'active' AND parent_id IS NULL
                AND metadata->>'dream_cycle' = 'true'
                AND source_type IS NULL AND type IN ('insight','connection','question','synthesis')
                AND EXISTS (SELECT 1 FROM dream_cycle_candidates c
                            WHERE c.created_memory_id=memories.id AND c.final_verdict='ACCEPTED')
                AND metadata->'source_memories' @> %s::jsonb
                AND metadata->'source_memories' <@ %s::jsonb
                AND 1 - (embedding <=> %s::vector) > 0.85
                ORDER BY id LIMIT 1"""
            duplicate_params = (json.dumps(source_ids), json.dumps(source_ids), str(embedding))
            duplicate_id = None
            if candidate.operation == "CREATE":
                cur.execute(duplicate_sql, duplicate_params)
                duplicate = cur.fetchone()
                duplicate_id = str(duplicate["id"]) if duplicate else None
            lock_ids = ids | ({duplicate_id} if duplicate_id else set())
            cur.execute("SELECT * FROM memories WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE", (sorted(lock_ids),))
            rows = {str(row["id"]): dict(row) for row in cur.fetchall()}
            if not ids <= rows.keys() or any(rows[mid].get("status") != "active" for mid in ids):
                raise CandidateAuthorizationError("Every source and target must exist and be active")
            for mid in ids:
                if rows[mid].get("content") != authorized_memories[mid].get("content"):
                    raise CandidateAuthorizationError("Source or target changed since evaluation")
            memory_id = None
            if candidate.operation == "CREATE":
                # Recheck the selected duplicate after locking it with all source
                # and target rows. Never acquire a later row lock out of order.
                if duplicate_id and duplicate_id in rows:
                    cur.execute(duplicate_sql, duplicate_params)
                    duplicate = cur.fetchone()
                    if duplicate and str(duplicate["id"]) == duplicate_id:
                        # Reuse only an authoritative discovery that remains in
                        # our writable namespace, never an imported marker or guidance.
                        try:
                            _assert_target_mutable(rows[duplicate_id], rows[duplicate_id])
                        except CandidateAuthorizationError:
                            pass
                        else:
                            memory_id = duplicate_id
            else:
                target_id = _uuid(candidate.target_memory_id)
                _assert_target_mutable(rows[target_id], authorized_memories[target_id])
                cur.execute("SELECT EXISTS(SELECT 1 FROM dream_cycle_candidates WHERE created_memory_id=%s AND final_verdict='ACCEPTED')", (target_id,))
                if not cur.fetchone()["exists"]:
                    raise CandidateAuthorizationError("Mutation target has no accepted discovery provenance")
            if memory_id is None:
                if candidate.operation == "UPDATE":
                    memory_id = _uuid(candidate.target_memory_id)
                    metadata = {**_metadata(rows[memory_id]), **metadata,
                                "last_dream_cycle_update": datetime.now(timezone.utc).isoformat()}
                    update_memory(memory_id, title=candidate.title, content=candidate.content,
                        embedding=embedding, metadata=metadata,
                        mem_class=classify_memory(candidate.type, candidate.content), conn=conn)
                else:
                    memory_id = create_memory(type=candidate.type, title=candidate.title,
                        content=candidate.content, embedding=embedding,
                        tags=["dream-cycle", candidate.schema_operation], metadata=metadata,
                        mem_class=classify_memory(candidate.type, candidate.content), conn=conn)
                    if candidate.operation == "SUPERSEDE":
                        update_memory(candidate.target_memory_id, status="superseded", conn=conn)
                        create_relationship(candidate.target_memory_id, memory_id, "superseded_by", candidate.supersedes_reason, conn=conn)
                for source_id in source_ids:
                    if source_id != memory_id:
                        create_relationship(memory_id, source_id, "derived_from", conn=conn)
                for rel in candidate.relationships:
                    if rel["target_id"] == memory_id:
                        raise CandidateAuthorizationError("A discovery cannot relate to itself")
                    create_relationship(memory_id, rel["target_id"], rel["relation_type"], rel.get("note"), conn=conn)
            store_candidate(run_id, asdict(candidate), verdicts, "ACCEPTED", memory_id, conn=conn)
    return str(memory_id)


def check_duplicate(content: str, threshold: float = 0.85) -> str | None:
    """Read-only similarity preview. Never use it to authorize a mutation."""
    results = search_similar(generate_embedding(content), limit=5, status="active")
    return next((str(row["id"]) for row in results if row.get("parent_id") is None and row.get("similarity", 0) > threshold), None)
