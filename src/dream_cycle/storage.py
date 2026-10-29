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

from botocore.exceptions import BotoCoreError, ClientError
from psycopg2.extras import RealDictCursor
from src.classify import classify_memory
from src.db import create_memory, create_relationship, search_similar, update_memory, transaction
from src.db.maintenance import install_chunk_set, purge_superseded_chunks
from src.depth import compute_depth_score
from src.embeddings import generate_embedding
from src.ingest import chunk_by_section
from src.models import CandidateInsight
from src.validation import RELATION_TYPES

DISCOVERY_TYPES = frozenset({"insight", "connection", "question", "synthesis"})
# Lifecycle and provenance edges are derived by code, never delegated to a model.
PROPOSED_RELATIONS = RELATION_TYPES - {"superseded_by", "derived_from"}
PROTECTED_MARKERS = frozenset({"source", "capture", "governing", "guidance", "steering", "correction", "correction_episode"})


class CandidateAuthorizationError(ValueError):
    """A suggested mutation is outside the discovery writer's authority."""


class DiscoveryEmbeddingError(RuntimeError):
    """The embedding provider failed for an accepted discovery; nothing was written."""


def canonical_uuid(value) -> str | None:
    """Canonical (lower-case, hyphenated) UUID string, or None when malformed."""
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _uuid(value) -> str:
    canonical = canonical_uuid(value)
    if canonical is None:
        raise CandidateAuthorizationError("Candidate references must be valid memory UUIDs")
    return canonical


#: The corpus chunk bound (``chunk_by_section`` default, ``oversize_vector_rows``):
#: no stored vector describes more than this many characters. A longer discovery
#: is stored like ingested long content: a parent row without its own vector and
#: active chunk children that carry the vectors.
MAX_CHUNK_CHARS = 4000


def _prepare_vectors(content: str):
    """``(comparison_vector, chunks)`` for ``content``.

    Short content: its own embedding and ``None`` (stored on the row, as before).
    Long content: one embedding per bounded chunk, ``[(text, vector), ...]``, and
    their mean as the whole-text comparison vector for the duplicate checks only;
    the mean is never stored.
    """
    if len(content) <= MAX_CHUNK_CHARS:
        return _embed(content, "text"), None
    texts = [text for text in chunk_by_section(content, MAX_CHUNK_CHARS) if text.strip()]
    chunks = [(text, _embed(text, f"chunk {i}/{len(texts)}")) for i, text in enumerate(texts, 1)]
    mean = [sum(column) / len(chunks) for column in zip(*(vector for _, vector in chunks))]
    return mean, chunks


def _embed(text: str, part: str):
    """``generate_embedding``, with a provider failure raised as :class:`DiscoveryEmbeddingError`.

    Runs before the write transaction, so a failure on any call writes nothing.
    """
    try:
        return generate_embedding(text)
    except (BotoCoreError, ClientError) as exc:
        raise DiscoveryEmbeddingError(f"Embedding provider failed on {part}: {exc}") from exc


def _install_chunks(conn, memory_id: str, chunks, candidate: CandidateInsight, metadata: dict) -> None:
    """Give ``memory_id`` this chunk set (clears the parent's vector), purge uncited old chunks."""
    install_chunk_set(conn, memory_id, chunks,
                      fields={"type": candidate.type, "title": candidate.title,
                              "tags": ["dream-cycle", candidate.schema_operation]},
                      metadata=metadata, mem_class=lambda text: classify_memory(candidate.type, text))
    purge_superseded_chunks(conn, memory_id)


def _retire_chunks(conn, memory_id: str) -> None:
    """Supersede a parent's active chunks (they no longer describe it), purge uncited ones."""
    with conn.cursor() as cur:
        cur.execute("UPDATE memories SET status='superseded', updated_at=now() WHERE parent_id=%s AND status='active'",
                    (memory_id,))
        retired = cur.rowcount
    if retired:
        purge_superseded_chunks(conn, memory_id)


# Semantic duplicate threshold, as in production before the refactor
# (``check_duplicate(content, threshold=0.85)`` over active parent memories).
SEMANTIC_DUPLICATE_THRESHOLD = 0.85


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


def is_mutable_target(row: dict) -> bool:
    """Whether :func:`_assert_target_mutable` accepts ``row`` as an UPDATE/SUPERSEDE target.

    Used to mark targets in the Thinker packet; authorization still runs in full
    (including the accepted-provenance lookup) before any paid call.
    """
    try:
        _assert_target_mutable(row, row)
    except CandidateAuthorizationError:
        return False
    return True


