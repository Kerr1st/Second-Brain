"""Minimal MCP stdio client for scheduled capture jobs.

The Slack and Outlook collectors pull data through the same local MCP servers
that Kiro uses interactively (``slack-mcp`` via Midway, ``grasp-mcp`` via a
cached Microsoft Graph token). Both authenticate headlessly, which is what a
launchd job needs. This module hides the asyncio session plumbing and returns
parsed JSON.

Payload parsing is deliberately tolerant: ``slack-mcp`` prefixes every reply
with a plain-text "[CONTENT SAFETY DIRECTIVE]" preamble before the JSON
document, and individual message texts are wrapped in ``<slack-user-content>``
tags with whitespace "datamarked" as U+E000. :func:`parse_payload` finds the
JSON document; per-source modules undo the datamarking.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

DEFAULT_CALL_TIMEOUT = 120  # seconds per tool call


class McpToolError(RuntimeError):
    """A tool call returned an error or an unparseable payload."""


def parse_payload(raw: str) -> Any:
    """Return the JSON document embedded in a tool's text reply.

    Accepts a bare JSON document, or free text followed by a JSON object /
    array on its own line (the slack-mcp preamble case). Raises
    :class:`McpToolError` when no JSON can be found.
    """
    text = (raw or "").strip()
    if not text:
        raise McpToolError("empty tool reply")
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Preamble case: the document starts at the first line that begins with { or [.
    for opener in ("\n{", "\n["):
        idx = text.find(opener)
        while idx >= 0:
            try:
                return json.loads(text[idx + 1:])
            except ValueError:
                idx = text.find(opener, idx + 1)
    raise McpToolError(f"no JSON document in tool reply: {text[:200]!r}")


class McpClient:
    """Async context manager around one stdio MCP server process.

    Usage::

        async with McpClient(cmd, args) as client:
            data = await client.call_json("get_emails", {"folder": "inbox"})
    """

    def __init__(self, command: str, args: list[str] | None = None,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT):
        self._params = StdioServerParameters(command=command, args=list(args or []))
        self._timeout = call_timeout
        self._stack: list = []
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpClient":
        transport = stdio_client(self._params)
        read, write = await transport.__aenter__()
        self._stack.append(transport)
        session = ClientSession(read, write)
        await session.__aenter__()
        self._stack.append(session)
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Unwind in reverse order; swallow shutdown noise from the server process.
        while self._stack:
            cm = self._stack.pop()
            try:
                await cm.__aexit__(exc_type, exc, tb)
            except Exception as e:  # noqa: BLE001 - best-effort teardown
                logger.debug("MCP teardown: %s", e)
        self._session = None

    async def call_json(self, tool: str, params: dict | None = None) -> Any:
        """Call ``tool`` and return its parsed JSON payload."""
        assert self._session is not None, "McpClient used outside 'async with'"
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool, params or {}), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise McpToolError(f"{tool}: timed out after {self._timeout}s") from e
        text = "".join(getattr(c, "text", "") for c in result.content)
        if getattr(result, "isError", False):
            raise McpToolError(f"{tool}: {text[:300]}")
        return parse_payload(text)
