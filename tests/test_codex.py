"""Mocked-subprocess unit tests for CodexInvoker — task 5.1 scope.

Covers command construction (the documented ``codex exec`` surface), Invoker
conformance, system-prompt delivery (native ``model_instructions_file`` /
``developer_instructions`` / ``instructions`` + the prepend fallback), and
final-text extraction (``--output-last-message`` file and ``--json`` events).

Every test mocks ``src.backends.codex.subprocess.run`` so CI and unit testing do
not require a live CLI (Req 22.6). The MCP-attach/sandbox/fail-loud (5.2) and
usage/failure-mode (5.3) behaviors are covered by later sub-tasks.

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 11.1, 11.2, 11.3, 11.4, 12.1,
12.2, 12.3, 21.2
"""

from __future__ import annotations

import json
import os

import pytest
from unittest.mock import MagicMock, patch

from src.backends.base import Invoker
from src.backends.codex import CodexInvoker


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


def _writes_last_message(result_text: str):
    """Return ``result_text`` through whichever documented mode was requested."""

    def _side_effect(cmd, **kwargs):
        if "--output-last-message" in cmd:
            path = cmd[cmd.index("--output-last-message") + 1]
            with open(path, "w", encoding="utf-8") as f:
                f.write(result_text)
            return _ok("")
        return _ok(_json_events_stream_with_usage(result_text))

    return _side_effect


def _json_events_stream(result_text: str) -> str:
    """A ``--json`` event stream whose final agent_message carries result_text."""
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps(
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": result_text}}
        ),
    ]
    return "\n".join(lines)


def _json_events_stream_with_usage(result_text: str) -> str:
    """A successful JSONL turn with final text and real token usage."""
    return "\n".join([
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }),
        _json_events_stream(result_text),
    ])


class TestConstruction:
    def test_accepts_model(self):
        inv = CodexInvoker(model="gpt-5-codex")
        assert inv.model == "gpt-5-codex"

    def test_blank_model_rejected(self):
        # Metered backend: a blank model id must not silently bill a default.
        with pytest.raises(ValueError):
            CodexInvoker(model="")
        with pytest.raises(ValueError):
            CodexInvoker()

    def test_invalid_delivery_rejected(self):
        with pytest.raises(ValueError):
            CodexInvoker(model="m", system_prompt_delivery="bogus")

    def test_invalid_final_source_rejected(self):
        with pytest.raises(ValueError):
            CodexInvoker(model="m", final_message_source="bogus")

    def test_satisfies_invoker_protocol(self):
        assert isinstance(CodexInvoker(model="m"), Invoker)


class TestCommandConstruction:
    """Req 11: documented surface — codex exec <msg> -m <id> ..."""

    @patch("src.backends.codex.subprocess.run")
    def test_core_flags(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="gpt-5-codex").invoke("sys", "hello")
        cmd = _last_cmd(mock_run)

        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        # user message is the positional prompt input, distinct from system prompt
        assert cmd[2] == "hello"
        assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5-codex"

    @patch("src.backends.codex.subprocess.run")
    def test_configurable_binary_path(self, mock_run, monkeypatch):
        """Req 11 portability: CODEX_CLI overrides the binary; mirrors KIRO_CLI."""
        monkeypatch.setattr("src.backends.codex.CODEX_CLI", "/opt/codex/codex")
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "hi")
        assert _last_cmd(mock_run)[0] == "/opt/codex/codex"

    @patch("src.backends.codex.subprocess.run")
    def test_effort_passed_as_config(self, mock_run):
        # Req 11.2 / 21.2: effort via -c model_reasoning_effort=<level>.
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "hi", effort="high")
        cmd = _last_cmd(mock_run)
        assert "-c" in cmd
        assert "model_reasoning_effort=high" in cmd

    @patch("src.backends.codex.subprocess.run")
    def test_effort_omitted_when_absent(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "hi")
        assert "model_reasoning_effort=" not in " ".join(_last_cmd(mock_run))

    @patch("src.backends.codex.subprocess.run")
    def test_output_schema_flag_when_provided(self, mock_run):
        # Req 11.4: schema-constrained output via --output-schema.
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "hi", output_schema="/tmp/schema.json")
        cmd = _last_cmd(mock_run)
        assert "--output-schema" in cmd
        assert cmd[cmd.index("--output-schema") + 1] == "/tmp/schema.json"

    @patch("src.backends.codex.subprocess.run")
    def test_output_schema_omitted_by_default(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "hi")
        assert "--output-schema" not in _last_cmd(mock_run)


