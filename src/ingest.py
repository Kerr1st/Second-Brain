"""Atomic ingestion pipeline for rendered capture documents.

All source types (Crawlee markdown, CLI chats, IDE chats, notes) are
normalized to markdown with metadata headers before reaching this module.

Pipeline: parse metadata header → chunk by section → embed → store.
"""

import logging
import re
import psycopg2
from datetime import datetime, timezone
from src.db import (
    create_memory,
    create_relationship,
    find_temporal_neighbors,
    get_memory,
    transaction, update_memory, update_fields, find_captured_source, lock_source_identity,
    search_similar,
)
from src.embeddings import generate_embedding
from src.classify import classify_memory
from src.depth import compute_depth_score
from src.project import normalize_project_tag

logger = logging.getLogger(__name__)


def parse_metadata_header(content):
    """Extract metadata fields from a markdown file's header.

    Expected format:
        # Title
        
        Source: url
        Type: web-page
        Date: 2026-03-08
        [additional fields...]
        
        ---
        
        body content

    Returns (metadata_dict, body_text).
    """
    meta = {}
    lines = content.split("\n")
    body_start = 0

    for i, line in enumerate(lines):
        if line.strip() == "---":
            body_start = i + 1
            break
        if line.startswith("# "):
            meta["title"] = line[2:].strip()
        elif ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = re.sub(r"[\s-]+", "_", key.strip().lower())
            meta[key] = value.strip()

    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def chunk_by_section(body, max_chunk_chars=4000):
    """Lossless bounded chunks, preferring section, paragraph then line breaks.

    A single oversized line is hard-split. Joining the returned chunks reproduces
    the complete input, including whitespace; separators count toward the bound.
    """
    if isinstance(max_chunk_chars, bool) or not isinstance(max_chunk_chars, int) or max_chunk_chars < 1:
        raise ValueError("max_chunk_chars must be a positive integer")
    if not body:
        return []
    chunks = []
    offset = 0
    while offset < len(body):
        end = min(offset + max_chunk_chars, len(body))
        if end < len(body):
            window = body[offset:end]
            # Avoid tiny chunks: only prefer boundaries in the latter half.
            floor = max_chunk_chars // 2
            candidates = [m.start() + 1 for m in re.finditer(r"\n(?=#{2,6}\s)", window)]
            split = next((i for i in reversed(candidates) if i >= floor), 0)
            if not split:
                split = window.rfind("\n\n", floor)
                split = split + 2 if split >= 0 else 0
            if not split:
                split = window.rfind("\n", floor)
                split = split + 1 if split >= 0 else 0
            if split:
                end = offset + split
        chunks.append(body[offset:end])
        offset = end
    return chunks


def _discover_relationships(parent_id, body=None):
    """Best-effort temporal associations, with no discarded paid embedding.

    Semantic links belong to the discovery pipeline, which can reuse persisted
    chunk embeddings. A full-body embedding here was neither stored nor bounded.
    """
    try:
        parent = get_memory(parent_id)
        if parent and parent.get("created_at"):
            for neighbor in find_temporal_neighbors(parent_id, parent["created_at"], limit=3):
                create_relationship(parent_id, neighbor["id"], "related_to", note="temporal_neighbor")
    except psycopg2.Error:
        logger.warning("temporal_relationship_discovery_failed parent=%s", parent_id, exc_info=True)


def merge_tags(existing, new_tags):
    """Existing tags first (order kept), then new ones; no duplicates.

    Capture stages must commute: the QD facts stage supplies the record's own
    tags while the QD tags stage enriches the same rows with upstream tags. A
    refresh therefore merges rather than overwrites, so neither stage undoes the
    other (D N-4: 280 rows flipped every hour).
    """
    seen, merged = set(), []
    for tag in list(existing or []) + list(new_tags or []):
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged


