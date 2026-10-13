"""Hermetic tests for the MCP failure taxonomy and the bounded reconnect.

No network, no DB, no MCP server process: the stdio connect is replaced by a
fake session, so ``_call_once`` (payload parsing, auth detection) and
``call_json`` (reconnect-once) run exactly as in production.

Regression cover for 2026-09-20/21, when a stale Midway cookie and a grasp-mcp
that exited mid-session both surfaced as `exit 1` + stack trace and cost days of
silent data loss.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from src.capture import mcp_client as mc
from src.capture.mcp_client import (
    EXIT_AUTH, EXIT_ERROR, EXIT_OK, EXIT_TRANSPORT, CaptureFailure, McpAuthError, McpClient,
    McpToolError, McpTransportError, auth_error_in_payload, classify_error, is_connection_closed,
    report_failure,
)

# The two errors that actually happened, verbatim in shape.
SLACK_AUTH_PAYLOAD = ('{"error":"AUTH_ERROR","message":"Failed to refresh authentication token: '
                      'Request to IDP URL https://midway-auth.amazon.com/... failed"}')


def closed() -> McpError:
    """A fresh 'Connection closed' (exceptions accumulate __context__ when reused)."""
    return McpError(ErrorData(code=0, message="Connection closed"))


# --------------------------------------------------------------------------- #
# Classification                                                               #
# --------------------------------------------------------------------------- #
class TestClassifyError:
    def test_slack_stale_midway_cookie_is_auth(self):
        exc = McpToolError(f"list_my_channels: {SLACK_AUTH_PAYLOAD}")
        assert classify_error(exc) == "auth"
        assert is_connection_closed(exc) is False  # retrying a stale cookie is pointless

    def test_grasp_connection_closed_is_transport(self):
        assert classify_error(closed()) == "transport"
        assert is_connection_closed(closed()) is True

    @pytest.mark.parametrize("exc", [
        BrokenPipeError(32, "Broken pipe"),
        anyio.ClosedResourceError(),
        ConnectionResetError("Connection reset by peer"),
        RuntimeError("server process exited with code 1"),
    ])
    def test_other_dead_connection_shapes_are_transport(self, exc):
        assert classify_error(exc) == "transport" and is_connection_closed(exc)

    def test_transport_error_nested_in_an_exception_group_is_found(self):
        # anyio wraps stdio failures in task-group ExceptionGroups.
        group = BaseExceptionGroup("unhandled errors in a TaskGroup", [closed()])
        assert classify_error(group) == "transport" and is_connection_closed(group)

    def test_cause_chain_is_walked(self):
        try:
            raise closed()
        except McpError as inner:
            outer = RuntimeError("capture failed")
            outer.__cause__ = inner
        assert classify_error(outer) == "transport"

    def test_hung_server_is_transport_but_not_a_closed_connection(self):
        exc = McpToolError("get_emails: timed out after 180s")
        assert classify_error(exc) == "transport"
        assert is_connection_closed(exc) is False  # no reconnect on a timeout

    def test_expired_graph_token_is_auth_not_transport(self):
        assert classify_error(McpToolError("get_emails: 401 unauthorized, token expired")) == "auth"

    def test_ordinary_bug_is_error(self):
        assert classify_error(ValueError("bad row")) == "error"
        assert classify_error(McpToolError("get_messages: rate limited")) == "error"

    def test_auth_wins_over_transport_when_both_appear(self):
        exc = McpAuthError("list_my_channels: AUTH_ERROR (connection closed afterwards)")
        assert classify_error(exc) == "auth"


class TestAuthErrorInPayload:
    def test_detects_auth_error_in_a_200_reply(self):
        import json
        assert "AUTH_ERROR" in auth_error_in_payload(json.loads(SLACK_AUTH_PAYLOAD))

    def test_healthy_and_unrelated_payloads_are_clean(self):
        assert auth_error_in_payload({"sections": []}) is None
        assert auth_error_in_payload({"error": "RATE_LIMITED", "message": "slow down"}) is None
        assert auth_error_in_payload([1, 2]) is None


# --------------------------------------------------------------------------- #
# Exit-code taxonomy + reporting                                               #
# --------------------------------------------------------------------------- #
class TestCaptureFailure:
    def test_exit_codes_are_distinct_and_stable(self):
        assert (EXIT_OK, EXIT_ERROR, EXIT_AUTH, EXIT_TRANSPORT) == (0, 1, 3, 4)
        assert CaptureFailure("auth", "x").exit_code == 3
        assert CaptureFailure("transport", "x").exit_code == 4
        assert CaptureFailure("error", "x").exit_code == 1
        assert CaptureFailure("nonsense", "x").exit_code == 1  # unknown kind degrades to 1

    def test_log_line_is_one_greppable_line_naming_the_fix(self):
        f = CaptureFailure("auth", "list_my_channels:\nAUTH_ERROR\n  token", fix="run `mwinit`")
        line = f.log_line("slack")
        assert "\n" not in line
        assert line.startswith("FAILED (auth) slack:") and "run `mwinit`" in line

    def test_only_auth_needs_a_human(self):
        assert CaptureFailure("auth", "x").needs_reauth is True
        assert CaptureFailure("transport", "x").needs_reauth is False


class TestReportFailure:
    def test_notifies_on_auth_only(self):
        sent = []
        auth = CaptureFailure("auth", "AUTH_ERROR", fix="run `mwinit`")
        assert report_failure(auth, "slack", notify_fn=lambda m, t=None: sent.append(m)) == EXIT_AUTH
        assert len(sent) == 1 and "mwinit" in sent[0]

        for kind, code in (("transport", EXIT_TRANSPORT), ("error", EXIT_ERROR)):
            assert report_failure(CaptureFailure(kind, "boom"), "outlook",
                                  notify_fn=lambda m, t=None: sent.append(m)) == code
        assert len(sent) == 1  # no extra notifications

    def test_uses_the_module_notify_by_default(self, monkeypatch):
        sent = []
        monkeypatch.setattr(mc, "notify", lambda m, t=None: sent.append((m, t)) or True)
        report_failure(CaptureFailure("auth", "x"), "slack")
        assert len(sent) == 1

    def test_notify_never_raises_when_osascript_is_missing(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("osascript")
        monkeypatch.setattr(mc.subprocess, "run", boom)
        assert mc.notify("hi") is False

    def test_notify_escapes_quotes_in_the_applescript_literal(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(mc.subprocess, "run", lambda argv, **k: seen.setdefault("argv", argv))
        assert mc.notify('say "hello" \\ now', title='t"x') is True
        script = seen["argv"][-1]
        assert script.count('"') % 2 == 0 and 'say' in script


# --------------------------------------------------------------------------- #
# McpClient: bounded reconnect                                                 #
# --------------------------------------------------------------------------- #
def _reply(text: str, is_error: bool = False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=is_error)


class FakeSession:
    """Stands in for mcp.ClientSession; each script entry is a reply or a raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool, params):
        self.calls.append((tool, params))
        item = self.script.pop(0) if self.script else _reply('{"ok": true}')
        if isinstance(item, BaseException):
            raise item
        return item


