"""Mocked-subprocess unit tests for ClaudeCodeInvoker — tasks 4.1–4.4 scope.

Covers command construction (public flag surface), Invoker conformance,
system-prompt delivery (native file/append + the prepend fallback), and
``.result`` extraction (task 4.1); MCP attach / tool-less enforcement /
fail-loud probe (task 4.2); and real usage capture, the tolerate-and-warn
fallback, plus every failure-mode row (task 4.3).

Task 4.4 is the dedicated mocked-subprocess coverage task: every test here
mocks ``src.backends.claude_code.subprocess.run`` so no live CLI is ever
required (Req 22.6), and the full 4.4 checklist — command construction (public
flags), tools=True vs tools=False shapes, native + fallback system-prompt
delivery, MCP-config emission with ``cwd``=repo + ``--strict-mcp-config``,
``.result`` extraction, usage capture incl. tolerate-and-warn, every
failure-mode row, and probe behavior (performed on tools=True, skipped on
tools=False, not cached) — is asserted across the classes below. The
``TestMockedSubprocessCoverage`` class closes the gaps the 4.1–4.3 tests left
implicit: the subprocess boundary is the only process spawned (Req 22.6), the
MCP server ``cwd`` is the actual repo root, and each ``invoke()`` spawns its own
fresh subprocess (so the probe holds no cross-call state).

Fixtures use the ``--output-format stream-json --verbose`` JSONL event stream
(one JSON object per line): a ``system`` init event, assistant/user message
events carrying ``message.content`` blocks (``text`` / ``tool_use`` /
``tool_result``), and one terminal ``{"type":"result"}`` event carrying
``.result`` / ``is_error`` / ``usage``. The adapter reads ``.result`` / usage /
``is_error`` from the terminal result event and feeds the stream events to the
probe (the ``tool_result`` blocks live in the user/assistant events, not the
terminal result event) — see the spec
``claude-code-stream-json-probe-fix`` (task 3.6).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.5, 2.6, 2.7,
2.8, 3.1, 3.2, 3.3, 3.6, 4.1, 4.2, 4.3, 5.2, 5.3, 5.5, 6.1, 6.2, 6.3, 7.1, 7.2,
7.3, 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 9.3, 21.1, 22.1, 22.6
"""

from __future__ import annotations

import json
import os

import pytest
from unittest.mock import MagicMock, patch

from src.backends.base import Invoker
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.mcp_probe import MCPStartupProbe


@pytest.fixture(autouse=True)
def _isolate_llm_metrics(tmp_path, monkeypatch):
    """Redirect per-call metrics writes to a temp dir so invoke tests don't
    pollute the real logs/llm_metrics/ directory."""
    monkeypatch.setattr(
        "src.backends.agentic_cli.METRICS_DIR", str(tmp_path / "llm_metrics")
    )


def _ok(stdout: str):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _last_cmd(mock_run) -> list:
    """The argv passed to the most recent subprocess.run call."""
    return mock_run.call_args[0][0]


# --- stream-json JSONL fixture helpers (design "Unit Tests" section) --------
# ``--output-format stream-json --verbose`` emits one JSON object per line: a
# ``system`` init event, assistant/user message events with ``message.content``
# blocks, and one terminal ``{"type":"result"}`` event carrying ``.result`` /
# ``is_error`` / ``usage``.
def _stream(events: list) -> str:
    """Join events into a JSONL stream — one JSON object per line."""
    return "\n".join(json.dumps(e) for e in events)


def _system_event() -> dict:
    return {"type": "system", "subtype": "init", "session_id": "s1"}


def _assistant_text_event(text: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def _assistant_tool_use_event(name: str = "memory_search") -> dict:
    return {"type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": name,
                 "input": {"query": "__mcp_startup_probe__", "limit": 1}}]}}


def _user_tool_result_event(is_error: bool = False) -> dict:
    """A user message event carrying a completed ``tool_result`` content block —
    the signal the MCP_Startup_Probe confirms (process-attach alone never
    suffices — Req 5.4). The block lives in the stream events, NOT the terminal
    result event."""
    return {"type": "user",
            "message": {"content": [
                {"type": "tool_result", "is_error": is_error, "content": "[]"}]}}


def _result_event(result_text: str, *, is_error: bool = False,
                  usage: dict | None = None, total_cost_usd=None) -> dict:
    """The terminal ``{"type":"result"}`` event — source of ``.result`` /
    ``is_error`` / ``usage`` for the adapter."""
    env = {"type": "result", "subtype": "success", "is_error": is_error,
           "result": result_text}
    if usage is not None:
        env["usage"] = usage
    if total_cost_usd is not None:
        env["total_cost_usd"] = total_cost_usd
    return env