class TestSystemPromptDelivery:
    """Req 12: native delivery (file preferred / inline) + prepend fallback."""

    @patch("src.backends.codex.subprocess.run")
    def test_model_instructions_file_default(self, mock_run):
        captured = {}

        def _capture(cmd, **kwargs):
            # The temp file must exist and hold the system prompt at call time.
            i = cmd.index("-c")
            # find the model_instructions_file=<path> config value
            path = None
            for tok in cmd:
                if tok.startswith("model_instructions_file="):
                    path = tok.split("=", 1)[1]
            assert path is not None
            with open(path, encoding="utf-8") as f:
                captured["contents"] = f.read()
            captured["cmd"] = cmd
            return _ok(_json_events_stream_with_usage('{"ok": 1}'))

        mock_run.side_effect = _capture
        CodexInvoker(model="m").invoke("SYSTEM ROLE", "user data")
        assert captured["contents"] == "SYSTEM ROLE"
        # user message stays distinct from the system prompt (Req 12.2)
        assert captured["cmd"][2] == "user data"

    @patch("src.backends.codex.subprocess.run")
    def test_model_instructions_file_cleaned_up(self, mock_run):
        seen = {}

        def _capture(cmd, **kwargs):
            for tok in cmd:
                if tok.startswith("model_instructions_file="):
                    seen["path"] = tok.split("=", 1)[1]
            return _ok(_json_events_stream_with_usage('{"ok": 1}'))

        mock_run.side_effect = _capture
        CodexInvoker(model="m").invoke("sys", "msg")
        assert not os.path.exists(seen["path"])

    @patch("src.backends.codex.subprocess.run")
    def test_developer_instructions_inline(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m", system_prompt_delivery="developer_instructions").invoke(
            "SYS", "msg"
        )
        cmd = _last_cmd(mock_run)
        assert "developer_instructions=SYS" in cmd
        assert cmd[2] == "msg"

    @patch("src.backends.codex.subprocess.run")
    def test_instructions_inline(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m", system_prompt_delivery="instructions").invoke(
            "SYS", "msg"
        )
        cmd = _last_cmd(mock_run)
        assert "instructions=SYS" in cmd
        assert cmd[2] == "msg"

    @patch("src.backends.codex.subprocess.run")
    def test_prepend_fallback(self, mock_run):
        """Req 12.3: fallback folds the system prompt into the user message."""
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m", system_prompt_delivery="prepend").invoke(
            "SYS ROLE", "USER MSG"
        )
        cmd = _last_cmd(mock_run)
        assert cmd[2] == "SYS ROLE\n\nUSER MSG"
        # no inline instruction config when prepending
        joined = " ".join(cmd)
        assert "developer_instructions=" not in joined
        assert "instructions=" not in joined
        assert "model_instructions_file=" not in joined


