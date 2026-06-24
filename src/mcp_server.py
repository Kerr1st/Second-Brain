"""MCP server — exposes the Second Brain to AI agents.

Run: python -m src.mcp_server (stdio transport for Kiro/Claude Code)
"""

from mcp.server.fastmcp import FastMCP
from datetime import datetime, timedelta, timezone
from src.db import (
    create_memory, get_memory, update_memory, list_memories,
    create_relationship, get_relationships, find_temporal_neighbors,
    find_schemas_for_memory, get_schema_with_constituents,
)
from src.search import hybrid_search, rerank, increment_access_count
from src.embeddings import generate_embedding
from src.classify import classify_memory
from src.depth import compute_depth_score
from src.project import normalize_project_tag
from src import express

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
    project: str | None = None,
    encoding_context: str | None = None,
) -> str:
    """Create a new memory and generate its embedding.

    type: idea, synthesis, research, insight, question, decision, priority, project, connection, source
    source_type: youtube, article, pdf, course, kiro_cli_chat, kiro_ide_chat, notes, manual

    DEPTH REQUIREMENT for ideas/insights/decisions: explain WHAT, WHAT HAPPENS when violated, and WHY.
    Include 'Questions this answers:' with 3-5 natural-language queries.

    encoding_context: Optional. Describe what you were working on when creating this memory
    (e.g., "debugging auth flow", "reading about CLS theory"). Improves future retrieval
    by matching the cognitive context at search time (Godden & Baddeley 1975).
    """
    embedding = generate_embedding(content)
    mem_class = classify_memory(type, content)
    depth_score = compute_depth_score(content)
    project = normalize_project_tag(project)
    if metadata is None:
        metadata = {}
    metadata["depth_score"] = depth_score
    mid = create_memory(
        type=type, title=title, content=content, embedding=embedding,
        tags=tags, source_type=source_type, source_url=source_url, metadata=metadata,
        mem_class=mem_class, project=project, encoding_context=encoding_context,
    )

    warnings = []
    if type in ("idea", "synthesis", "insight", "decision"):
        if depth_score < 0.3:
            warnings.append("Low depth: add 'because...' or 'when X, then Y' to explain WHY.")
        if "questions this answers" not in content.lower():
            warnings.append("Missing 'Questions this answers:' — add 3-5 natural queries for better retrieval.")

    result = f"Created memory {mid}"
    if warnings:
        result += "\n⚠ " + "\n⚠ ".join(warnings)
    return result


@mcp.tool()
def memory_search(query: str, type: str | None = None, limit: int = 10, project: str | None = None,
                  source_type: str | None = None, since_days: int | None = None,
                  status: str | None = None) -> dict:
    """Semantic + keyword search across all memories. Hybrid retrieval (full-text +
    vector + RRF) with utility reranking (recency, type boost, token overlap, access
    reinforcement). Also returns `temporal_context` (memories near the top hit in time)
    and `schema_context` (higher-level notes the top hits belong to).

    Filters (all optional, combine freely):
    - type: memory type, e.g. 'decision', 'insight', 'synthesis'.
    - source_type: capture channel, e.g. 'distilled_chat', 'cli_chat', 'kiro_ide_chat', 'article'.
    - since_days: only memories created in the last N days (e.g. 30 = last month).
    - status: e.g. 'active' to exclude superseded items.
    - project: scope to a project tag.

    Iterate for best results: if hits are thin or off-target, refine the query or
    filters and search again, then call memory_read(id) for full content or
    memory_graph(id) to follow related memories."""
    embedding = generate_embedding(query)
    project = normalize_project_tag(project)
    created_after = None
    if since_days is not None and since_days > 0:
        created_after = datetime.now(timezone.utc) - timedelta(days=since_days)
    results = hybrid_search(query, embedding, limit=limit, type=type, project=project,
                            source_type=source_type, created_after=created_after, status=status)
    results = rerank(results, query, query_project=project)

    increment_access_count([str(r["id"]) for r in results])

    formatted = [
        {"id": str(r["id"]), "title": r["title"], "type": r["type"],
         "score": round(float(r.get("rerank_score", 0)), 3),
         "content": r["content"][:500], "tags": r["tags"]}
        for r in results
    ]

    # Temporal context: find neighbors of the top result, deduplicated
    temporal_context = []
    if results:
        top_id = str(results[0]["id"])
        top_record = get_memory(top_id)
        if top_record and top_record.get("created_at"):
            result_ids = {str(r["id"]) for r in results}
            neighbors = find_temporal_neighbors(top_id, top_record["created_at"], limit=3)
            for n in neighbors:
                if n["id"] not in result_ids:
                    created_at = n["created_at"]
                    temporal_context.append({
                        "id": n["id"],
                        "title": n["title"],
                        "type": n["type"],
                        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                        "relation": "temporal_neighbor",
                    })
            temporal_context = temporal_context[:3]

    # Schema context: if any top results belong to schemas, surface the schema
    schema_context = []
    if results:
        seen_schemas = set()
        for r in results[:5]:  # check top 5 results
            schemas = find_schemas_for_memory(str(r["id"]))
            for s in schemas:
                sid = str(s["id"])
                if sid not in seen_schemas:
                    seen_schemas.add(sid)
                    schema_context.append({
                        "id": sid,
                        "title": s["title"],
                        "type": "schema",
                        "content": (s.get("content") or "")[:300],
                    })
        schema_context = schema_context[:3]

    return {"results": formatted, "temporal_context": temporal_context, "schema_context": schema_context}


