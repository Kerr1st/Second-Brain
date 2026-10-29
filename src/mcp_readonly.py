"""Read-only MCP surface for discovery agents. No mutation tools are registered.

Search intentionally omits access reinforcement: exploring the corpus cannot
change retrieval scores or create/update/delete any evidence.
"""
from mcp.server.fastmcp import FastMCP
from src import mcp_server as public

mcp = FastMCP("Second Brain Read Only")


@mcp.tool()
@public._boundary("dict")
def memory_search(query: str, type: str | None = None, limit: public.Int = 10,
                  project: str | None = None, source_type: str | None = None,
                  since_days: public.OptionalInt = None, status: str | None = None) -> dict:
    """Read-only search, without reinforcement writes or implicit expansion."""
    results, _ = public.search_hits(query, type=type, limit=limit, project=project,
                                    source_type=source_type, since_days=since_days, status=status)
    return {"results": [public.format_hit(r) for r in results]}


# Register named read endpoints explicitly. Never copy the mutable server registry.
for endpoint in (public.memory_read, public.memory_list, public.memory_recent_activity, public.memory_graph):
    mcp.add_tool(endpoint)

if __name__ == "__main__":
    mcp.run()