class TestFinalTextExtraction:
    """Req 11.3 / 10.3-10.4: final text from --output-last-message or --json."""

    @patch("src.backends.codex.subprocess.run")
    def test_json_event_stream_with_real_usage_is_default(self, mock_run):
        payload = {"candidates": [{"title": "insight"}]}
        stream = "\n".join([
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 123, "output_tokens": 45},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(payload),
                },
            }),
        ])
        mock_run.return_value = _ok(stream)
        result = CodexInvoker(model="m").invoke("sys", "msg")

        cmd = _last_cmd(mock_run)
        assert "--json" in cmd
        assert "--output-last-message" not in cmd
        assert set(result) == {"output", "raw", "usage", "usage_source"}
        assert result["output"] == payload
        assert result["raw"] == json.dumps(payload)
        assert result["usage"] == {"input_tokens": 123, "output_tokens": 45}
        assert result["usage_source"] == "real"

    @patch("src.backends.codex.subprocess.run")
    def test_output_last_message_file_cleaned_up(self, mock_run):
        seen = {}

        def _capture(cmd, **kwargs):
            path = cmd[cmd.index("--output-last-message") + 1]
            seen["path"] = path
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"ok": 1}')
            return _ok("")

        mock_run.side_effect = _capture
        CodexInvoker(
            model="m", final_message_source="output-last-message"
        ).invoke("sys", "msg")
        assert not os.path.exists(seen["path"])

    @patch("src.backends.codex.subprocess.run")
    def test_output_last_message_prose_wrapped(self, mock_run):
        payload = {"verdict": "ACCEPT"}
        result_text = f"Here is my answer:\n{json.dumps(payload)}\nDone."
        mock_run.side_effect = _writes_last_message(result_text)
        result = CodexInvoker(
            model="m", final_message_source="output-last-message"
        ).invoke("sys", "msg")
        assert result["output"] == payload
        assert result["raw"] == result_text

    @patch("src.backends.codex.subprocess.run")
    def test_json_event_stream_extraction(self, mock_run):
        payload = {"candidates": []}
        inv = CodexInvoker(model="m", final_message_source="json")
        mock_run.return_value = _ok(_json_events_stream(json.dumps(payload)))
        result = inv.invoke("sys", "msg")

        cmd = _last_cmd(mock_run)
        assert "--json" in cmd
        assert "--output-last-message" not in cmd
        assert result["output"] == payload
        assert result["raw"] == json.dumps(payload)

    @patch("src.backends.codex.subprocess.run")
    def test_json_event_stream_takes_last_agent_message(self, mock_run):
        # Two agent messages; the final one is the assistant's last turn.
        stream = "\n".join([
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "draft"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message",
                                 "text": json.dumps({"final": True})}}),
        ])
        inv = CodexInvoker(model="m", final_message_source="json")
        mock_run.return_value = _ok(stream)
        result = inv.invoke("sys", "msg")
        assert result["output"] == {"final": True}

    @patch("src.backends.codex.subprocess.run")
    def test_unparseable_final_text_raises_valueerror(self, mock_run):
        mock_run.side_effect = _writes_last_message("just prose, no json here")
        with pytest.raises(ValueError):
            CodexInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.codex.subprocess.run")
    def test_output_last_message_usage_is_estimate(self, mock_run):
        # Explicit file mode has no JSONL usage events.
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        result = CodexInvoker(
            model="m", final_message_source="output-last-message"
        ).invoke("sys", "msg")
        assert result["usage"] is None
        assert result["usage_source"] == "estimate"


# --- Task 5.2: MCP attach/sandbox, fail-loud, tool-less enforcement ----------
# Validates: Requirements 13.1, 13.2, 13.3, 13.4, 14.2, 14.4, 14.5, 15.1, 15.2,
# 15.3

from src.backends.mcp_probe import MCPStartupProbe  # noqa: E402
from src.backends import codex as _codex_mod  # noqa: E402
import sys  # noqa: E402


def _tool_result_event() -> str:
    """A Codex ``--json`` event line showing a completed MCP tool call.

    This is the signal the MCP_Startup_Probe confirms — a tool *result* actually
    returned, not merely a server process attaching (Req 14.3).
    """
    return json.dumps(
        {"type": "mcp_tool_call_end",
         "item": {"type": "tool_call_output", "status": "completed"}}
    )


def _writes_last_message_with_events(result_text: str, events: str = ""):
    """Return a tool-using result through the requested Codex output mode."""

    def _side_effect(cmd, **kwargs):
        if "--output-last-message" in cmd:
            path = cmd[cmd.index("--output-last-message") + 1]
            with open(path, "w", encoding="utf-8") as f:
                f.write(result_text)
            return _ok(events)
        lines = [events] if events else []
        lines.extend([
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": result_text},
            }),
        ])
        return _ok("\n".join(lines))

    return _side_effect


def _agentic_ok(result_text: str):
    """Successful tools=True completion: final message file + a confirming tool
    result event on stdout."""
    return _writes_last_message_with_events(result_text, _tool_result_event())


