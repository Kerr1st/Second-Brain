"""The discovery agent has no route to writable corpus or built-in tools."""
import asyncio
from unittest.mock import patch

import pytest
from src import mcp_readonly


def test_registry_contains_only_explicit_read_tools():
    names = {tool.name for tool in asyncio.run(mcp_readonly.mcp.list_tools())}
    assert names == {"memory_search", "memory_read", "memory_list", "memory_recent_activity", "memory_graph"}


def test_search_does_not_reinforce_access_or_expose_mutators():
    with patch.object(mcp_readonly.public, "generate_embedding", return_value=[1.0]), \
         patch.object(mcp_readonly.public, "hybrid_search", return_value=[]), \
         patch.object(mcp_readonly.public, "rerank", return_value=[]), \
         patch.object(mcp_readonly.public, "increment_access_count") as reinforce:
        assert mcp_readonly.memory_search("test")["results"] == []
        reinforce.assert_not_called()


def test_search_boundary_validates_source_before_embedding():
    with patch.object(mcp_readonly.public, "generate_embedding") as embed:
        result = mcp_readonly.memory_search("test", source_type="off-taxonomy")
    assert "error" in result
    embed.assert_not_called()


def test_search_boundary_omits_database_diagnostics():
    import psycopg2
    with patch.object(mcp_readonly.public, "generate_embedding", return_value=[1.0]), \
         patch.object(mcp_readonly.public, "hybrid_search", side_effect=psycopg2.OperationalError("secret database host")):
        result = mcp_readonly.memory_search("test")
    assert "temporarily unavailable" in result["error"]
    assert "secret" not in str(result)
