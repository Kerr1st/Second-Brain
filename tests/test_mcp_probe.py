"""Tests for the shared MCP_Startup_Probe helper (src/backends/mcp_probe.py).

The probe is the secondary, sandbox-aware fail-loud guard both agentic-CLI
adapters (Claude Code, Codex) reuse. It confirms reachability ONLY when a tool
*result* actually returned through the CLI envelope/event stream — process
attach alone is insufficient — runs only when ``tools=True``, and is stateless
(never caches across invocations).

Validates: Requirements 5.1, 5.2, 5.4, 5.5, 5.6, 14.1, 14.2, 14.3, 14.5
See docs/MODEL-BACKENDS.md and the design "The MCP_Startup_Probe" section.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, strategies as st

from src.backends.mcp_probe import MCPStartupProbe, detect_tool_result


# --- Representative backend envelopes/events ---------------------------------

def _claude_envelope_with_tool_result(is_error: bool = False):
    """A Claude transcript-style payload with a completed tool-use + result."""
    return {
        "type": "result",
        "is_error": False,
        "result": "done",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Probing tools."},
                {"type": "tool_use", "name": "memory_search",
                 "input": {"query": "__mcp_startup_probe__", "limit": 1}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "is_error": is_error,
                 "content": [{"type": "text", "text": "[]"}]},
            ]},
        ],
    }


def _claude_envelope_attach_only():
    """A Claude payload where the model *intended* to call a tool but no result
    came back (server attached, tool never returned)."""
    return {
        "type": "result",
        "is_error": False,
        "result": "I could not reach my tools.",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "memory_search",
                 "input": {"query": "__mcp_startup_probe__", "limit": 1}},
            ]},
        ],
    }


def _codex_events_with_tool_result(error: bool = False):
    """A Codex --json event list with a completed MCP tool call."""
    return [
        {"type": "mcp_tool_call_begin", "tool": "memory_search"},
        {"type": "mcp_tool_call_end", "tool": "memory_search",
         "status": "failed" if error else "completed",
         "result": {"items": []}},
        {"type": "agent_message", "message": "done"},
    ]


def _codex_events_attach_only():
    """Codex events where the server connected and the call began but never
    produced a result."""
    return [
        {"type": "mcp_server_connected", "server": "second_brain"},
        {"type": "mcp_tool_call_begin", "tool": "memory_search"},
    ]


# --- detect_tool_result -------------------------------------------------------

class TestDetectToolResult:
    def test_detects_claude_tool_result(self):
        assert detect_tool_result(parsed=_claude_envelope_with_tool_result()) is True

    def test_claude_attach_only_not_confirmed(self):
        """A tool_use with no returning tool_result is process/intent evidence
        only — not a confirmed result (Req 5.4)."""
        assert detect_tool_result(parsed=_claude_envelope_attach_only()) is False

    def test_errored_claude_tool_result_not_confirmed(self):
        assert detect_tool_result(
            parsed=_claude_envelope_with_tool_result(is_error=True)
        ) is False

    def test_detects_codex_tool_result_via_events(self):
        assert detect_tool_result(events=_codex_events_with_tool_result()) is True

    def test_codex_attach_only_not_confirmed(self):
        assert detect_tool_result(events=_codex_events_attach_only()) is False

    def test_failed_codex_tool_call_not_confirmed(self):
        assert detect_tool_result(
            events=_codex_events_with_tool_result(error=True)
        ) is False

    def test_detects_tool_result_in_raw_jsonl(self):
        """Codex emits one JSON object per line; raw text is scanned as JSONL."""
        raw = "\n".join(json.dumps(e) for e in _codex_events_with_tool_result())
        assert detect_tool_result(raw=raw) is True

    def test_raw_prose_with_no_tool_result_not_confirmed(self):
        raw = "> I looked things over and here is my answer.\nNo tools were used."
        assert detect_tool_result(raw=raw) is False

    def test_empty_inputs_not_confirmed(self):
        assert detect_tool_result() is False
        assert detect_tool_result(parsed={}, events=[], raw="") is False

    def test_server_connected_alone_not_confirmed(self):
        """Process-attach (server connected) is explicitly insufficient."""
        assert detect_tool_result(
            parsed={"type": "mcp_server_connected", "server": "second_brain"}
        ) is False


# --- MCPStartupProbe.run: policy + fail-loud ---------------------------------

class TestProbePolicy:
    def test_skipped_when_toolless(self):
        """Req 5.5 / 14.5: the probe never runs on a tool-less stage. It returns
        False (skipped) and must not raise even with no tool evidence."""
        assert MCPStartupProbe.run(backend="claude_code", needs_tools=False) is False

    def test_confirmed_returns_true(self):
        assert MCPStartupProbe.run(
            backend="claude_code",
            needs_tools=True,
            parsed=_claude_envelope_with_tool_result(),
        ) is True

    def test_no_tool_result_raises_runtimeerror_naming_probe_and_backend(self):
        """Req 5.2 / 14.2: no confirmed result → RuntimeError naming the probe
        and the backend (an infrastructure failure, never a verdict)."""
        with pytest.raises(RuntimeError) as exc:
            MCPStartupProbe.run(
                backend="codex",
                needs_tools=True,
                events=_codex_events_attach_only(),
            )
        msg = str(exc.value)
        assert MCPStartupProbe.PROBE_NAME in msg
        assert "codex" in msg

    def test_errored_result_raises(self):
        with pytest.raises(RuntimeError):
            MCPStartupProbe.run(
                backend="claude_code",
                needs_tools=True,
                parsed=_claude_envelope_with_tool_result(is_error=True),
            )

    def test_not_cached_across_invocations(self):
        """Req 5.6 / 14.5: a first healthy probe must NOT mask a later failure.
        Two sequential runs are evaluated independently."""
        assert MCPStartupProbe.run(
            backend="claude_code",
            needs_tools=True,
            parsed=_claude_envelope_with_tool_result(),
        ) is True
        with pytest.raises(RuntimeError):
            MCPStartupProbe.run(
                backend="claude_code",
                needs_tools=True,
                parsed=_claude_envelope_attach_only(),
            )

    def test_instruction_mentions_trivial_tool_call(self):
        instr = MCPStartupProbe.instruction()
        assert MCPStartupProbe.TOOL in instr
        assert MCPStartupProbe.QUERY in instr
        assert f"limit={MCPStartupProbe.LIMIT}" in instr

    def test_instruction_omits_injection_flagged_phrases(self):
        """The reworded request drops the framing safety models flag as
        prompt-injection (Finding 2), while staying self-describing."""
        instr = MCPStartupProbe.instruction().lower()
        assert "before anything else" not in instr
        assert "disregard" not in instr
        assert "return only json" not in instr
        # ...but the self-describing tokens are still present.
        assert MCPStartupProbe.PROBE_NAME in MCPStartupProbe.instruction()


# --- Property: attach-only signals never confirm -----------------------------

_ATTACH_ONLY_TYPES = st.sampled_from([
    "tool_use",
    "tool_call",
    "tool_call_begin",
    "mcp_tool_call_begin",
    "function_call",
    "mcp_server_connected",
    "mcp_connected",
    "mcp_list_tools",
])


class TestProbeProperties:
    @given(types=st.lists(_ATTACH_ONLY_TYPES, min_size=1, max_size=8))
    def test_attach_or_intent_signals_never_confirm(self, types):
        """For any transcript built only from attach/intent signals (no returning
        tool result), the probe must NOT confirm — process-attach is insufficient.

        **Validates: Requirements 5.4, 14.3**
        """
        events = [{"type": t, "tool": "memory_search"} for t in types]
        assert detect_tool_result(events=events) is False
        with pytest.raises(RuntimeError):
            MCPStartupProbe.run(backend="codex", needs_tools=True, events=events)

    @given(needs_tools=st.booleans())
    def test_toolless_skips_tooled_evaluates(self, needs_tools):
        """tools=False always skips (returns False, no raise); tools=True with a
        confirmed result returns True.

        **Validates: Requirements 5.5, 14.5**
        """
        confirmed = _claude_envelope_with_tool_result()
        result = MCPStartupProbe.run(
            backend="claude_code", needs_tools=needs_tools, parsed=confirmed
        )
        assert result is needs_tools
