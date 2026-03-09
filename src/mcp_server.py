"""MCP server — exposes the Second Brain to AI agents.

Run: python -m src.mcp_server (stdio transport for Kiro/Claude Code)
"""

from mcp.server.fastmcp import FastMCP
from src.db import (
    create_memory, get_memory, update_memory, list_memories,
    search_similar, create_relationship, get_relationships,
)
from src.embeddings import generate_embedding

mcp = FastMCP("Second Brain")


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
    """
    embedding = generate_embedding(content)
    mid = create_memory(
        type=type, title=title, content=content, embedding=embedding,
        tags=tags, source_type=source_type, source_url=source_url, metadata=metadata,
    )
    return f"Created memory {mid}"


@mcp.tool()
def memory_search(query: str, type: str | None = None, limit: int = 10) -> list[dict]:
    """Semantic search across all memories. Returns most relevant matches."""
    embedding = generate_embedding(query)
    results = search_similar(embedding, limit=limit, type=type)
    return [
        {"id": str(r["id"]), "title": r["title"], "type": r["type"],
         "similarity": round(float(r["similarity"]), 3),
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