def _stream_ok(result_text: str = '{"ok": 1}') -> str:
    """system + assistant(text) + terminal result (no tool result)."""
    return _stream([
        _system_event(),
        _assistant_text_event("here is the answer"),
        _result_event(result_text),
    ])


def _stream_with_tool_result(result_text: str = '{"ok": 1}', *,
                             is_error: bool = False) -> str:
    """system + assistant(tool_use) + user(tool_result) + terminal result.

    Models a tool-using turn where ``memory_search`` ran and returned, so the
    probe confirms via the stream events; the terminal result event carries the
    final ``.result``.
    """
    return _stream([
        _system_event(),
        _assistant_tool_use_event(),
        _user_tool_result_event(is_error=False),
        _result_event(result_text, is_error=is_error),
    ])


def _stream_with_usage(result_text: str = '{"ok": 1}', *, usage: dict,
                       total_cost_usd=None) -> str:
    """system + assistant(text) + terminal result carrying ``usage`` (Req 7.1)."""
    return _stream([
        _system_event(),
        _assistant_text_event("here is the answer"),
        _result_event(result_text, usage=usage, total_cost_usd=total_cost_usd),
    ])


class TestConstruction:
    def test_accepts_model(self):
        inv = ClaudeCodeInvoker(model="claude-sonnet-4")
        assert inv.model == "claude-sonnet-4"

    def test_blank_model_rejected(self):
        # Metered backend: a blank model id must not silently bill a default.
        with pytest.raises(ValueError):
            ClaudeCodeInvoker(model="")
        with pytest.raises(ValueError):
            ClaudeCodeInvoker()

    def test_invalid_delivery_rejected(self):
        with pytest.raises(ValueError):
            ClaudeCodeInvoker(model="m", system_prompt_delivery="bogus")

    def test_satisfies_invoker_protocol(self):
        assert isinstance(ClaudeCodeInvoker(model="m"), Invoker)


class TestCommandConstruction:
    """Req 2: public flag surface — claude -p ... --model ...
    --output-format stream-json --verbose."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_core_flags(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="claude-opus-4").invoke("sys", "hello")
        cmd = _last_cmd(mock_run)

        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        # user message is the -p positional input, distinct from system prompt (Req 3.2)
        assert cmd[2] == "hello"
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-opus-4"
        # stream-json (with --verbose) exposes the tool-use/tool-result transcript
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd

    @patch("src.backends.claude_code.subprocess.run")
    def test_effort_passed_through(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "hi", effort="high")
        cmd = _last_cmd(mock_run)
        assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"

    @patch("src.backends.claude_code.subprocess.run")
    def test_effort_omitted_when_absent(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "hi")
        assert "--effort" not in _last_cmd(mock_run)

    @patch("src.backends.claude_code.subprocess.run")
    def test_json_schema_flag_when_provided(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "hi", json_schema="/tmp/schema.json")
        cmd = _last_cmd(mock_run)
        assert "--json-schema" in cmd
        assert cmd[cmd.index("--json-schema") + 1] == "/tmp/schema.json"

    @patch("src.backends.claude_code.subprocess.run")
    def test_json_schema_omitted_by_default(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "hi")
        assert "--json-schema" not in _last_cmd(mock_run)

    @patch("src.backends.claude_code.subprocess.run")
    def test_no_asbx_wrapper_flags(self, mock_run):
        """Req 2.8: never depend on the enterprise-managed provider wrapper surface."""
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "hi", effort="high")
        cmd = _last_cmd(mock_run)
        for forbidden in ("--aws-profile", "--claude-help"):
            assert forbidden not in cmd

    @patch("src.backends.claude_code.subprocess.run")
    def test_configurable_binary_path(self, mock_run, monkeypatch):
        """Req 2.7: CLAUDE_CLI overrides the binary; mirrors KIRO_CLI."""
        monkeypatch.setattr("src.backends.claude_code.CLAUDE_CLI", "/opt/asbx/claude")
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "hi")
        assert _last_cmd(mock_run)[0] == "/opt/asbx/claude"


class TestSystemPromptDelivery:
    """Req 3: native delivery (file preferred / append) + prepend fallback."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_file_delivery_default(self, mock_run):
        captured = {}

        def _capture(cmd, **kwargs):
            # The temp file must exist and contain the system prompt at call time.
            i = cmd.index("--system-prompt-file")
            path = cmd[i + 1]
            with open(path, encoding="utf-8") as f:
                captured["contents"] = f.read()
            captured["cmd"] = cmd
            return _ok(_stream_ok('{"ok": 1}'))

        mock_run.side_effect = _capture
        ClaudeCodeInvoker(model="m").invoke("SYSTEM ROLE", "user data")
        assert "--system-prompt-file" in captured["cmd"]
        assert captured["contents"] == "SYSTEM ROLE"
        # user message stays distinct from the system prompt (Req 3.2)
        assert captured["cmd"][2] == "user data"
        assert "--append-system-prompt" not in captured["cmd"]

    @patch("src.backends.claude_code.subprocess.run")
    def test_file_cleaned_up_after_invoke(self, mock_run):
        seen = {}

        def _capture(cmd, **kwargs):
            seen["path"] = cmd[cmd.index("--system-prompt-file") + 1]
            return _ok(_stream_ok('{"ok": 1}'))

        mock_run.side_effect = _capture
        ClaudeCodeInvoker(model="m").invoke("sys", "msg")
        import os
        assert not os.path.exists(seen["path"])

    @patch("src.backends.claude_code.subprocess.run")
    def test_append_delivery(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m", system_prompt_delivery="append").invoke(
            "SYS", "msg"
        )
        cmd = _last_cmd(mock_run)
        assert "--append-system-prompt" in cmd
        assert cmd[cmd.index("--append-system-prompt") + 1] == "SYS"
        assert "--system-prompt-file" not in cmd
        assert cmd[2] == "msg"

    @patch("src.backends.claude_code.subprocess.run")
    def test_prepend_fallback(self, mock_run):
        """Req 3.3: fallback folds the system prompt into the user message."""
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m", system_prompt_delivery="prepend").invoke(
            "SYS ROLE", "USER MSG"
        )
        cmd = _last_cmd(mock_run)
        assert "--system-prompt-file" not in cmd
        assert "--append-system-prompt" not in cmd
        assert cmd[2] == "SYS ROLE\n\nUSER MSG"


