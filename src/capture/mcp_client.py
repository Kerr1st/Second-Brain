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

Failure taxonomy
----------------
On 2026-09-20/21 both capture jobs died at 01:00 and nobody noticed for days:
Slack on a stale Midway cookie (``AUTH_ERROR`` from ``list_my_channels``) and
Outlook on ``McpError: Connection closed`` from a ``grasp-mcp`` that exited
mid-session. Both exited 1 with a stack trace, indistinguishable from a bug.
So failures are now *classified* (:func:`classify_error`) and mapped to distinct
exit codes (:data:`EXIT_AUTH`, :data:`EXIT_TRANSPORT`), and the two kinds are
handled differently: an auth lapse needs a human (notify + exit 3, no retry),
a closed connection is retried exactly once per session (see
:meth:`McpClient.call_json`) before exiting 4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

DEFAULT_CALL_TIMEOUT = 120  # seconds per tool call
DEFAULT_MAX_RECONNECTS = 1  # reconnect budget for the whole session, not per call
DEFAULT_RECONNECT_BACKOFF = 2.0  # seconds before the single retry

# --------------------------------------------------------------------------- #
# Exit-code taxonomy for scheduled capture jobs                                #
# --------------------------------------------------------------------------- #
# A monitor (or a human grepping the log's last line) must be able to tell
# "needs human re-auth" from "the server died" from "our code is broken".
EXIT_OK = 0
EXIT_ERROR = 1        # unexpected failure: bug, bad data, DB problem
EXIT_AUTH = 3         # credentials lapsed — needs a human (mwinit / Graph re-auth)
EXIT_TRANSPORT = 4    # MCP server died or the stdio connection closed

KIND_EXIT = {"ok": EXIT_OK, "auth": EXIT_AUTH, "transport": EXIT_TRANSPORT, "error": EXIT_ERROR}


class McpToolError(RuntimeError):
    """A tool call returned an error or an unparseable payload.

    Collectors deliberately swallow this per item (one unreadable message must
    not sink the run). Auth and transport failures therefore do *not* use it —
    swallowing those is exactly how two days of Slack went missing.
    """


class McpAuthError(RuntimeError):
    """Credentials have lapsed; no amount of retrying will help."""


class McpTransportError(RuntimeError):
    """The MCP server process died or the stdio connection closed for good."""


class CaptureFailure(RuntimeError):
    """A classified, fatal capture failure carrying its exit code and the fix."""

    def __init__(self, kind: str, detail: str, fix: str | None = None):
        self.kind = kind if kind in KIND_EXIT else "error"
        self.detail = _one_line(detail)
        self.fix = fix
        super().__init__(self.detail)

    @property
    def exit_code(self) -> int:
        return KIND_EXIT[self.kind]

    @property
    def needs_reauth(self) -> bool:
        return self.kind == "auth"

    def log_line(self, channel: str) -> str:
        """The single actionable line a human greps for — no stack trace."""
        tail = f" — fix: {self.fix}" if self.fix else ""
        return f"FAILED ({self.kind}) {channel}: {self.detail}{tail}"


# --------------------------------------------------------------------------- #
# Error classification                                                         #
# --------------------------------------------------------------------------- #
# Matched case-insensitively against the whole exception chain.
_AUTH_MARKERS = (
    "auth_error", "failed to refresh authentication token", "midway-auth",
    "unauthorized", "invalid_grant", "token expired", "expired token",
    "authenticationfailed", "invalid authentication", "not authenticated",
    "re-authenticate", "mwinit",
)
# A connection that is gone: reconnecting once is worth a try.
_CLOSED_MARKERS = (
    "connection closed", "broken pipe", "closed resource", "closedresourceerror",
    "brokenresourceerror", "end of stream", "endofstream", "connection reset",
    "server process exited", "process exited", "transport closed", "session closed",
)
_CLOSED_TYPES = (
    "BrokenPipeError", "ConnectionResetError", "ClosedResourceError",
    "BrokenResourceError", "EndOfStream", "ProcessLookupError",
)
# Transport-ish but not worth reconnecting inside the same call (a hung server).
_HUNG_MARKERS = ("timed out after", "timeout", "exit status")


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception plus its causes/contexts and any ExceptionGroup members.

    anyio wraps stdio transport failures in task-group ExceptionGroups, so the
    interesting error is rarely the outermost one.
    """
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is None or any(e is s for s in seen):
            continue
        seen.append(e)
        todo.extend([e.__cause__, e.__context__])
        todo.extend(getattr(e, "exceptions", None) or [])
    return seen


def _matches(exc: BaseException, markers: tuple[str, ...], types: tuple[str, ...] = ()) -> bool:
    for e in _chain(exc):
        if type(e).__name__ in types:
            return True
        text = f"{type(e).__name__}: {e}".lower()
        if any(m in text for m in markers):
            return True
    return False


def is_auth_error(exc: BaseException) -> bool:
    """True when the failure is a lapsed credential rather than a sick server."""
    return isinstance(exc, McpAuthError) or _matches(exc, _AUTH_MARKERS)


def is_connection_closed(exc: BaseException) -> bool:
    """True when the stdio connection/server is gone — the reconnect trigger.

    Deliberately narrow: a *timeout* is not a closed connection, and an auth
    error never is (retrying a stale cookie just wastes a second).
    """
    if is_auth_error(exc):
        return False
    return _matches(exc, _CLOSED_MARKERS, _CLOSED_TYPES)


def classify_error(exc: BaseException) -> str:
    """``"auth"`` | ``"transport"`` | ``"error"`` — auth wins over transport."""
    if is_auth_error(exc):
        return "auth"
    if isinstance(exc, McpTransportError) or is_connection_closed(exc) \
            or _matches(exc, _HUNG_MARKERS):
        return "transport"
    return "error"


def auth_error_in_payload(payload: Any) -> str | None:
    """Return the auth message when a *successful* reply carries an auth error.

    slack-mcp reports the stale cookie as a tool error, but a server that
    answers 200 with ``{"error": "AUTH_ERROR"}`` must not read as healthy.
    """
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, str) and _matches(RuntimeError(f"{err} {payload.get('message') or ''}"),
                                             _AUTH_MARKERS):
            return _one_line(f"{err}: {payload.get('message') or ''}")
    return None


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #
def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', "'")


def notify(message: str, title: str = "Second Brain — capture") -> bool:
    """Best-effort macOS notification; never raises (the `|| true` equivalent)."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{_escape(_one_line(message, 180))}" '
                                f'with title "{_escape(title)}" sound name "Basso"'],
            timeout=10, check=False, capture_output=True)
        return True
    except Exception as e:  # noqa: BLE001 - a missing osascript must not mask the real failure
        logger.debug("notification failed: %s", e)
        return False