@mcp.tool()
def memory_read(memory_id: str) -> dict:
    """Read a single memory by ID and return its full content. Use this to expand a
    memory_search hit whose 500-char preview was truncated, before acting on it."""
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
        existing = get_memory(memory_id)
        existing_metadata = (existing.get("metadata") or {}) if existing else {}
        if isinstance(existing_metadata, str):
            import json as _json
            existing_metadata = _json.loads(existing_metadata)
        mem_type = fields.get("type") or (existing.get("type") if existing else "idea")
        fields["mem_class"] = classify_memory(mem_type, fields["content"])
        existing_metadata["depth_score"] = compute_depth_score(fields["content"])
        fields["metadata"] = existing_metadata
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
    """List/browse recent memories by filter (type, source_type, status) — newest first,
    no relevance ranking. Use memory_search for relevance; use this to enumerate or audit."""
    results = list_memories(type=type, status=status, source_type=source_type, limit=limit)
    return [
        {"id": str(r["id"]), "title": r["title"], "type": r["type"],
         "source_type": r["source_type"], "created_at": str(r["created_at"]),
         "tags": r["tags"]}
        for r in results
    ]


@mcp.tool()
def memory_graph(memory_id: str) -> dict:
    """Get a memory plus its linked relationships (target_id, relation_type, note).
    Use it to traverse from a memory_search hit to related memories, then memory_read
    those target_ids to follow a chain of reasoning."""
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


@mcp.tool()
def memory_brief(window_days: int = 14, use_llm: bool = False) -> str:
    """Surface what the second brain has synthesized but hasn't told you yet.

    Call this at the START of a work session to get oriented: it volunteers recent
    cross-project insights, detected contradictions (decisions you may be reversing),
    high-value memories you haven't revisited in a while, the latest activity digest,
    and open questions — connections you'd otherwise have to go looking for.

    This is the "Express" surface: the system pushing its synthesis to you rather than
    waiting to be queried. Use memory_search instead when you have a specific question.

    Args:
        window_days: how far back to pull recent dream-cycle insights (default 14).
        use_llm: write polished headlines via an LLM editor pass (slower, spawns a
            subprocess); default False uses fast deterministic ranking.

    Returns a scannable Markdown briefing (headlines first, detail below).
    """
    class _NoLLM:
        def invoke(self, *a, **k):
            raise RuntimeError("deterministic")

    briefing = express.compose_briefing(window_days=window_days)
    briefing = express.edit_briefing(briefing, invoker=None if use_llm else _NoLLM())
    return express.render_markdown(briefing)


if __name__ == "__main__":
    mcp.run()
