"""MCP server — exposes the Second Brain to AI agents.

Run: python -m src.mcp_server (stdio transport for Kiro/Claude Code)
"""

from mcp.server.fastmcp import FastMCP
from src.db import (
    create_memory, get_memory, update_memory, list_memories,
    search_similar, create_relationship, get_relationships,
    hybrid_search, rerank, increment_access_count,
)
from src.embeddings import generate_embedding

mcp = FastMCP("Second Brain")


import re as _re

_DEPTH_RE = _re.compile(
    r"(because|when .{5,40}(?:then|,)|the fix|the cost|this means|"
    r"which (?:causes|leads|means)|→|producing \d|cost us|"
    r"so (?:that|each|every)|if you don.t|without .{3,30} you)",
    _re.IGNORECASE,
)


@mcp.tool()
def memory_create(
    type: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_type: str | None = None,
    source_url: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Create a new memory and generate its embedding.

    type: idea, synthesis, research, insight, question, decision, priority, project, connection, source
    source_type: youtube, article, pdf, course, kiro_cli_chat, kiro_ide_chat, notes, manual

    DEPTH REQUIREMENT for ideas/insights/decisions: explain WHAT, WHAT HAPPENS when violated, and WHY.
    Include 'Questions this answers:' with 3-5 natural-language queries.
    """
    embedding = generate_embedding(content)
    mid = create_memory(
        type=type, title=title, content=content, embedding=embedding,
        tags=tags, source_type=source_type, source_url=source_url, metadata=metadata,
    )

    # Quality warnings for knowledge-type memories
    warnings = []
    if type in ("idea", "synthesis", "insight", "decision"):
        if len(_DEPTH_RE.findall(content)) < 2:
            warnings.append("Low depth: add 'because...' or 'when X, then Y' to explain WHY.")
        if "questions this answers" not in content.lower():
            warnings.append("Missing 'Questions this answers:' — add 3-5 natural queries for better retrieval.")

    result = f"Created memory {mid}"
    if warnings:
        result += "\n⚠ " + "\n⚠ ".join(warnings)
    return result


@mcp.tool()
def memory_search(query: str, type: str | None = None, limit: int = 10) -> list[dict]:
    """Semantic + keyword search across all memories. Uses hybrid retrieval
    (BM25 + vector embeddings + RRF) with utility reranking (recency, type boost,
    token overlap, retrieval reinforcement)."""
    embedding = generate_embedding(query)
    results = hybrid_search(query, embedding, limit=limit, type=type)
    results = rerank(results, query)

    # Retrieval reinforcement — bump access_count for returned memories
    increment_access_count([str(r["id"]) for r in results])

    return [
        {"id": str(r["id"]), "title": r["title"], "type": r["type"],
         "score": round(float(r.get("rerank_score", 0)), 3),
         "content": r["content"][:500], "tags": r["tags"]}
        for r in results
    ]


@mcp.tool()
def memory_read(memory_id: str) -> dict:
    """Read a single memory by ID. Returns full content."""
    mem = get_memory(memory_id)
    if not mem:
        return {"error": "Not found"}
    return {k: str(v) if k == "id" else v for k, v in mem.items() if k != "embedding"}


@mcp.tool()
def memory_update(memory_id: str, **fields) -> str:
    """Update fields on a memory. Pass only the fields to change (title, content, status, tags, etc.)."""
    if "content" in fields:
        fields["embedding"] = generate_embedding(fields["content"])
    update_memory(memory_id, **fields)
    return f"Updated memory {memory_id}"


@mcp.tool()
def memory_relate(
    source_id: str, target_id: str, relation_type: str, note: str | None = None
) -> str:
    """Create a relationship between two memories.

    relation_type: supports, contradicts, extends, inspires, derived_from, related_to
    """
    create_relationship(source_id, target_id, relation_type, note)
    return f"Related {source_id} → {relation_type} → {target_id}"


@mcp.tool()
def memory_list(
    type: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List memories with optional filters."""
    results = list_memories(type=type, status=status, source_type=source_type, limit=limit)
    return [
        {"id": str(r["id"]), "title": r["title"], "type": r["type"],
         "source_type": r["source_type"], "created_at": str(r["created_at"]),
         "tags": r["tags"]}
        for r in results
    ]


@mcp.tool()
def memory_graph(memory_id: str) -> dict:
    """Get a memory and all its relationships."""
    mem = get_memory(memory_id)
    if not mem:
        return {"error": "Not found"}
    rels = get_relationships(memory_id)
    return {
        "memory": {"id": str(mem["id"]), "title": mem["title"], "type": mem["type"]},
        "relationships": [
            {"target_id": str(r["target_id"]), "relation_type": r["relation_type"], "note": r["note"]}
            for r in rels
        ],
    }


if __name__ == "__main__":
    mcp.run()
