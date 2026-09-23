"""Hermetic tests for Outlook transport resilience (no MCP, no DB, no network).

On 2026-09-20/21 grasp-mcp exited mid-session (`McpError: Connection closed`)
and `capture-outlook` died with exit 1, losing 57–81 h of mail and calendar.
The capture must now survive one server death, and must exit 4 (not 1, and not
the auth code 3) when the server stays dead.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from src.capture import outlook
from src.capture.mcp_client import (
    EXIT_AUTH, EXIT_ERROR, EXIT_OK, EXIT_TRANSPORT, CaptureFailure, McpAuthError, McpClient,
    McpToolError, McpTransportError,
)

GRAPH_AUTH = "get_emails: 401 unauthorized — token expired"


def closed() -> McpError:
    return McpError(ErrorData(code=0, message="Connection closed"))


def _reply(text: str, is_error: bool = False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=is_error)


class FakeSession:
    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool, params):
        self.calls.append((tool, params))
        item = self.script.pop(0) if self.script else _reply('{"emails": []}')
        if isinstance(item, BaseException):
            raise item
        return item


def _client(*sessions, **kw):
    """A connected McpClient whose (re)connect hands out the next fake session."""
    client = McpClient(outlook.GRASP_CMD, outlook.GRASP_ARGS, reconnect_backoff=0, **kw)
    queue, connects = list(sessions), []

    async def _connect():
        if not queue:
            raise closed()
        client._session = queue.pop(0)
        connects.append(client._session)

    client._connect = _connect
    asyncio.run(_connect())
    client.connects = connects
    return client


MAIL_PAGE = '{"emails": [{"id": "m1", "subject": "Hi", "from": {"emailAddress": "a@x.com"}, ' \
            '"toRecipients": [], "receivedDateTime": "2026-09-20T09:00:00Z"}], "hasMore": false}'
MAIL_BODY = '{"bodyType": "text", "bodyContent": "synthetic body"}'
EMPTY_PAGE = '{"emails": [], "hasMore": false}'

START = outlook.datetime(2026, 9, 20, tzinfo=outlook.timezone.utc)
END = outlook.datetime(2026, 9, 22, tzinfo=outlook.timezone.utc)


class TestCollectMailSurvivesOneServerDeath:
    def test_reconnects_once_mid_collection_and_completes(self):
        # The server dies on the inbox listing; after one reconnect the whole
        # collection finishes and the email is still captured.
        dead = FakeSession(closed())
        fresh = FakeSession(_reply(MAIL_PAGE), _reply(MAIL_BODY), _reply(EMPTY_PAGE))
        client = _client(dead, fresh)
        items = asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))
        assert len(items) == 1 and items[0][0].startswith("outlook://mail/2026-09-20T09:00:00/")
        assert "synthetic body" in items[0][1]
        assert client.reconnects_used == 1                     # exactly one reconnect
        assert len(client.connects) == 2                       # initial + one
        assert len(dead.calls) == 1                             # the dead call is not re-tried twice
        assert [t for t, _ in fresh.calls] == ["get_emails", "get_email_details", "get_emails"]

    def test_death_during_get_email_details_is_survived_too(self):
        dead = FakeSession(_reply(MAIL_PAGE), closed())
        fresh = FakeSession(_reply(MAIL_BODY), _reply(EMPTY_PAGE))
        client = _client(dead, fresh)
        items = asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))
        assert len(items) == 1 and client.reconnects_used == 1

    def test_a_second_death_is_not_retried_and_surfaces_as_transport(self):
        first = FakeSession(closed())
        second = FakeSession(_reply(MAIL_PAGE), _reply(MAIL_BODY), closed())
        client = _client(first, second)
        with pytest.raises(McpTransportError, match="giving up"):
            asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))
        assert client.reconnects_used == 1  # bounded for the whole session

    def test_persistent_death_raises_transport_not_a_swallowable_tool_error(self):
        client = _client(FakeSession(closed()), FakeSession(closed()))
        with pytest.raises(McpTransportError):
            asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))

    def test_a_dead_server_during_details_is_never_silently_skipped(self):
        # collect_mail swallows McpToolError per message ("skip this email");
        # a closed connection must not take that path or the run would exit 0
        # having captured nothing — the silent-loss failure mode.
        client = _client(FakeSession(_reply(MAIL_PAGE), closed()))  # no replacement session
        with pytest.raises(McpTransportError):
            asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))

    def test_ordinary_per_message_errors_are_still_skipped(self):
        session = FakeSession(_reply(MAIL_PAGE), _reply("mailbox item not found", is_error=True),
                              _reply(EMPTY_PAGE))
        client = _client(session)
        items = asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))
        assert items == [] and client.reconnects_used == 0

    def test_expired_graph_token_mid_run_aborts_instead_of_skipping_every_message(self):
        session = FakeSession(_reply(MAIL_PAGE), _reply("401 unauthorized: token expired", is_error=True))
        client = _client(session)
        with pytest.raises(McpAuthError):
            asyncio.run(outlook.collect_mail(client.call_json, START, END, set()))
        assert client.reconnects_used == 0


# --------------------------------------------------------------------------- #
# The job's exit-code taxonomy                                                 #
# --------------------------------------------------------------------------- #
class FailingClient:
    def __init__(self, exc):
        self.exc = exc
        self.reconnects_used = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call_json(self, tool, params=None):
        raise self.exc


def _patch(monkeypatch, client):
    monkeypatch.setattr(outlook, "McpClient", lambda *a, **k: client)
    monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: [])
    monkeypatch.setattr("src.ingest.ingest_batch",
                        lambda *a, **k: pytest.fail("must not ingest after a fatal failure"))


class TestCaptureClassifiesFailures:
    def test_persistent_transport_failure_is_transport(self, monkeypatch):
        _patch(monkeypatch, FailingClient(McpTransportError("get_emails: reconnect failed")))
        with pytest.raises(CaptureFailure) as exc:
            outlook.capture(days_back=1)
        assert exc.value.exit_code == EXIT_TRANSPORT
        assert exc.value.needs_reauth is False and exc.value.fix is None

    def test_expired_graph_token_is_auth_with_a_reauth_hint(self, monkeypatch):
        _patch(monkeypatch, FailingClient(McpAuthError(GRAPH_AUTH)))
        with pytest.raises(CaptureFailure) as exc:
            outlook.capture(days_back=1)
        assert exc.value.exit_code == EXIT_AUTH
        assert exc.value.needs_reauth and "Graph" in (exc.value.fix or "")

    def test_a_bug_is_neither(self, monkeypatch):
        _patch(monkeypatch, FailingClient(ValueError("bad row")))
        with pytest.raises(CaptureFailure) as exc:
            outlook.capture(days_back=1)
        assert exc.value.exit_code == EXIT_ERROR

    def test_a_server_that_never_starts_is_transport(self, monkeypatch):
        class DeadOnConnect:
            async def __aenter__(self):
                raise closed()

            async def __aexit__(self, *a):
                return None

        _patch(monkeypatch, DeadOnConnect())
        with pytest.raises(CaptureFailure) as exc:
            outlook.capture(days_back=1)
        assert exc.value.exit_code == EXIT_TRANSPORT


def _run_main(monkeypatch, failure=None, summary=None):
    sent = []
    monkeypatch.setattr("src.capture.mcp_client.notify", lambda m, t=None: sent.append((m, t)) or True)
    monkeypatch.setattr("sys.argv", ["prog"])

    def fake_capture(**kwargs):
        if failure:
            raise failure
        return summary or {outlook.SOURCE_TYPE_MAIL: {"processed": 1}}

    monkeypatch.setattr(outlook, "capture", fake_capture)
    return outlook.main(), sent


class TestMainExitCodes:
    def test_transport_exits_4_and_does_not_nag_for_credentials(self, monkeypatch, caplog):
        with caplog.at_level("ERROR"):
            code, sent = _run_main(monkeypatch, failure=CaptureFailure("transport", "Connection closed"))
        assert code == EXIT_TRANSPORT and sent == []
        assert "FAILED (transport) outlook" in caplog.text

    def test_auth_exits_3_and_notifies_once(self, monkeypatch, caplog):
        failure = CaptureFailure("auth", GRAPH_AUTH, fix=outlook.AUTH_FIX)
        with caplog.at_level("ERROR"):
            code, sent = _run_main(monkeypatch, failure=failure)
        assert code == EXIT_AUTH and len(sent) == 1
        line = caplog.text.strip().splitlines()[-1]
        assert "FAILED (auth) outlook" in line and "Graph" in line and "\n" not in line

    def test_success_exits_0(self, monkeypatch):
        code, sent = _run_main(monkeypatch)
        assert code == EXIT_OK and sent == []

    def test_unexpected_error_exits_1(self, monkeypatch):
        code, _ = _run_main(monkeypatch, failure=CaptureFailure("error", "boom"))
        assert code == EXIT_ERROR


class TestCallTimeout:
    def test_capture_timeout_is_generous_but_bounded(self):
        # 60s (the credentials probe's value) is too tight for a 50-message page;
        # unbounded would let a hung grasp-mcp hold the job past the next run.
        assert outlook.OUTLOOK_CALL_TIMEOUT > 60
        assert outlook.OUTLOOK_CALL_TIMEOUT <= 600

    def test_the_client_is_built_with_that_timeout_and_a_reconnect_budget(self, monkeypatch):
        seen = {}

        class Recording(FailingClient):
            def __init__(self, *a, **kw):
                seen.update(kw)
                super().__init__(McpTransportError("stop here"))

        monkeypatch.setattr(outlook, "McpClient", Recording)
        monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: [])
        with pytest.raises(CaptureFailure):
            outlook.capture(days_back=1)
        assert seen.get("call_timeout") == outlook.OUTLOOK_CALL_TIMEOUT
        # The default budget is one reconnect per session — bounded, not a loop.
        assert McpClient(outlook.GRASP_CMD)._max_reconnects == 1