class TestMcpAttachAndSandbox:
    """Req 13: tools=True configures the MCP server (cwd, required) + sandbox."""

    @patch("src.backends.codex.subprocess.run")
    def test_tools_true_emits_mcp_server_config(self, mock_run):
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=True)
        cmd = _last_cmd(mock_run)
        joined = " ".join(cmd)

        # Req 13.1: command = python -m src.mcp_server
        assert f"mcp_servers.second_brain.command={json.dumps(sys.executable)}" in cmd
        assert 'mcp_servers.second_brain.args=["-m", "src.mcp_server"]' in cmd
        # Req 13.3: required=true is the primary startup guard
        assert "mcp_servers.second_brain.required=true" in cmd
        assert "src.mcp_server" in joined

    @patch("src.backends.codex.subprocess.run")
    def test_tools_true_cwd_is_repo_root(self, mock_run):
        # Req 13.2: server cwd is the adapter's computed repo root, mirroring the
        # repo-root convention the other adapters (kiro/claude) use exactly.
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=True)
        cmd = _last_cmd(mock_run)

        assert f"mcp_servers.second_brain.cwd={json.dumps(_codex_mod._REPO_ROOT)}" in cmd
        # the convention matches the other adapters' repo-root computation
        from src.backends.kiro import _SECOND_BRAIN_MCP as _KIRO_MCP
        assert _codex_mod._REPO_ROOT == _KIRO_MCP["second-brain"]["cwd"]

    @patch("src.backends.codex.subprocess.run")
    def test_tools_true_sandbox_workspace_write_with_network(self, mock_run):
        # Req 13.4: --sandbox workspace-write + network_access=true.
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=True)
        cmd = _last_cmd(mock_run)

        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
        assert "sandbox_workspace_write.network_access=true" in cmd

    @patch("src.backends.codex.subprocess.run")
    def test_tools_true_prepends_probe_instruction(self, mock_run):
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "the real task", tools=True)
        prompt_input = _last_cmd(mock_run)[2]
        assert MCPStartupProbe.PROBE_NAME in prompt_input
        assert "the real task" in prompt_input


class TestToolLessEnforcement:
    """Req 15: tools=False loads no MCP servers, runs read-only, no probe."""

    @patch("src.backends.codex.subprocess.run")
    def test_tools_false_no_mcp_servers(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=False)
        joined = " ".join(_last_cmd(mock_run))
        assert "mcp_servers" not in joined

    @patch("src.backends.codex.subprocess.run")
    def test_tools_false_sandbox_read_only(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=False)
        cmd = _last_cmd(mock_run)
        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert "workspace-write" not in cmd
        assert "network_access=true" not in " ".join(cmd)

    @patch("src.backends.codex.subprocess.run")
    def test_tools_false_does_not_prepend_probe(self, mock_run):
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "user data", tools=False)
        assert _last_cmd(mock_run)[2] == "user data"

    @patch("src.backends.codex.subprocess.run")
    def test_tools_false_skips_probe_even_without_tool_result(self, mock_run):
        # No tool-result anywhere, yet tools=False must NOT raise (Req 15.3).
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        result = CodexInvoker(model="m").invoke("sys", "msg", tools=False)
        assert result["output"] == {"ok": 1}