def _client(*sessions, **kw):
    """A connected McpClient whose (re)connect hands out the next fake session."""
    client = McpClient("/nonexistent/server", [], reconnect_backoff=0, **kw)
    queue = list(sessions)
    connects: list[FakeSession] = []

    async def _connect():
        if not queue:
            raise McpError(ErrorData(code=0, message="Connection closed"))
        client._session = queue.pop(0)
        connects.append(client._session)

    client._connect = _connect
    asyncio.run(_connect())  # initial connection
    client.connects = connects
    return client


class TestReconnect:
    def test_succeeds_on_the_second_attempt_and_reconnects_exactly_once(self):
        dead, fresh = FakeSession(closed()), FakeSession(_reply('{"emails": []}'))
        client = _client(dead, fresh)
        assert asyncio.run(client.call_json("get_emails", {"folder": "inbox"})) == {"emails": []}
        assert client.reconnects_used == 1
        assert len(client.connects) == 2                      # initial + one reconnect
        assert len(dead.calls) == 1 and len(fresh.calls) == 1  # the call is retried once, not twice
        assert fresh.calls[0] == ("get_emails", {"folder": "inbox"})

    def test_persistent_transport_failure_raises_transport_error(self):
        dead, still_dead = FakeSession(closed()), FakeSession(closed())
        client = _client(dead, still_dead)
        with pytest.raises(McpTransportError, match="reconnect failed"):
            asyncio.run(client.call_json("get_emails"))
        assert client.reconnects_used == 1  # bounded: no third attempt

    def test_reconnect_budget_is_per_session_not_per_call(self):
        first, second = FakeSession(closed()), FakeSession(_reply('{"ok": 1}'), closed())
        client = _client(first, second)
        assert asyncio.run(client.call_json("a")) == {"ok": 1}
        with pytest.raises(McpTransportError, match="giving up"):
            asyncio.run(client.call_json("b"))
        assert client.reconnects_used == 1 and len(client.connects) == 2

    def test_a_server_that_will_not_restart_is_transport_death(self):
        client = _client(FakeSession(closed()))  # no replacement session queued
        with pytest.raises(McpTransportError, match="not answering"):
            asyncio.run(client.call_json("get_emails"))

    def test_transport_error_is_not_a_tool_error_so_collectors_cannot_swallow_it(self):
        # collect_mail/collect wrap per-item calls in `except McpToolError: continue`;
        # a dead server must never be silently skipped that way.
        assert not issubclass(McpTransportError, McpToolError)
        assert not issubclass(McpAuthError, McpToolError)

    def test_no_reconnect_for_auth_errors(self):
        session = FakeSession(_reply(SLACK_AUTH_PAYLOAD, is_error=True))
        client = _client(session, FakeSession(_reply('{"ok": 1}')))
        with pytest.raises(McpAuthError, match="AUTH_ERROR"):
            asyncio.run(client.call_json("list_my_channels"))
        assert client.reconnects_used == 0 and len(client.connects) == 1

    def test_no_reconnect_for_ordinary_tool_errors(self):
        client = _client(FakeSession(_reply("rate limited", is_error=True)))
        with pytest.raises(McpToolError, match="rate limited"):
            asyncio.run(client.call_json("get_messages"))
        assert client.reconnects_used == 0

    def test_auth_error_discovered_after_the_reconnect_is_reported_as_auth(self):
        client = _client(FakeSession(closed()), FakeSession(_reply(SLACK_AUTH_PAYLOAD, is_error=True)))
        with pytest.raises(McpAuthError):
            asyncio.run(client.call_json("list_my_channels"))

    def test_auth_error_in_a_successful_reply_still_raises(self):
        client = _client(FakeSession(_reply(SLACK_AUTH_PAYLOAD)))  # isError not set
        with pytest.raises(McpAuthError, match="AUTH_ERROR"):
            asyncio.run(client.call_json("list_my_channels"))

    def test_reconnect_can_be_disabled(self):
        client = _client(FakeSession(closed()), FakeSession(_reply('{"ok": 1}')), max_reconnects=0)
        with pytest.raises(McpTransportError, match="giving up"):
            asyncio.run(client.call_json("get_emails"))
        assert client.reconnects_used == 0

    def test_backoff_is_awaited_before_the_retry(self, monkeypatch):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(mc.asyncio, "sleep", fake_sleep)
        client = McpClient("/nonexistent", [], reconnect_backoff=2.5)
        queue = [FakeSession(closed()), FakeSession(_reply('{"ok": 1}'))]

        async def _connect():
            client._session = queue.pop(0)

        client._connect = _connect
        asyncio.run(_connect())
        assert asyncio.run(client.call_json("x")) == {"ok": 1}
        assert slept == [2.5]