def report_failure(failure: CaptureFailure, channel: str, notify_fn=None) -> int:
    """Log the one actionable line, ping the user if a human is needed, return the exit code."""
    logger.error("%s", failure.log_line(channel))
    if failure.needs_reauth:
        (notify_fn or notify)(f"{channel} capture needs re-auth — {failure.fix or 'see the log'}",
                              f"Second Brain — {channel} auth")
    return failure.exit_code


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

    Survives the server dying mid-session: the first :meth:`call_json` that hits
    a closed connection tears the process down, waits ``reconnect_backoff`` and
    retries the call once. The budget (``max_reconnects``) is spent for the whole
    session, not per call, so a persistently dead server fails fast instead of
    grinding through hundreds of reconnects.
    """

    def __init__(self, command: str, args: list[str] | None = None,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT,
                 max_reconnects: int = DEFAULT_MAX_RECONNECTS,
                 reconnect_backoff: float = DEFAULT_RECONNECT_BACKOFF):
        self._params = StdioServerParameters(command=command, args=list(args or []))
        self._timeout = call_timeout
        self._max_reconnects = max(0, int(max_reconnects))
        self._reconnect_backoff = reconnect_backoff
        self.reconnects_used = 0
        self._stack: list = []
        self._session: ClientSession | None = None

    async def _connect(self) -> None:
        transport = stdio_client(self._params)
        read, write = await transport.__aenter__()
        self._stack.append(transport)
        session = ClientSession(read, write)
        await session.__aenter__()
        self._stack.append(session)
        await session.initialize()
        self._session = session

    async def _teardown(self, exc_type=None, exc=None, tb=None) -> None:
        # Unwind in reverse order; swallow shutdown noise from the server process.
        while self._stack:
            cm = self._stack.pop()
            try:
                await cm.__aexit__(exc_type, exc, tb)
            except Exception as e:  # noqa: BLE001 - best-effort teardown
                logger.debug("MCP teardown: %s", e)
        self._session = None

    async def __aenter__(self) -> "McpClient":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._teardown(exc_type, exc, tb)

    async def _call_once(self, tool: str, params: dict | None = None) -> Any:
        assert self._session is not None, "McpClient used outside 'async with'"
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool, params or {}), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise McpToolError(f"{tool}: timed out after {self._timeout}s") from e
        text = "".join(getattr(c, "text", "") for c in result.content)
        if getattr(result, "isError", False):
            # Distinguish a lapsed credential from an ordinary tool error: the
            # former must abort the run, the latter is skippable per item.
            if _matches(RuntimeError(text), _AUTH_MARKERS):
                raise McpAuthError(f"{tool}: {_one_line(text)}")
            raise McpToolError(f"{tool}: {text[:300]}")
        payload = parse_payload(text)
        auth = auth_error_in_payload(payload)
        if auth:
            raise McpAuthError(f"{tool}: {auth}")
        return payload

    async def call_json(self, tool: str, params: dict | None = None) -> Any:
        """Call ``tool`` and return its parsed JSON payload.

        Reconnects and retries **once per session** when the connection is gone
        (``McpError: Connection closed``, a broken pipe, or a server that
        exited). Anything else — including auth errors — propagates untouched.
        """
        try:
            return await self._call_once(tool, params)
        except Exception as e:
            if not is_connection_closed(e):
                raise
            if self.reconnects_used >= self._max_reconnects:
                raise McpTransportError(
                    f"{tool}: MCP server connection closed again after "
                    f"{self.reconnects_used} reconnect(s); giving up: {_one_line(e)}") from e
            self.reconnects_used += 1
            logger.warning("%s: connection closed (%s) — reconnecting once in %.1fs",
                           tool, _one_line(e, 120), self._reconnect_backoff)
            await self._teardown()
            await asyncio.sleep(self._reconnect_backoff)
            try:
                await self._connect()
                return await self._call_once(tool, params)
            except Exception as retry_exc:
                if is_auth_error(retry_exc):
                    raise  # the server came back and told us the token is stale
                raise McpTransportError(
                    f"{tool}: reconnect failed, MCP server is not answering: "
                    f"{_one_line(retry_exc)}") from retry_exc
