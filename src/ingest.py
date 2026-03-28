"""Ingestion pipeline — the single path from content to PostgreSQL.

All source types (Crawlee markdown, CLI chats, IDE chats, notes) are
normalized to markdown with metadata headers before reaching this module.

Pipeline: parse metadata header → chunk by section → embed → store.
"""

import logging
import re
from src.db import (
    create_memory,
    create_relationship,
    find_temporal_neighbors,
    get_memory,
    get_processed_source_urls,
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
            key = key.strip().lower().replace("-", "_")
            meta[key] = value.strip()

    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def chunk_by_section(body, max_chunk_chars=4000):
    """Split body text into chunks by section headers or paragraph groups.

    Uses markdown headers (##, ###) as natural split points.
    Falls back to splitting by double newlines if no headers found.
    Merges small chunks to avoid tiny fragments.
    """
    # Try splitting on markdown headers
    sections = re.split(r'\n(?=##\s)', body)

    if len(sections) <= 1:
        # No headers — split by double newlines into paragraph groups
        paragraphs = body.split("\n\n")
        sections = []
        current = []
        current_len = 0
        for para in paragraphs:
            if current_len + len(para) > max_chunk_chars and current:
                sections.append("\n\n".join(current))
                current = []
                current_len = 0
            current.append(para)
            current_len += len(para)
        if current:
            sections.append("\n\n".join(current))

    # Merge small sections
    merged = []
    buffer = ""
    for section in sections:
        if len(buffer) + len(section) < max_chunk_chars:
            buffer = buffer + "\n\n" + section if buffer else section
        else:
            if buffer:
                merged.append(buffer)
            buffer = section
    if buffer:
        merged.append(buffer)

    return merged if merged else [body]


def _discover_relationships(parent_id, body):
    """Discover and create semantic + temporal relationships for a parent memory.

    Finds up to 3 semantic neighbors (cosine similarity > 0.75) and up to 3
    temporal neighbors (±24h), creating `related_to` relationships for each.
    Silently skips duplicate relationship errors.
    """
    # Semantic neighbors: embed parent content, search top-3
    try:
        embedding = generate_embedding(body)
        neighbors = search_similar(embedding, limit=3)
        for neighbor in neighbors:
            nid = str(neighbor["id"])
            sim = neighbor.get("similarity", 0)
            if sim > 0.75 and nid != parent_id and neighbor.get("parent_id") is None:
                try:
                    create_relationship(
                        parent_id, nid, "related_to",
                        note=f"semantic_neighbor (sim={sim:.3f})",
                    )
                except Exception:
                    pass  # duplicate key or other error — skip
    except Exception:
        pass  # embedding or search failure — skip semantic discovery

    # Temporal neighbors: get parent record for created_at, find top-3 within ±24h
    try:
        parent_record = get_memory(parent_id)
        if parent_record and parent_record.get("created_at"):
            temporal = find_temporal_neighbors(
                parent_id, parent_record["created_at"], limit=3,
            )
            for neighbor in temporal:
                try:
                    create_relationship(
                        parent_id, neighbor["id"], "related_to",
                        note="temporal_neighbor",
                    )
                except Exception:
                    pass  # duplicate key or other error — skip
    except Exception:
        pass  # temporal discovery failure — skip


def ingest_content(content, source_type, source_url=None, project=None):
    """Ingest a single piece of content through the full pipeline.

    Args:
        content: Full markdown text with metadata header.
        source_type: e.g. 'youtube', 'article', 'kiro_cli_chat', 'kiro_ide_chat'
        source_url: Unique identifier for deduplication.
        project: Optional project tag for scoping.

    Returns:
        parent_id (str) or None if skipped/failed.
    """
    meta, body = parse_metadata_header(content)
    title = meta.get("title", "Untitled")

    # Resolve project: explicit param > header > None
    resolved_project = project if project is not None else meta.get("project")
    resolved_project = normalize_project_tag(resolved_project)

    if not body.strip():
        return None

    # Store parent record (full content, no embedding)
    mem_class = classify_memory("source", body)
    depth_score = compute_depth_score(body)
    meta["depth_score"] = depth_score
    # Parent record has no embedding — intentional. Vector search finds chunks,
    # not full documents. Parent serves as the grouping record for chunk hierarchy.
    parent_id = create_memory(
        type="source",
        title=title,
        content=body,
        source_url=source_url or meta.get("source"),
        source_type=source_type,
        metadata=meta,
        mem_class=mem_class,
        project=resolved_project,
    )

    # Relationship discovery for parent memories
    _discover_relationships(parent_id, body)

    # Chunk and embed
    chunks = chunk_by_section(body)
    for i, chunk_text in enumerate(chunks):
        embedding = generate_embedding(chunk_text)
        chunk_mem_class = classify_memory("source", chunk_text)
        create_memory(
            type="source",
            title=f"{title} (chunk {i+1}/{len(chunks)})",
            content=chunk_text,
            embedding=embedding,
            source_url=source_url or meta.get("source"),
            source_type=source_type,
            metadata={**meta, "chunk_index": i, "total_chunks": len(chunks)},
            parent_id=parent_id,
            mem_class=chunk_mem_class,
            project=resolved_project,
        )

    return parent_id


def ingest_batch(items, source_type):
    """Ingest multiple items, skipping already-processed ones.

    Args:
        items: iterable of (source_url, content) tuples.
        source_type: e.g. 'youtube', 'article', 'kiro_cli_chat'

    Returns:
        dict with counts: processed, skipped, failed.
    """
    already = get_processed_source_urls(source_type)
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for source_url, content in items:
        if source_url in already:
            stats["skipped"] += 1
            continue

        try:
            result = ingest_content(content, source_type, source_url)
            if result:
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            logger.error("Failed to ingest %s: %s", source_url, e, exc_info=True)
            stats["failed"] += 1

    return stats