# --------------------------------------------------------------------------- #
# McpClient.call_text: raw markdown replies (midway-playwright)                #
# --------------------------------------------------------------------------- #
PLAYWRIGHT_EVAL = '### Result\n"{\\"x\\":\\"hello\\"}"\n### Ran Playwright code\n```js\nawait page.evaluate(...)\n```'


class TestCallText:
    def test_returns_concatenated_text_unparsed(self):
        reply = SimpleNamespace(content=[SimpleNamespace(text="### Page\n"), SimpleNamespace(type="image"),
                                         SimpleNamespace(text="- Page URL: https://x/")], isError=False)
        client = _client(FakeSession(reply))
        assert asyncio.run(client.call_text("browser_navigate", {"url": "https://x/"})) == \
            "### Page\n- Page URL: https://x/"

    def test_markdown_that_call_json_rejects_is_fine_for_call_text(self):
        client = _client(FakeSession(_reply(PLAYWRIGHT_EVAL), _reply(PLAYWRIGHT_EVAL)))
        assert asyncio.run(client.call_text("browser_evaluate")) == PLAYWRIGHT_EVAL
        with pytest.raises(McpToolError, match="no JSON"):
            asyncio.run(client.call_json("browser_evaluate"))

    def test_page_text_mentioning_auth_words_is_not_an_auth_error(self):
        # A successful reply is page content; only isError replies are judged.
        client = _client(FakeSession(_reply('{"error": "AUTH_ERROR", "message": "unauthorized"}')))
        assert "AUTH_ERROR" in asyncio.run(client.call_text("browser_evaluate"))

    def test_tool_error_is_a_tool_error(self):
        client = _client(FakeSession(_reply("### Error\nExecution context was destroyed", is_error=True)))
        with pytest.raises(McpToolError, match="Execution context was destroyed"):
            asyncio.run(client.call_text("browser_evaluate"))
        assert client.reconnects_used == 0

    def test_auth_tool_error_is_auth(self):
        client = _client(FakeSession(_reply("Missing ~/.midway/cookie — run mwinit first", is_error=True)))
        with pytest.raises(McpAuthError):
            asyncio.run(client.call_text("browser_navigate"))

    def test_reconnects_once_like_call_json(self):
        dead, fresh = FakeSession(closed()), FakeSession(_reply("### Page\nok"))
        client = _client(dead, fresh)
        assert asyncio.run(client.call_text("browser_navigate")) == "### Page\nok"
        assert client.reconnects_used == 1 and len(client.connects) == 2
        client2 = _client(FakeSession(closed()), FakeSession(closed()))
        with pytest.raises(McpTransportError):
            asyncio.run(client2.call_text("browser_navigate"))

    def test_timeout_is_classified_like_call_json(self):
        class Hung(FakeSession):
            async def call_tool(self, tool, params):
                await asyncio.sleep(5)

        client = _client(Hung(), call_timeout=0.01)
        with pytest.raises(McpToolError, match="timed out after") as info:
            asyncio.run(client.call_text("browser_evaluate"))
        assert classify_error(info.value) == "transport" and client.reconnects_used == 0

    def test_env_and_cwd_reach_the_server_parameters(self, tmp_path):
        client = McpClient("/bin/true", ["--caps", "core"], env={"PATH": "/usr/bin"}, cwd=str(tmp_path))
        assert client._params.env == {"PATH": "/usr/bin"} and str(client._params.cwd) == str(tmp_path)
        assert McpClient("/bin/true")._params.env is None