def is_active_memory(row: dict | None) -> bool:
    """The lifecycle rule for anything a discovery cites, targets or relates to."""
    return bool(row) and row.get("status") == "active"


def _assert_all_active(ids: set[str], rows: dict[str, dict]) -> None:
    """Every source, target and relationship target must exist and be active.

    ``rows`` maps canonical id to the current database row. Shared by
    :func:`authorize_candidate` (before any paid call) and :func:`store_accepted`
    (under row locks after the panel), so the two can never drift.
    """
    if not ids <= rows.keys() or not all(is_active_memory(rows[mid]) for mid in ids):
        raise CandidateAuthorizationError("Every source and target must exist and be active")


def _current_rows(cur, ids: set[str], *, columns: str = "*", lock: bool = False) -> dict[str, dict]:
    sql = f"SELECT {columns} FROM memories WHERE id = ANY(%s::uuid[]) ORDER BY id" + (" FOR UPDATE" if lock else "")
    cur.execute(sql, (sorted(ids),))
    return {str(row["id"]): dict(row) for row in cur.fetchall()}


def _has_accepted_provenance(cur, target_id: str) -> bool:
    cur.execute("SELECT EXISTS(SELECT 1 FROM dream_cycle_candidates WHERE created_memory_id=%s AND final_verdict='ACCEPTED')", (target_id,))
    row = cur.fetchone()
    return bool(row["exists"] if isinstance(row, dict) else row[0])


def authorize_candidate(candidate: CandidateInsight, authorized_memories: dict[str, dict], *, conn=None) -> set[str]:
    """Everything that can be decided before spending on grounding and the panel.

    Vocabulary, citation and relationship-target checks (:func:`validate_candidate`);
    for UPDATE/SUPERSEDE, the target authority rules from the supplied snapshot
    (captured sources and guidance are immutable; only a discovery type with
    ``dream_cycle`` metadata); then one cheap read of the current rows: every
    source, target and relationship target must still exist and be active (the
    rule ``store_accepted`` enforces, via the shared :func:`_assert_all_active`),
    and a mutation target needs accepted-discovery provenance. ``store_accepted``
    repeats all of it under row locks after the panel as defence in depth.
    Raises CandidateAuthorizationError.
    """
    ids = validate_candidate(candidate, authorized_memories)
    mutation = candidate.operation in {"UPDATE", "SUPERSEDE"}
    if mutation:
        target_id = _uuid(candidate.target_memory_id)
        snapshot = authorized_memories[target_id]
        _assert_target_mutable(snapshot, snapshot)
    from src.db.pool import connection_scope
    with connection_scope(conn) as scope:
        with scope.cursor(cursor_factory=RealDictCursor) as cur:
            _assert_all_active(ids, _current_rows(cur, ids, columns="id, status"))
            if mutation and not _has_accepted_provenance(cur, target_id):
                raise CandidateAuthorizationError("Mutation target has no accepted discovery provenance")
    return ids


