"""Ingestion pipeline — the single path from content to PostgreSQL.

All source types (Crawlee markdown, CLI chats, IDE chats, notes) are
normalized to markdown with metadata headers before reaching this module.

Pipeline: parse metadata header → chunk by section → embed → store.
"""

import re
from src.db import create_memory, get_processed_source_urls
from src.embeddings import generate_embedding


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


def ingest_content(content, source_type, source_url=None):
    """Ingest a single piece of content through the full pipeline.

    Args:
        content: Full markdown text with metadata header.
        source_type: e.g. 'youtube', 'article', 'kiro_cli_chat', 'kiro_ide_chat'
        source_url: Unique identifier for deduplication.

    Returns:
        parent_id (str) or None if skipped/failed.
    """
    meta, body = parse_metadata_header(content)
    title = meta.get("title", "Untitled")

    if not body.strip():
        return None

    # Store parent record (full content, no embedding)
    parent_id = create_memory(
        type="source",
        title=title,
        content=body,
        source_url=source_url or meta.get("source"),
        source_type=source_type,
        metadata=meta,
    )

    # Chunk and embed
    chunks = chunk_by_section(body)
    for i, chunk_text in enumerate(chunks):
        embedding = generate_embedding(chunk_text)
        create_memory(
            type="source",
            title=f"{title} (chunk {i+1}/{len(chunks)})",
            content=chunk_text,
            embedding=embedding,
            source_url=source_url or meta.get("source"),
            source_type=source_type,
            metadata={**meta, "chunk_index": i, "total_chunks": len(chunks)},
            parent_id=parent_id,
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
            stats["failed"] += 1

    return stats