class TestFailLoud:
    """Req 14: secondary probe raises when tools attach but are unreachable."""

    @patch("src.backends.codex.subprocess.run")
    def test_probe_failure_maps_to_runtimeerror(self, mock_run):
        # Server "started" (no startup hard-fail) but the sandbox blocked it, so
        # no tool result comes back → RuntimeError naming the probe (Req 14.2).
        mock_run.side_effect = _writes_last_message_with_events('{"ok": 1}', "")
        with pytest.raises(RuntimeError) as exc:
            CodexInvoker(model="m").invoke("sys", "msg", tools=True)
        assert MCPStartupProbe.PROBE_NAME in str(exc.value)

    @patch("src.backends.codex.subprocess.run")
    def test_tools_true_with_tool_result_succeeds(self, mock_run):
        mock_run.side_effect = _agentic_ok('{"candidates": []}')
        result = CodexInvoker(model="m").invoke("sys", "msg", tools=True)
        assert result["output"] == {"candidates": []}

    @patch("src.backends.codex.subprocess.run")
    def test_probe_confirms_via_json_event_stream(self, mock_run):
        # final_message_source="json": the tool result and final agent_message
        # both ride the --json stream on stdout.
        inv = CodexInvoker(model="m", final_message_source="json")
        stream = "\n".join([
            _tool_result_event(),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message",
                                 "text": json.dumps({"candidates": []})}}),
        ])
        mock_run.return_value = _ok(stream)
        result = inv.invoke("sys", "msg", tools=True)
        assert result["output"] == {"candidates": []}

    @patch("src.backends.codex.subprocess.run")
    def test_probe_not_cached_second_call_can_fail(self, mock_run):
        # Req 14.5: the probe holds no "healthy" state across invocations. First
        # tool-using call confirms a result; the second does not and must raise.
        inv = CodexInvoker(model="m")
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        inv.invoke("sys", "msg", tools=True)

        mock_run.side_effect = _writes_last_message_with_events('{"ok": 1}', "")
        with pytest.raises(RuntimeError):
            inv.invoke("sys", "msg", tools=True)

    @patch("src.backends.codex.subprocess.run")
    def test_nonzero_exit_is_runtimeerror_primary_guard(self, mock_run):
        # Req 14.4: required=true → codex hard-fails at startup → non-zero exit →
        # RuntimeError via the base (the PRIMARY guard, before any probe).
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="MCP server 'second_brain' failed to start"
        )
        with pytest.raises(RuntimeError):
            CodexInvoker(model="m").invoke("sys", "msg", tools=True)


# --- Task 5.3: usage capture (tolerate-and-warn), failure-mode parity --------
# Validates: Requirements 16.1, 16.2, 16.3, 16.4, 17.1, 17.2, 17.3, 18.1, 18.2,
# 18.3


def _turn_completed_usage(usage: dict, total_cost_usd=None) -> str:
    """A ``--json`` turn.completed event carrying an explicit usage object."""
    event = {"type": "turn.completed", "usage": dict(usage)}
    if total_cost_usd is not None:
        event["total_cost_usd"] = total_cost_usd
    return json.dumps(event)


def _token_count_event(input_tokens: int, output_tokens: int) -> str:
    """A ``--json`` token_count event with the info.total_token_usage wrapper."""
    return json.dumps(
        {"type": "token_count",
         "info": {"total_token_usage": {"input_tokens": input_tokens,
                                        "output_tokens": output_tokens}}}
    )


def _json_stream_with_usage(result_text: str, usage_line: str) -> str:
    """A full ``--json`` stream: a usage event plus the final agent_message."""
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        usage_line,
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": result_text}}),
    ])