def store_accepted(candidate: CandidateInsight, *, run_id: str, verdicts: dict,
                   authorized_memories: dict[str, dict]) -> str:
    """Commit memory, provenance/relationship edges and accepted ledger together.

    The context mapping must be the actual slice snapshots supplied for reasoning,
    never an allow-list made from model citations. Row locks are acquired in UUID
    order. A source-set advisory lock serializes CREATE dedup across concurrent
    callers, including the no-existing-row case.

    Content over :data:`MAX_CHUNK_CHARS` is stored as a parent without a vector
    plus active chunk children (``install_chunk_set``, as ingest and rechunk do).
    The parent keeps the ID: edges, ``created_memory_id`` and dedup refer to it,
    and dedup compares a chunked parent by the mean of its chunk vectors.
    """
    from src.dream_cycle_db import store_candidate
    ids = validate_candidate(candidate, authorized_memories)
    source_ids = sorted({_uuid(mid) for mid in candidate.source_memories})
    source_key = json.dumps(source_ids, separators=(",", ":"))
    lock_key = int.from_bytes(hashlib.sha256(source_key.encode()).digest()[:8], "big", signed=True)
    embedding, chunks = _prepare_vectors(candidate.content)
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
                AND 1 - (COALESCE(embedding, (SELECT avg(c.embedding) FROM memories c
                          WHERE c.parent_id = memories.id AND c.status = 'active')) <=> %s::vector) > 0.85
                ORDER BY id LIMIT 1"""
            duplicate_params = (json.dumps(source_ids), json.dumps(source_ids), str(embedding))
            duplicate_id = None
            semantic_duplicate = None
            if candidate.operation == "CREATE":
                cur.execute(duplicate_sql, duplicate_params)
                duplicate = cur.fetchone()
                duplicate_id = str(duplicate["id"]) if duplicate else None
                if duplicate_id is None:
                    # Production (1071a81) semantics kept: a candidate that restates ANY
                    # active parent memory above the threshold -- captured fact,
                    # distilled insight or a discovery with a different source set --
                    # is a duplicate and does not become a new memory (review C S3 2.4).
                    semantic_duplicate = _semantic_duplicate(cur, embedding)
            lock_ids = ids | ({duplicate_id} if duplicate_id else set())
            rows = _current_rows(cur, lock_ids, lock=True)
            _assert_all_active(ids, rows)
            for mid in ids:
                if rows[mid].get("content") != authorized_memories[mid].get("content"):
                    raise CandidateAuthorizationError("Source or target changed since evaluation")
            memory_id = None
            if semantic_duplicate is not None:
                # Record the acceptance against the existing memory without creating
                # one. created_memory_id stays NULL: an arbitrary matched memory must
                # not acquire accepted-discovery provenance (which would make it a
                # legal UPDATE/SUPERSEDE target).
                store_candidate(run_id, asdict(candidate) | {"duplicate_of": semantic_duplicate["id"],
                                "duplicate_similarity": semantic_duplicate["similarity"]},
                                verdicts, "ACCEPTED", None, conn=conn)
                return str(semantic_duplicate["id"])
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
                if not _has_accepted_provenance(cur, target_id):
                    raise CandidateAuthorizationError("Mutation target has no accepted discovery provenance")
            if memory_id is None:
                if candidate.operation == "UPDATE":
                    memory_id = _uuid(candidate.target_memory_id)
                    metadata = {**_metadata(rows[memory_id]), **metadata,
                                "last_dream_cycle_update": datetime.now(timezone.utc).isoformat()}
                    if chunks is None:
                        _retire_chunks(conn, memory_id)
                    update_memory(memory_id, title=candidate.title, content=candidate.content,
                        embedding=embedding if chunks is None else None, metadata=metadata,
                        mem_class=classify_memory(candidate.type, candidate.content), conn=conn)
                    if chunks is not None:
                        _install_chunks(conn, memory_id, chunks, candidate, metadata)
                else:
                    memory_id = create_memory(type=candidate.type, title=candidate.title,
                        content=candidate.content, embedding=embedding if chunks is None else None,
                        tags=["dream-cycle", candidate.schema_operation], metadata=metadata,
                        mem_class=classify_memory(candidate.type, candidate.content), conn=conn)
                    if chunks is not None:
                        _install_chunks(conn, memory_id, chunks, candidate, metadata)
                    if candidate.operation == "SUPERSEDE":
                        update_memory(candidate.target_memory_id, status="superseded", conn=conn)
                        _retire_chunks(conn, candidate.target_memory_id)
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


def _semantic_duplicate(cur, embedding, threshold: float = SEMANTIC_DUPLICATE_THRESHOLD) -> dict | None:
    """Nearest active embedded parent memory above ``threshold`` (index-friendly ORDER BY)."""
    cur.execute("""SELECT id, 1 - (embedding <=> %s::vector) AS similarity FROM memories
        WHERE status = 'active' AND parent_id IS NULL AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector LIMIT 1""", (str(embedding), str(embedding)))
    row = cur.fetchone()
    if row and row["similarity"] is not None and float(row["similarity"]) > threshold:
        return {"id": str(row["id"]), "similarity": float(row["similarity"])}
    return None


def check_duplicate(content: str, threshold: float = SEMANTIC_DUPLICATE_THRESHOLD) -> str | None:
    """Read-only similarity preview. Never use it to authorize a mutation."""
    results = search_similar(generate_embedding(content), limit=5, status="active")
    return next((str(row["id"]) for row in results if row.get("parent_id") is None and row.get("similarity", 0) > threshold), None)
