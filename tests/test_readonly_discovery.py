"""The discovery agent has no route to writable corpus or built-in tools."""
import asyncio
from unittest.mock import patch

import pytest
from src import mcp_readonly
from src.backends.claude_code import ClaudeCodeInvoker, _SECOND_BRAIN_MCP
from src.backends.base import CorpusRoutingError
from src.backends.resolver import Resolver, RoleBackend


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


def test_claude_tools_have_no_builtins_and_readonly_database_default(tmp_path):
    inv = ClaudeCodeInvoker("global.anthropic.claude-sonnet-5")
    with patch.object(inv, "_write_mcp_config", return_value=str(tmp_path / "mcp.json")):
        flags = inv._tool_flags(name="explorer", needs_tools=True)
    assert flags[flags.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in flags
    entry = _SECOND_BRAIN_MCP["second-brain"]
    assert entry["args"] == ["-m", "src.mcp_readonly"]
    assert entry["env"]["PGOPTIONS"] == "-c default_transaction_read_only=on"


@pytest.mark.parametrize("backend", ["kiro", "codex", "bedrock"])
def test_explorer_fails_before_adapter_construction_without_enforced_surface(backend):
    with pytest.raises(CorpusRoutingError):
        Resolver({"explorer": RoleBackend("explorer", backend, "safe-model")})


def test_raw_debug_is_off_by_default_and_requires_private_directory(tmp_path, monkeypatch):
    import os
    inv = ClaudeCodeInvoker("safe-model")
    monkeypatch.delenv("SECOND_BRAIN_RAW_DEBUG_DIR", raising=False)
    inv._dump_raw_debug("ignored", "synthetic text")
    assert not list(tmp_path.iterdir())
    private = tmp_path / "private"
    monkeypatch.setenv("SECOND_BRAIN_RAW_DEBUG_DIR", str(private))
    inv._dump_raw_debug("ignored", "synthetic text")
    files = list(private.iterdir())
    assert len(files) == 1 and files[0].read_text() == "synthetic text"
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert private.stat().st_mode & 0o777 == 0o700
    os.chmod(private, 0o755)
    inv._dump_raw_debug("ignored", "should not be written")
    assert len(list(private.iterdir())) == 1


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