class TestResultExtraction:
    """Req 2.3 / Req 1.3-1.4: final text from the terminal result event's
    ``.result``, parsed payload in output."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_extracts_result_and_parses_payload(self, mock_run):
        payload = {"candidates": [{"title": "insight"}]}
        mock_run.return_value = _ok(_stream_ok(json.dumps(payload)))
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")

        assert set(result) == {"output", "raw", "usage", "usage_source"}
        assert result["output"] == payload
        # raw is the terminal result event's .result text, not the whole stream
        assert result["raw"] == json.dumps(payload)

    @patch("src.backends.claude_code.subprocess.run")
    def test_result_with_prose_wrapped_json(self, mock_run):
        payload = {"verdict": "ACCEPT"}
        result_text = f"Here is my answer:\n{json.dumps(payload)}\nDone."
        mock_run.return_value = _ok(_stream_ok(result_text))
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")
        assert result["output"] == payload
        assert result["raw"] == result_text

    @patch("src.backends.claude_code.subprocess.run")
    def test_default_usage_is_estimate(self, mock_run):
        # Real-usage capture is task 4.3; the 4.1 default is None/"estimate".
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")
        assert result["usage"] is None
        assert result["usage_source"] == "estimate"

    @patch("src.backends.claude_code.subprocess.run")
    def test_unparseable_result_raises_valueerror(self, mock_run):
        # The terminal result event's .result is prose with no recoverable JSON.
        mock_run.return_value = _ok(_stream_ok("just prose, no json here"))
        with pytest.raises(ValueError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.claude_code.subprocess.run")
    def test_no_result_event_raises_valueerror(self, mock_run):
        # A malformed/truncated stream with no terminal result event → the
        # adapter returns "" for the final text and the shared backstop raises
        # ValueError (it must NOT recover an interior event as a bogus answer).
        stdout = _stream([
            _system_event(),
            _assistant_text_event("interior answer that must NOT be recovered"),
        ])
        mock_run.return_value = _ok(stdout)
        with pytest.raises(ValueError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg")


class TestToolLessEnforcement:
    """Req 6: tools=False disables tools the documented way and runs no probe."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_false_strict_mcp_without_mcp_config(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)
        cmd = _last_cmd(mock_run)
        # --strict-mcp-config present, but no MCP server config loaded at all.
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" not in cmd

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_false_disables_builtin_tools(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)
        cmd = _last_cmd(mock_run)
        # --tools "" turns built-in tools off (insufficient ALONE for MCP, but
        # paired with strict-mcp-config-without-mcp-config it guarantees no tools).
        assert "--tools" in cmd
        assert cmd[cmd.index("--tools") + 1] == ""

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_false_does_not_prepend_probe_instruction(self, mock_run):
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "user data", tools=False)
        # The -p positional stays the bare user message; no probe instruction.
        assert _last_cmd(mock_run)[2] == "user data"

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_false_skips_probe_even_without_tool_result(self, mock_run):
        # No tool-result in the stream, yet tools=False must NOT raise.
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)
        assert result["output"] == {"ok": 1}