class TestUsageCapture:
    """Req 16: real usage from --json events + tolerate-and-warn fallback."""

    @patch("src.backends.codex.subprocess.run")
    def test_real_usage_from_turn_completed_event(self, mock_run):
        # Req 16.1: --json stream with a usage event → usage_source="real".
        inv = CodexInvoker(model="m", final_message_source="json")
        stream = _json_stream_with_usage(
            '{"ok": 1}',
            _turn_completed_usage(
                {"input_tokens": 123, "output_tokens": 456,
                 "cached_input_tokens": 7},
                total_cost_usd=0.0123,
            ),
        )
        mock_run.return_value = _ok(stream)
        result = inv.invoke("sys", "msg")

        assert result["usage_source"] == "real"
        assert result["usage"]["input_tokens"] == 123
        assert result["usage"]["output_tokens"] == 456
        assert result["usage"]["cached_input_tokens"] == 7
        assert result["usage"]["total_cost_usd"] == 0.0123
        # the result is still preserved alongside the real usage
        assert result["output"] == {"ok": 1}

    @patch("src.backends.codex.subprocess.run")
    def test_real_usage_from_token_count_event(self, mock_run):
        # The token_count/info.total_token_usage shape is also recognized.
        inv = CodexInvoker(model="m", final_message_source="json")
        stream = _json_stream_with_usage('{"ok": 1}', _token_count_event(10, 20))
        mock_run.return_value = _ok(stream)
        result = inv.invoke("sys", "msg")

        assert result["usage_source"] == "real"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 20
        assert "total_cost_usd" not in result["usage"]

    @patch("src.backends.codex.subprocess.run")
    def test_last_usage_event_wins(self, mock_run):
        # Codex emits cumulative token_count events; the final carries the total.
        inv = CodexInvoker(model="m", final_message_source="json")
        stream = "\n".join([
            _token_count_event(5, 5),
            _token_count_event(100, 200),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message",
                                 "text": json.dumps({"ok": 1})}}),
        ])
        mock_run.return_value = _ok(stream)
        result = inv.invoke("sys", "msg")
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 200

    @patch("src.backends.codex.subprocess.run")
    def test_real_usage_recorded_in_metrics(self, mock_run, tmp_path, monkeypatch):
        # Real counts are folded into the per-call metrics line (Req 16.1).
        metrics_dir = tmp_path / "m"
        monkeypatch.setattr("src.backends.agentic_cli.METRICS_DIR", str(metrics_dir))
        inv = CodexInvoker(model="m", final_message_source="json")
        stream = _json_stream_with_usage(
            '{"ok": 1}',
            _turn_completed_usage({"input_tokens": 11, "output_tokens": 22},
                                  total_cost_usd=0.5),
        )
        mock_run.return_value = _ok(stream)
        inv.invoke("sys", "msg", run_id="run1")

        line = (metrics_dir / "run1.jsonl").read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        assert rec["usage_source"] == "real"
        assert rec["real_input_tokens"] == 11
        assert rec["real_output_tokens"] == 22
        assert rec["total_cost_usd"] == 0.5

    @patch("src.backends.codex.subprocess.run")
    def test_missing_usage_tolerate_and_warn(self, mock_run, caplog):
        # Explicit output-last-message mode emits no --json usage events on
        # stdout: keep result, usage=None/"estimate", warn, do NOT raise
        # (Req 16.2, 17.1).
        mock_run.side_effect = _writes_last_message('{"candidates": []}')
        with caplog.at_level("WARNING"):
            result = CodexInvoker(
                model="m", final_message_source="output-last-message"
            ).invoke("sys", "msg")

        assert result["output"] == {"candidates": []}
        assert result["usage"] is None
        assert result["usage_source"] == "estimate"
        assert any(
            "usage" in r.message.lower() and r.levelname == "WARNING"
            for r in caplog.records
        )

    @patch("src.backends.codex.subprocess.run")
    def test_json_mode_without_usage_event_tolerate_and_warn(self, mock_run):
        # --json stream present but carrying no usage event: still tolerate.
        inv = CodexInvoker(model="m", final_message_source="json")
        mock_run.return_value = _ok(_json_events_stream(json.dumps({"ok": 1})))
        result = inv.invoke("sys", "msg")
        assert result["usage"] is None
        assert result["usage_source"] == "estimate"
        assert result["output"] == {"ok": 1}

    @patch("src.backends.codex.subprocess.run")
    def test_missing_usage_does_not_raise(self, mock_run):
        # Explicit: the tolerate path never routes into the failure path.
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        result = CodexInvoker(
            model="m", final_message_source="output-last-message"
        ).invoke("sys", "msg")
        assert result["usage"] is None


class TestFailureModeParity:
    """Req 17 / 18: every failure-mode row maps to the documented exception."""

    @patch("src.backends.codex.subprocess.run")
    def test_timeout_maps_to_timeouterror(self, mock_run):
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired(cmd="codex", timeout=1)
        with pytest.raises(TimeoutError):
            CodexInvoker(model="m").invoke("sys", "msg", timeout=1)

    @patch("src.backends.codex.subprocess.run")
    def test_nonzero_exit_maps_to_runtimeerror(self, mock_run):
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="boom")
        with pytest.raises(RuntimeError):
            CodexInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.codex.subprocess.run")
    def test_unrecoverable_json_maps_to_valueerror(self, mock_run):
        mock_run.side_effect = _writes_last_message("no json at all, just prose")
        with pytest.raises(ValueError):
            CodexInvoker(model="m").invoke("sys", "msg")

    @patch("src.backends.codex.subprocess.run")
    def test_real_usage_path_preserves_failure_mode_exits(self, mock_run):
        # A non-zero exit is mapped before usage extraction even when --json is
        # requested — usage capture never masks an infrastructure failure.
        inv = CodexInvoker(model="m", final_message_source="json")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        with pytest.raises(RuntimeError):
            inv.invoke("sys", "msg")


