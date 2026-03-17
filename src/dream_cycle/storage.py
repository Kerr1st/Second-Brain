"""Storage operations — memory creation, updates, and deduplication."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.db import create_memory, create_relationship, get_memory, search_similar, update_memory
from src.embeddings import generate_embedding
from src.models import CandidateInsight

logger = logging.getLogger(__name__)


def store_accepted(candidate: CandidateInsight) -> str:
    """Create memory + relationships for an accepted insight.

    Handles CREATE, UPDATE, and SUPERSEDE operations. After any operation,
    creates all proposed relationships from the candidate.

    Args:
        candidate: The accepted CandidateInsight to store.

    Returns:
        The memory ID (string) of the created or updated memory.
    """
    if candidate.operation == "UPDATE":
        update_memory(
            candidate.target_memory_id,
            content=candidate.content,
            embedding=generate_embedding(candidate.content),
            metadata={"last_dream_cycle_update": datetime.now(timezone.utc).isoformat()},
        )
        memory_id = candidate.target_memory_id

    elif candidate.operation == "SUPERSEDE":
        # Check if target exists and is active
        target = get_memory(candidate.target_memory_id)
        if target is None or target.get("status") == "superseded":
            logger.warning(
                "SUPERSEDE target %s does not exist or is already superseded, downgrading to CREATE",
                candidate.target_memory_id,
            )
            # Downgrade to CREATE
            embedding = generate_embedding(candidate.content)
            memory_id = create_memory(
                type=candidate.type,
                title=candidate.title,
                content=candidate.content,
                embedding=embedding,
                tags=["dream-cycle", candidate.schema_operation],
                metadata={
                    "dream_cycle": True,
                    "strategy": candidate.strategy_that_found_it,
                    "source_memories": candidate.source_memories,
                    "confidence": candidate.confidence,
                },
            )
        else:
            # Create new memory
            embedding = generate_embedding(candidate.content)
            memory_id = create_memory(
                type=candidate.type,
                title=candidate.title,
                content=candidate.content,
                embedding=embedding,
                tags=["dream-cycle", candidate.schema_operation],
                metadata={
                    "dream_cycle": True,
                    "strategy": candidate.strategy_that_found_it,
                    "source_memories": candidate.source_memories,
                    "confidence": candidate.confidence,
                },
            )
            # Mark old memory as superseded
            update_memory(candidate.target_memory_id, status="superseded")
            # Create superseded_by relationship
            create_relationship(
                candidate.target_memory_id,
                memory_id,
                "superseded_by",
                candidate.supersedes_reason,
            )

    else:
        # CREATE (default)
        embedding = generate_embedding(candidate.content)
        memory_id = create_memory(
            type=candidate.type,
            title=candidate.title,
            content=candidate.content,
            embedding=embedding,
            tags=["dream-cycle", candidate.schema_operation],
            metadata={
                "dream_cycle": True,
                "strategy": candidate.strategy_that_found_it,
                "source_memories": candidate.source_memories,
                "confidence": candidate.confidence,
            },
        )

    # Create all proposed relationships
    for rel in candidate.relationships:
        create_relationship(
            memory_id,
            rel["target_id"],
            rel["relation_type"],
            rel.get("note"),
        )

    return memory_id


def check_duplicate(content: str, threshold: float = 0.85) -> str | None:
    """Embedding similarity check against existing memories.

    Skips chunk memories (parent_id not null). Uses strict greater-than
    comparison against the similarity threshold.

    Args:
        content: The candidate insight content text.
        threshold: Cosine similarity threshold (default 0.85).

    Returns:
        Existing memory ID if duplicate found, None otherwise.
    """
    embedding = generate_embedding(content)
    results = search_similar(embedding, limit=5, status="active")

    for result in results:
        if result.get("parent_id") is not None:
            continue  # skip chunk memories
        similarity = result.get("similarity", 0)
        if similarity > threshold:
            return str(result["id"])

    return None