class TestMcpAttach:
    """Req 4: tools=True emits --mcp-config + --strict-mcp-config with cwd=repo."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_true_emits_mcp_config_and_strict(self, mock_run):
        mock_run.return_value = _ok(_stream_with_tool_result())
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)
        cmd = _last_cmd(mock_run)
        assert "--mcp-config" in cmd
        assert "--strict-mcp-config" in cmd
        # tool-less flag must NOT appear on a tool-using call
        assert "--tools" not in cmd

    @patch("src.backends.claude_code.subprocess.run")
    def test_mcp_config_file_contents(self, mock_run):
        import sys as _sys
        from src.backends.kiro import _SECOND_BRAIN_MCP as _KIRO_MCP

        captured = {}

        def _capture(cmd, **kwargs):
            path = cmd[cmd.index("--mcp-config") + 1]
            with open(path, encoding="utf-8") as f:
                captured["config"] = json.load(f)
            return _ok(_stream_with_tool_result())

        mock_run.side_effect = _capture
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)

        servers = captured["config"]["mcpServers"]
        assert "second-brain" in servers
        entry = servers["second-brain"]
        # python -m src.mcp_server launched in the current venv (Req 4.1)
        assert entry["command"] == _sys.executable
        assert entry["args"] == ["-m", "src.mcp_server"]
        # cwd mirrors KiroInvoker's _SECOND_BRAIN_MCP exactly (Req 4.2) so both
        # agentic-CLI adapters launch the server identically.
        assert entry["cwd"] == _KIRO_MCP["second-brain"]["cwd"]

    @patch("src.backends.claude_code.subprocess.run")
    def test_mcp_config_temp_file_cleaned_up(self, mock_run):
        seen = {}

        def _capture(cmd, **kwargs):
            seen["path"] = cmd[cmd.index("--mcp-config") + 1]
            return _ok(_stream_with_tool_result())

        mock_run.side_effect = _capture
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)
        assert not os.path.exists(seen["path"])

    @patch("src.backends.claude_code.subprocess.run")
    def test_mcp_config_cleaned_up_with_prepend_delivery(self, mock_run):
        # No system-prompt temp file in prepend mode, so the base wouldn't clean
        # anything — the adapter must still remove the MCP config file.
        seen = {}

        def _capture(cmd, **kwargs):
            seen["path"] = cmd[cmd.index("--mcp-config") + 1]
            return _ok(_stream_with_tool_result())

        mock_run.side_effect = _capture
        ClaudeCodeInvoker(model="m", system_prompt_delivery="prepend").invoke(
            "sys", "msg", tools=True
        )
        assert not os.path.exists(seen["path"])

    @patch("src.backends.claude_code.subprocess.run")
    def test_probe_instruction_prepended_on_tools_true(self, mock_run):
        mock_run.return_value = _ok(_stream_with_tool_result())
        ClaudeCodeInvoker(model="m").invoke("sys", "the real task", tools=True)
        prompt_input = _last_cmd(mock_run)[2]
        assert MCPStartupProbe.PROBE_NAME in prompt_input
        assert "the real task" in prompt_input


class TestFailLoud:
    """Req 5 / 8.3: probe failure and is_error result events raise, never a verdict."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_true_no_tool_result_raises(self, mock_run):
        # MCP server may have "attached" but no tool result came back — fail loud.
        # The stream has no tool_result block in any event.
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        with pytest.raises(RuntimeError) as exc:
            ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)
        assert MCPStartupProbe.PROBE_NAME in str(exc.value)

    @patch("src.backends.claude_code.subprocess.run")
    def test_tools_true_with_tool_result_succeeds(self, mock_run):
        mock_run.return_value = _ok(_stream_with_tool_result('{"candidates": []}'))
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)
        assert result["output"] == {"candidates": []}

    @patch("src.backends.claude_code.subprocess.run")
    def test_is_error_envelope_raises_runtimeerror(self, mock_run):
        # Terminal result event carries is_error + a parseable .result so
        # is_error (RuntimeError) wins over the ValueError backstop.
        mock_run.return_value = _ok(
            _stream_with_tool_result('{"ok": 1}', is_error=True)
        )
        with pytest.raises(RuntimeError) as exc:
            ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)
        assert "is_error" in str(exc.value)

    @patch("src.backends.claude_code.subprocess.run")
    def test_is_error_raises_even_when_tools_false(self, mock_run):
        # is_error is an infrastructure failure regardless of tool use (Req 8.3).
        stdout = _stream([
            _system_event(),
            _assistant_text_event("done"),
            _result_event('{"ok": 1}', is_error=True),
        ])
        mock_run.return_value = _ok(stdout)
        with pytest.raises(RuntimeError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)

    @patch("src.backends.claude_code.subprocess.run")
    def test_probe_not_cached_second_call_can_fail(self, mock_run):
        # First tool-using call confirms a tool result; the second does not and
        # must raise — the probe holds no "healthy" state across invocations.
        inv = ClaudeCodeInvoker(model="m")
        mock_run.return_value = _ok(_stream_with_tool_result())
        inv.invoke("sys", "msg", tools=True)

        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        with pytest.raises(RuntimeError):
            inv.invoke("sys", "msg", tools=True)


