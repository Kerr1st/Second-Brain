"""Hermetic tests for the Slack pre-flight auth probe (no MCP, no DB, no network).

On 2026-09-20 and 09-21 `capture-slack` exited 1 with a stack trace because the
Midway cookie was stale; two days of Slack were lost and nothing flagged it. The
probe turns that into: one cheap `list_my_channels` call, exit 3, one actionable
line, one notification — and no capture attempt at all.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from src.capture import slack
from src.capture.mcp_client import (
    EXIT_AUTH, EXIT_ERROR, EXIT_OK, EXIT_TRANSPORT, CaptureFailure, McpAuthError, McpToolError,
)

AUTH_BLOB = ('{"error":"AUTH_ERROR","message":"Failed to refresh authentication token: '
             'Request to IDP URL https://midway-auth.amazon.com/... failed"}')
HEALTHY = {"sections": [{"channels": [{"id": "C1", "name": "eng"}]}], "totalChannels": 1}


def _probe_call(outcome, log=None):
    """Fake MCP `call` that answers the probe tool with `outcome` (reply or raise)."""
    async def call(tool, params):
        if log is not None:
            log.append(tool)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    return call


class TestPreflight:
    def test_healthy_probe_is_ok(self):
        log = []
        assert asyncio.run(slack.preflight(_probe_call(HEALTHY, log))) == ("ok", "")
        assert log == [slack.PROBE_TOOL]  # exactly one probe call, and it is the cheap one

    def test_stale_midway_cookie_raised_as_a_tool_error_is_auth(self):
        kind, detail = asyncio.run(slack.preflight(
            _probe_call(McpAuthError(f"list_my_channels: {AUTH_BLOB}"))))
        assert kind == "auth" and "AUTH_ERROR" in detail

    def test_auth_error_returned_as_a_successful_payload_is_auth(self):
        import json
        kind, detail = asyncio.run(slack.preflight(_probe_call(json.loads(AUTH_BLOB))))
        assert kind == "auth" and "AUTH_ERROR" in detail

    def test_dead_server_is_transport_not_auth(self):
        kind, detail = asyncio.run(slack.preflight(
            _probe_call(McpError(ErrorData(code=0, message="Connection closed")))))
        assert kind == "transport" and "Connection closed" in detail

    def test_hung_server_is_transport(self, monkeypatch):
        monkeypatch.setattr(slack, "PROBE_TIMEOUT", 0.01)

        async def call(tool, params):
            await asyncio.sleep(5)

        kind, detail = asyncio.run(slack.preflight(call))
        assert kind == "transport" and "hung" in detail

    def test_unexpected_reply_shape_is_a_plain_error(self):
        assert asyncio.run(slack.preflight(_probe_call({"nope": 1})))[0] == "error"
        assert asyncio.run(slack.preflight(_probe_call([])))[0] == "error"
        assert asyncio.run(slack.preflight(_probe_call(McpToolError("rate limited"))))[0] == "error"

    def test_preflight_never_raises(self):
        assert asyncio.run(slack.preflight(_probe_call(RuntimeError("weird"))))[0] == "error"


# --------------------------------------------------------------------------- #
# The job's exit-code taxonomy, end to end through main()                      #
# --------------------------------------------------------------------------- #
def _run_main(monkeypatch, failure=None, stats=None, argv=("prog",)):
    """Drive slack.main() with capture() stubbed, returning (exit_code, notifications)."""
    sent = []
    monkeypatch.setattr("src.capture.mcp_client.notify", lambda m, t=None: sent.append((m, t)) or True)
    monkeypatch.setattr("sys.argv", list(argv))

    def fake_capture(**kwargs):
        if failure:
            raise failure
        return stats or {"processed": 0}

    monkeypatch.setattr(slack, "capture", fake_capture)
    return slack.main(), sent


class TestMainExitCodes:
    def test_auth_failure_exits_3_and_notifies_once(self, monkeypatch, caplog):
        failure = CaptureFailure("auth", f"list_my_channels: {AUTH_BLOB}", fix=slack.AUTH_FIX)
        with caplog.at_level("ERROR"):
            code, sent = _run_main(monkeypatch, failure=failure)
        assert code == EXIT_AUTH
        assert len(sent) == 1 and "mwinit" in sent[0][0]
        line = caplog.text.strip().splitlines()[-1]
        assert "FAILED (auth) slack" in line and "mwinit" in line

    def test_transport_failure_exits_4_without_notifying(self, monkeypatch):
        code, sent = _run_main(monkeypatch, failure=CaptureFailure("transport", "Connection closed"))
        assert code == EXIT_TRANSPORT and sent == []

    def test_unexpected_failure_exits_1(self, monkeypatch):
        code, _ = _run_main(monkeypatch, failure=CaptureFailure("error", "bad row"))
        assert code == EXIT_ERROR

    def test_successful_run_exits_0(self, monkeypatch):
        code, sent = _run_main(monkeypatch, stats={"processed": 3})
        assert code == EXIT_OK and sent == []


# --------------------------------------------------------------------------- #
# The probe gates the capture: on auth, nothing else is attempted              #
# --------------------------------------------------------------------------- #
class FakeClient:
    """Minimal stand-in for McpClient: scripted probe, records every tool call."""

    def __init__(self, probe_outcome):
        self.probe_outcome = probe_outcome
        self.tools: list[str] = []
        self.reconnects_used = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call_json(self, tool, params=None):
        self.tools.append(tool)
        if tool == slack.PROBE_TOOL:
            if isinstance(self.probe_outcome, BaseException):
                raise self.probe_outcome
            return self.probe_outcome
        raise AssertionError(f"capture must not run after a failed probe (called {tool})")


def _patch_run(monkeypatch, client):
    monkeypatch.setattr(slack, "McpClient", lambda *a, **k: client)
    monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: [])
    monkeypatch.setattr("src.ingest.ingest_batch",
                        lambda *a, **k: pytest.fail("must not ingest on a failed probe"))


class TestProbeGatesTheCapture:
    def test_auth_failure_skips_the_capture_entirely(self, monkeypatch):
        client = FakeClient(McpAuthError(f"list_my_channels: {AUTH_BLOB}"))
        _patch_run(monkeypatch, client)
        with pytest.raises(CaptureFailure) as exc:
            slack.capture(days_back=1)
        assert exc.value.exit_code == EXIT_AUTH
        assert exc.value.fix and "mwinit" in exc.value.fix
        assert client.tools == [slack.PROBE_TOOL]  # probe only; no list_dms, no get_messages

    def test_transport_failure_skips_the_capture_and_offers_no_reauth(self, monkeypatch):
        client = FakeClient(McpError(ErrorData(code=0, message="Connection closed")))
        _patch_run(monkeypatch, client)
        with pytest.raises(CaptureFailure) as exc:
            slack.capture(days_back=1)
        assert exc.value.exit_code == EXIT_TRANSPORT and exc.value.needs_reauth is False
        assert client.tools == [slack.PROBE_TOOL]

    def test_a_mid_run_failure_is_classified_too(self, monkeypatch):
        """The probe passes, then the server dies during collection."""
        class Dying(FakeClient):
            async def call_json(self, tool, params=None):
                self.tools.append(tool)
                if tool == slack.PROBE_TOOL:
                    return HEALTHY
                raise McpError(ErrorData(code=0, message="Connection closed"))

        client = Dying(HEALTHY)
        _patch_run(monkeypatch, client)
        with pytest.raises(CaptureFailure) as exc:
            slack.capture(days_back=1)
        assert exc.value.exit_code == EXIT_TRANSPORT
        assert client.tools[0] == slack.PROBE_TOOL and len(client.tools) > 1

    def test_healthy_probe_lets_the_capture_run(self, monkeypatch):
        calls = []

        class Working(FakeClient):
            async def call_json(self, tool, params=None):
                calls.append(tool)
                return HEALTHY if tool == slack.PROBE_TOOL else {"dms": []}

        monkeypatch.setattr(slack, "McpClient", lambda *a, **k: Working(HEALTHY))
        monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: [])
        stats = slack.capture(days_back=1, dry_run=True)
        assert stats == {"processed": 0, "skipped": 0, "failed": 0, "planned": 0, "dry_run": True}
        assert calls[0] == slack.PROBE_TOOL and "list_dms" in calls


class TestTimeouts:
    def test_call_timeout_is_bounded_and_generous(self):
        # Generous enough for a 200-message channel-day page, never unbounded.
        assert 60 <= slack.SLACK_CALL_TIMEOUT <= 600
        assert 5 <= slack.PROBE_TIMEOUT <= slack.SLACK_CALL_TIMEOUT
