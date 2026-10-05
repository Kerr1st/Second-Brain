"""Read-only MCP surface for discovery agents. No mutation tools are registered.

Search intentionally omits access reinforcement: exploring the corpus cannot
change retrieval scores or create/update/delete any evidence.
"""
from datetime import datetime, timedelta, timezone
from mcp.server.fastmcp import FastMCP
from src import mcp_server as public
from src.validation import require_text, optional_one_of, clamp_limit, clamp_days, FILTERABLE_TYPES, MEMORY_STATUSES, SOURCE_TYPES

mcp = FastMCP("Second Brain Read Only")


@mcp.tool()
@public._boundary("dict")
def memory_search(query: str, type: str | None = None, limit: int = 10,
                  project: str | None = None, source_type: str | None = None,
                  since_days: int | None = None, status: str | None = None) -> dict:
    """Read-only search, without reinforcement writes or implicit expansion."""
    query = require_text(query, "query")
    type = optional_one_of(type, FILTERABLE_TYPES, "type")
    status = optional_one_of(status, MEMORY_STATUSES, "status")
    source_type = optional_one_of(source_type, SOURCE_TYPES, "source_type")
    limit = clamp_limit(limit)
    since_days = clamp_days(since_days, "since_days")
    project = public.normalize_project_tag(project)
    after = datetime.now(timezone.utc) - timedelta(days=since_days) if since_days else None
    results = public.hybrid_search(query, public.generate_embedding(query), limit=limit,
        type=type, project=project, source_type=source_type, created_after=after, status=status)
    results = public.rerank(results, query, query_project=project)
    return {"results": [{"id": str(r["id"]), "title": r["title"], "type": r["type"],
        "content": r["content"][:500], "source_type": r.get("source_type"),
        "created_at": public._iso(r.get("created_at")), "tags": r.get("tags", []),
        "score": round(float(r.get("rerank_score", 0)), 3)} for r in results]}


# Register named read endpoints explicitly. Never copy the mutable server registry.
for endpoint in (public.memory_read, public.memory_list, public.memory_recent_activity, public.memory_graph):
    mcp.add_tool(endpoint)

if __name__ == "__main__":
    mcp.run()