def ingest_content(content, source_type, source_url=None, project=None, *, refresh=False, source_metadata=None, memory_fields=None, parse_header=True):
    """Prepare every embedding first, then commit parent and chunks together.

    Identity is channel + URL (``project`` is descriptive, never identity, so a
    repaired or re-derived project tag cannot duplicate a source). Generated
    memories sharing a task URL are outside this captured-source namespace.
    Refresh keeps cited old chunk IDs as superseded history and atomically
    installs a fresh active chunk set. A non-NULL resolved project is written
    on refresh; a NULL one never erases an existing tag. Captured ``tags`` are
    merged into the existing row's tags, never substituted for them.
    """
    meta, body = parse_metadata_header(content) if parse_header else ({}, content)
    if source_metadata is not None:
        if not isinstance(source_metadata, dict):
            raise ValueError("source_metadata must be a mapping")
        meta.update(source_metadata)
    fields = dict(memory_fields or {})
    if set(fields) - {"type", "title", "confidence", "tags", "mem_class", "encoding_context"}:
        raise ValueError("Unsupported captured-memory field")
    from src.validation import FILTERABLE_TYPES, MEMORY_CLASSES, SOURCE_TYPES, one_of, require_text, clean_tags
    memory_type = one_of(fields.pop("type", "source"), FILTERABLE_TYPES, "type")
    title = fields.pop("title", meta.get("title", "Untitled"))
    explicit_class = fields.pop("mem_class", None)
    if explicit_class is not None:
        one_of(explicit_class, MEMORY_CLASSES, "mem_class")
    require_text(title, "title")
    if "tags" in fields:
        fields["tags"] = clean_tags(fields["tags"])
    if "confidence" in fields:
        import math
        confidence = fields["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and between zero and one")
    meta["capture_identity"] = True
    source_url = source_url or meta.get("source")
    if not isinstance(source_url, str) or not source_url.strip():
        raise ValueError("Captured content requires a non-empty source_url")
    source_url = source_url.strip()
    one_of(source_type, SOURCE_TYPES, "source_type")
    resolved_project = normalize_project_tag(project if project is not None else meta.get("project"))
    if not body.strip():
        return None
    meta["depth_score"] = compute_depth_score(body)
    existing = find_captured_source(source_type, source_url, resolved_project)
    if existing and not refresh:
        return None
    if existing and existing["content"] == body:
        with transaction() as conn:
            lock_source_identity(source_type, source_url, resolved_project, conn=conn)
            current = get_memory(existing["id"], conn=conn)
            if current is not None and "tags" in fields:
                fields["tags"] = merge_tags(current.get("tags"), fields["tags"])
            updates = {"title": title, "type": memory_type,
                       "mem_class": explicit_class or classify_memory(memory_type, body), **fields}
            if resolved_project is not None:
                updates["project"] = resolved_project
            if current and current["content"] == body:
                changed = any(current.get(k) != v for k, v in updates.items()) or any(
                    (current.get("metadata") or {}).get(k) != v for k, v in meta.items())
                if changed:
                    meta["capture_updated_at"] = datetime.now(timezone.utc).isoformat()
                    update_fields(current["id"], updates, metadata_patch=meta, conn=conn)
                    # Provenance-only refresh reaches the active chunks as well.
                    with conn.cursor() as cur:
                        cur.execute("SELECT id FROM memories WHERE parent_id=%s AND status='active' ORDER BY id FOR UPDATE", (str(current["id"]),))
                        child_ids = [row[0] for row in cur.fetchall()]
                    for child_id in child_ids:
                        update_fields(child_id, {k: v for k, v in updates.items() if k != "title"}, metadata_patch=meta, conn=conn)
                return str(current["id"]) if changed else None
    chunks = chunk_by_section(body)
    prepared = [(chunk, generate_embedding(chunk)) for chunk in chunks if chunk.strip()]
    if not prepared:
        return None
    with transaction() as conn:
        lock_source_identity(source_type, source_url, resolved_project, conn=conn)
        existing = find_captured_source(source_type, source_url, resolved_project, conn=conn)
        if existing and (not refresh or existing["content"] == body):
            return None
        meta["capture_updated_at"] = datetime.now(timezone.utc).isoformat()
        if existing:
            parent_id = str(existing["id"])
            if "tags" in fields:
                current = get_memory(parent_id, conn=conn)
                fields["tags"] = merge_tags(current.get("tags") if current else None, fields["tags"])
            # Retaining prior chunk rows preserves downstream references and exact
            # evidence. Retrieval defaults and caller filters can exclude history.
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET status = 'superseded', updated_at = now() WHERE parent_id = %s AND status = 'active'", (parent_id,))
            # The fresh chunk set carries the vectors; a parent embedded as a
            # single leaf by an older writer must not keep a vector of the old
            # text next to them (duplicate, stale retrieval hit — D N-3).
            update_fields(parent_id, {"title": title, "content": body, "embedding": None,
                          "mem_class": explicit_class or classify_memory(memory_type, body), "type": memory_type,
                          **({"project": resolved_project} if resolved_project is not None else {}), **fields},
                          metadata_patch=meta, conn=conn)
        else:
            parent_id = create_memory(type=memory_type, title=title, content=body,
                source_url=source_url, source_type=source_type, metadata=meta,
                mem_class=explicit_class or classify_memory(memory_type, body), project=resolved_project, **fields, conn=conn)
        for i, (chunk, embedding) in enumerate(prepared):
            create_memory(type=memory_type, title=f"{title} (chunk {i+1}/{len(prepared)})",
                content=chunk, embedding=embedding, source_url=source_url, source_type=source_type,
                metadata={**meta, "chunk_index": i, "total_chunks": len(prepared)},
                parent_id=parent_id, mem_class=explicit_class or classify_memory(memory_type, chunk),
                project=resolved_project, **fields, conn=conn)
    _discover_relationships(parent_id, body)
    return parent_id


def ingest_batch(items, source_type, *, refresh=False):
    """Ingest each captured identity once per batch; reject missing identities.

    One bad item never aborts the channel: any ``Exception`` (provider, database,
    validation, or a programming error such as TypeError/KeyError on a malformed
    item) is logged with its traceback and counted in ``failed`` so it surfaces in
    the job log and stats, and the batch continues. ``BaseException`` subclasses
    (KeyboardInterrupt, SystemExit, the dream cycle's RunInterrupted) propagate.
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0}
    already = set()
    for source_url, content in items:
        identity = (source_type, source_url)
        if identity in already and not refresh:
            stats["skipped"] += 1
            continue
        try:
            if not isinstance(source_url, str) or not source_url.strip():
                raise ValueError("Captured content requires a non-empty source_url")
            result = ingest_content(content, source_type, source_url, refresh=refresh)
            if result:
                already.add(identity)
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            logger.error("Failed to ingest source: %s", type(exc).__name__, exc_info=True)
            stats["failed"] += 1
    return stats
