"""MCP server — exposes the Second Brain to AI agents.

Run: python -m src.mcp_server (stdio transport for Kiro/Claude Code)
"""

import re
from mcp.server.fastmcp import FastMCP
from src.db import (
    create_memory, get_memory, update_memory, list_memories,
    search_similar, create_relationship, get_relationships,
    hybrid_search, rerank, increment_access_count,
)
from src.embeddings import generate_embedding

mcp = FastMCP("Second Brain")

_DEPTH_RE = re.compile(
    r"(because|when .{5,40}(?:then|,)|the fix|the cost|this means|"
    r"which (?:causes|leads|means)|→|producing \d|cost us|"
    r"so (?:that|each|every)|if you don.t|without .{3,30} you)",
    re.IGNORECASE,
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
    out = {}
    for k, v in mem.items():
        if k == "embedding":
            continue
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k == "id":
            out[k] = str(v)
        else:
            out[k] = v
    return out


@mcp.tool()
def memory_update(
    memory_id: str,
    title: str | None = None,
    content: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
    type: str | None = None,
) -> str:
    """Update fields on a memory. Pass only the fields to change."""
    fields = {k: v for k, v in {
        "title": title, "content": content, "status": status,
        "tags": tags, "summary": summary, "type": type,
    }.items() if v is not None}
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


@mcp.tool()
def memory_learn(
    content: str,
    topics: str,
    source: str | None = None,
) -> str:
    """Internalize external knowledge by connecting it to what you already know.

    Use when the user shares an article, talk, paper, video, or any external content.

    This is a TWO-STEP process:
    STEP 1 (this tool): Provide the content and 2-4 key topics. The tool searches
    existing memories for related knowledge and returns both together.

    STEP 2 (you, the agent): Synthesize connections and call memory_create for each
    atomic insight. Each insight MUST:
    1. State the insight (WHAT)
    2. Connect it to an existing memory — extends, contradicts, or explains it (HOW)
    3. Explain the causal mechanism — why this matters for future work (WHY)
    4. Include 'Questions this answers:' as future work queries
    5. Prefix the title with 'From [source]:' for attribution

    Create ONE memory per atomic insight, not one giant summary.
    The value is in the CONNECTIONS to existing knowledge, not the summary.
    """
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]

    # Search existing memories for each topic
    related_sections = []
    for topic in topic_list:
        embedding = generate_embedding(f"principles and lessons for {topic}")
        results = hybrid_search(f"principles and lessons for {topic}", embedding, limit=5)
        results = rerank(results, topic)
        if results:
            lines = [f"### Existing knowledge: \"{topic}\""]
            for r in results[:3]:
                lines.append(f"- [{r['type']}] {r['title']}")
                lines.append(f"  {r['content'][:300]}")
            related_sections.append("\n".join(lines))

    source_line = f"**Source:** {source}\n" if source else ""
    related = "\n\n".join(related_sections) if related_sections else "(No related memories found — this is new territory)"

    return "\n".join([
        "# External Knowledge to Internalize\n",
        source_line,
        "## New Content\n",
        content,
        "\n## Related Existing Memories\n",
        related,
        "\n## Your Task",
        "Synthesize: for each key insight in the new content, call memory_create with:",
        "1. WHAT is the insight",
        "2. HOW it connects to an existing memory above (extends, contradicts, explains)",
        "3. WHY this matters for future work (causal mechanism)",
        "4. 'Questions this answers:' phrased as future work queries",
        f"5. Prefix title with: 'From {source or '[source]'}:'",
        "\nCreate ONE memory per atomic insight, not one giant summary.",
    ])


if __name__ == "__main__":
    mcp.run()