class TestUsageCapture:
    """Req 7: real usage capture + tolerate-and-warn fallback (task 4.3)."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_real_usage_from_envelope(self, mock_run):
        usage = {"input_tokens": 123, "output_tokens": 456,
                 "cache_read_input_tokens": 7}
        mock_run.return_value = _ok(
            _stream_with_usage('{"ok": 1}', usage=usage, total_cost_usd=0.0123)
        )
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")

        assert result["usage_source"] == "real"
        # usage carries the result event usage fields plus total_cost_usd (Req 7.1)
        assert result["usage"]["input_tokens"] == 123
        assert result["usage"]["output_tokens"] == 456
        assert result["usage"]["cache_read_input_tokens"] == 7
        assert result["usage"]["total_cost_usd"] == 0.0123
        # result still preserved
        assert result["output"] == {"ok": 1}

    @patch("src.backends.claude_code.subprocess.run")
    def test_real_usage_without_total_cost(self, mock_run):
        usage = {"input_tokens": 10, "output_tokens": 20}
        mock_run.return_value = _ok(_stream_with_usage(usage=usage))
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")
        assert result["usage_source"] == "real"
        assert result["usage"]["input_tokens"] == 10
        assert "total_cost_usd" not in result["usage"]

    @patch("src.backends.claude_code.subprocess.run")
    def test_real_usage_recorded_in_metrics(self, mock_run, tmp_path, monkeypatch):
        metrics_dir = tmp_path / "m"
        monkeypatch.setattr("src.backends.agentic_cli.METRICS_DIR", str(metrics_dir))
        usage = {"input_tokens": 11, "output_tokens": 22}
        mock_run.return_value = _ok(
            _stream_with_usage(usage=usage, total_cost_usd=0.5)
        )
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", run_id="run1")

        line = (metrics_dir / "run1.jsonl").read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        assert rec["usage_source"] == "real"
        assert rec["real_input_tokens"] == 11
        assert rec["real_output_tokens"] == 22
        assert rec["total_cost_usd"] == 0.5

    @patch("src.backends.claude_code.subprocess.run")
    def test_missing_usage_tolerate_and_warn(self, mock_run, caplog):
        # Parseable success with NO usage: keep result, usage=None/"estimate",
        # warn, and DO NOT raise (Req 7.2, 7.3).
        mock_run.return_value = _ok(_stream_ok('{"candidates": []}'))
        with caplog.at_level("WARNING"):
            result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")

        assert result["output"] == {"candidates": []}
        assert result["usage"] is None
        assert result["usage_source"] == "estimate"
        # a loud warning was emitted about the missing metered usage
        assert any(
            "usage" in r.message.lower() and r.levelname == "WARNING"
            for r in caplog.records
        )

    @patch("src.backends.claude_code.subprocess.run")
    def test_missing_usage_does_not_raise(self, mock_run):
        # Explicit: the tolerate path never routes into the failure path.
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        # Should simply return, not raise.
        result = ClaudeCodeInvoker(model="m").invoke("sys", "msg")
        assert result["usage"] is None


class TestFailureModeParity:
    """Req 8 / 9: every failure-mode row maps to the documented exception."""

    @patch("src.backends.claude_code.subprocess.run")
    def test_timeout_maps_to_timeouterror(self, mock_run):
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired(cmd="claude", timeout=1)
        with pytest.raises(TimeoutError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg", timeout=1)

    @patch("src.backends.claude_code.subprocess.run")
    def test_nonzero_exit_maps_to_runtimeerror(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="boom"
        )
        with pytest.raises(RuntimeError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.claude_code.subprocess.run")
    def test_is_error_envelope_maps_to_runtimeerror(self, mock_run):
        stdout = _stream([
            _system_event(),
            _assistant_text_event("done"),
            _result_event('{"ok": 1}', is_error=True),
        ])
        mock_run.return_value = _ok(stdout)
        with pytest.raises(RuntimeError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.claude_code.subprocess.run")
    def test_unrecoverable_json_maps_to_valueerror(self, mock_run):
        mock_run.return_value = _ok(_stream_ok("no json at all, just prose"))
        with pytest.raises(ValueError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.claude_code.subprocess.run")
    def test_probe_failure_maps_to_runtimeerror(self, mock_run):
        # tools=True with no tool result confirmed → RuntimeError (Req 5.2).
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        with pytest.raises(RuntimeError):
            ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)


class TestMockedSubprocessCoverage:
    """Task 4.4: dedicated mocked-subprocess coverage (Req 22.1, 22.6).

    Closes the gaps the 4.1–4.3 suites left implicit — the subprocess is the
    only process spawned (no live CLI — Req 22.6), the MCP server ``cwd`` is the
    actual repo root (not just "matches Kiro"), and each ``invoke()`` spawns its
    own fresh subprocess so the probe can hold no cross-call state.
    """

    @patch("src.backends.claude_code.subprocess.run")
    def test_no_live_cli_single_subprocess_spawn(self, mock_run):
        # Req 22.6: the adapter never shells out to a real CLI — the only process
        # boundary is the mocked subprocess.run, called exactly once per turn.
        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        ClaudeCodeInvoker(model="m").invoke("sys", "msg")
        assert mock_run.call_count == 1

    @patch("src.backends.claude_code.subprocess.run")
    def test_mcp_config_cwd_is_repo_root(self, mock_run):
        # Checklist "cwd=repo": assert the server cwd is the adapter's computed
        # repo root explicitly, not merely equal to Kiro's value.
        from src.backends.claude_code import _REPO_ROOT

        captured = {}

        def _capture(cmd, **kwargs):
            path = cmd[cmd.index("--mcp-config") + 1]
            with open(path, encoding="utf-8") as f:
                captured["config"] = json.load(f)
            return _ok(_stream_with_tool_result())

        mock_run.side_effect = _capture
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)

        entry = captured["config"]["mcpServers"]["second-brain"]
        assert entry["cwd"] == _REPO_ROOT
        assert os.path.isdir(entry["cwd"])

    @patch("src.backends.claude_code.subprocess.run")
    def test_probe_performed_on_tools_true_skipped_on_tools_false(self, mock_run):
        # Both shapes exercised through one cached invoker: tools=True runs the
        # probe (instruction prepended, tool-result confirmed); tools=False skips
        # it (no probe instruction, succeeds without any tool result).
        inv = ClaudeCodeInvoker(model="m")

        mock_run.return_value = _ok(_stream_with_tool_result())
        inv.invoke("sys", "task", tools=True)
        assert MCPStartupProbe.PROBE_NAME in _last_cmd(mock_run)[2]

        mock_run.return_value = _ok(_stream_ok('{"ok": 1}'))
        result = inv.invoke("sys", "task", tools=False)
        assert MCPStartupProbe.PROBE_NAME not in _last_cmd(mock_run)[2]
        assert result["output"] == {"ok": 1}

    @patch("src.backends.claude_code.subprocess.run")
    def test_each_invoke_spawns_fresh_subprocess(self, mock_run):
        # Probe "not cached": every invoke() is its own subprocess, so a healthy
        # first turn cannot mask a later unhealthy one. Two successful tool-using
        # turns spawn two distinct processes.
        inv = ClaudeCodeInvoker(model="m")
        mock_run.return_value = _ok(_stream_with_tool_result())
        inv.invoke("sys", "msg", tools=True)
        inv.invoke("sys", "msg", tools=True)
        assert mock_run.call_count == 2
