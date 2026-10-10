"""Explorer runs on the read-only MCP server with an exact tool allowlist (F-145).

No model or CLI is invoked: ``subprocess.run`` is patched and the generated
argv and ``--mcp-config`` file are inspected.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.backends.claude_code import READONLY_TOOL_GRANTS, ClaudeCodeInvoker

MUTATING = {"memory_create", "memory_update", "memory_relate", "memory_learn", "memory_brief"}


def _stream_with_tool_result() -> str:
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__second-brain__memory_search", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": False, "content": "[]"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "[]"},
    ]
    return "\n".join(json.dumps(e) for e in events)


def _run(stage):
    captured = {}

    def fake(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        if "--mcp-config" in cmd:
            with open(cmd[cmd.index("--mcp-config") + 1], encoding="utf-8") as f:
                captured["config"] = json.load(f)
        return MagicMock(returncode=0, stdout=_stream_with_tool_result(), stderr="")

    with patch("src.backends.claude_code.subprocess.run", side_effect=fake):
        ClaudeCodeInvoker(model="global.anthropic.claude-sonnet-5").invoke(
            "sys", "msg", tools=True, stage=stage)
    return captured


def test_explorer_config_points_at_mcp_readonly():
    cap = _run("explorer")
    servers = cap["config"]["mcpServers"]
    assert list(servers) == ["second-brain"]
    assert servers["second-brain"]["args"] == ["-m", "src.mcp_readonly"]
    assert "src.mcp_server" not in json.dumps(cap["config"])


def test_explorer_argv_grants_exactly_the_allowlist_and_no_builtins():
    cmd = _run("explorer")["cmd"]
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""  # built-ins (Bash/Read/Write/WebFetch) off
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert allowed == [f"mcp__second-brain__{t}" for t in READONLY_TOOL_GRANTS["explorer"]]
    assert set(READONLY_TOOL_GRANTS["explorer"]) == {
        "memory_search", "memory_list", "memory_read", "memory_recent_activity"}
    assert not MUTATING & {a.rsplit("__", 1)[1] for a in allowed}


def test_allowlist_matches_the_tools_the_explorer_prompt_uses():
    from src.backends.mcp_probe import MCPStartupProbe
    from src.prompts import explorer as prompt_mod
    text = Path(prompt_mod.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r"\bmemory_(?:search|list|read|recent_activity|graph|create|update|relate|learn|brief)\b", text))
    used.add(MCPStartupProbe.TOOL)
    assert used == set(READONLY_TOOL_GRANTS["explorer"])


def test_other_tool_stages_keep_the_full_server():
    cap = _run("thinker")
    assert cap["config"]["mcpServers"]["second-brain"]["args"] == ["-m", "src.mcp_server"]
    assert "--allowedTools" not in cap["cmd"]


def test_mcp_readonly_registers_no_mutating_tool():
    pytest.importorskip("mcp.server.fastmcp")
    import asyncio
    from src import mcp_readonly
    tools = {t.name for t in asyncio.run(mcp_readonly.mcp.list_tools())}
    assert tools == {"memory_search", "memory_read", "memory_list",
                     "memory_recent_activity", "memory_graph"}
    assert not tools & MUTATING
    assert set(READONLY_TOOL_GRANTS["explorer"]) <= tools