class TestSchemaPreferredOutput:
    """Req 18.1/18.2: schema-constrained output preferred; parser is backstop."""

    @patch("src.backends.codex.subprocess.run")
    def test_schema_output_parsed_directly(self, mock_run):
        # With --output-schema requested, the final message is schema-valid JSON
        # and parses straight through the shared parse_json_output backstop.
        schema_payload = {"verdict": "ACCEPT", "score": 9}
        mock_run.side_effect = _writes_last_message(json.dumps(schema_payload))
        result = CodexInvoker(model="m").invoke(
            "sys", "msg", output_schema="/tmp/schema.json"
        )
        cmd = _last_cmd(mock_run)
        assert "--output-schema" in cmd
        assert result["output"] == schema_payload


# --- Task 5.4: explicit no-live-CLI / single-spawn coverage ------------------
# The 5.1-5.3 classes above already cover the full 5.4 checklist (command
# construction, tools=True/False shapes, sandbox flags, mcp_servers cwd +
# required=true, native + fallback system-prompt delivery, output-last-message /
# --json extraction, usage capture + tolerate-and-warn, every failure-mode row,
# and probe secondary-check/skipped-tool-less/not-cached). This final class
# pins the Req 22.6 guarantee head-on: every invoke() is served entirely by the
# mocked subprocess (no live Codex CLI is ever required for unit testing) and
# each invoke() is exactly one fresh spawn, so the secondary probe is
# never a second process and never cached across calls.
# Validates: Requirements 22.4, 22.6


class TestNoLiveCliSingleSpawn:
    """Req 22.6: mocked subprocess only — no live CLI, one spawn per invoke()."""

    @patch("src.backends.codex.subprocess.run")
    def test_tool_less_invoke_is_single_spawn(self, mock_run):
        # tools=False: one spawn, no probe, no live CLI.
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=False)
        assert mock_run.call_count == 1

    @patch("src.backends.codex.subprocess.run")
    def test_tool_using_invoke_is_single_spawn(self, mock_run):
        # tools=True: the SECONDARY probe inspects THIS subprocess's output — it
        # is not a second spawn (Req 14.3 secondary check, not a separate CLI run).
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg", tools=True)
        assert mock_run.call_count == 1

    @patch("src.backends.codex.subprocess.run")
    def test_each_invoke_is_a_fresh_spawn_not_cached(self, mock_run):
        # Req 14.5: every invoke() is its own fresh subprocess; nothing about a
        # prior call (probe health, output) is reused. N invokes ⇒ N spawns.
        inv = CodexInvoker(model="m")
        mock_run.side_effect = _agentic_ok('{"ok": 1}')
        inv.invoke("sys", "msg", tools=True)
        inv.invoke("sys", "msg", tools=True)
        inv.invoke("sys", "msg", tools=False)
        assert mock_run.call_count == 3

    @patch("src.backends.codex.subprocess.run")
    def test_no_live_cli_binary_is_executed(self, mock_run):
        # The argv targets the configured CODEX_CLI binary but is handed to the
        # MOCK, never to a real process — proving the suite needs no Codex install.
        mock_run.side_effect = _writes_last_message('{"ok": 1}')
        CodexInvoker(model="m").invoke("sys", "msg")
        assert mock_run.called
        # argv was constructed (binary + exec subcommand) yet only the mock ran.
        cmd = _last_cmd(mock_run)
        assert cmd[0] == _codex_mod.CODEX_CLI
        assert cmd[1] == "exec"
