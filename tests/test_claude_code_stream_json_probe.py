"""Bug-condition exploration test for the stream-json probe fix.

Spec: .kiro/specs/claude-code-stream-json-probe-fix/ (Task 1).

Property 1 (Bug Condition) — an agentic (``tools=True``) Claude Code turn where
the MCP tools genuinely attached AND a ``tool_result`` came back through the
``--output-format stream-json --verbose`` event stream MUST return a successful
``InvocationResult`` ``{output, raw, usage, usage_source}`` with **no**
false-positive ``RuntimeError`` from ``MCP_Startup_Probe``.

isBugCondition(X) = X.tools == True AND mcp_tools_attached(X) AND tool_result_returned(X)

**CRITICAL**: This test is EXPECTED TO FAIL on the unfixed adapter — that
failure confirms the bug. The unfixed ``ClaudeCodeInvoker`` builds
``--output-format json`` and recovers a single envelope by scanning the *whole*
stdout for the largest balanced JSON object. Against a JSONL event stream that
recovers an **interior** event (a long thinking/text block), not the terminal
``{"type":"result"}`` event, so ``.result``/``usage`` extraction reads the wrong
object and ``invoke(..., tools=True)`` never returns the real answer/usage. The
SAME test is re-run after the fix (task 3.7) to validate it.

**Validates: Requirements 1.1, 1.2, 2.1, 2.2, 2.4**
"""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import given, settings, strategies as st
from unittest.mock import MagicMock, patch

from src.backends.claude_code import ClaudeCodeInvoker


# ---------------------------------------------------------------------------
# stream-json JSONL fixture helpers (design "Unit Tests" section)
# ---------------------------------------------------------------------------
def _stream(events: list[dict]) -> str:
    """One JSON object per line (JSONL), as ``stream-json --verbose`` emits."""
    return "\n".join(json.dumps(e) for e in events)


def _ok(stdout: str):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _system_event() -> dict:
    return {"type": "system", "subtype": "init", "session_id": "s1"}


def _assistant_thinking_event(text: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": text}]}}


def _assistant_text_event(text: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def _assistant_tool_use_event(name: str = "memory_search") -> dict:
    return {"type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": name,
                 "input": {"query": "__mcp_startup_probe__", "limit": 1}}]}}


def _user_tool_result_event(is_error: bool = False) -> dict:
    return {"type": "user",
            "message": {"content": [
                {"type": "tool_result", "is_error": is_error, "content": "[]"}]}}


def _result_event(result_text: str, *, is_error: bool = False,
                  usage: dict | None = None, total_cost_usd=None) -> dict:
    env = {"type": "result", "subtype": "success", "is_error": is_error,
           "result": result_text}
    if usage is not None:
        env["usage"] = usage
    if total_cost_usd is not None:
        env["total_cost_usd"] = total_cost_usd
    return env


def _stream_with_tool_result(result_text: str, *, thinking: str = "",
                             usage: dict | None = None,
                             total_cost_usd=None) -> str:
    """system -> assistant(thinking) -> assistant(tool_use) -> user(tool_result)
    -> terminal result. The confirmed ``tool_result`` is what the probe must
    recognize; the terminal result event is where ``.result``/``usage`` live."""
    events = [_system_event()]
    if thinking:
        events.append(_assistant_thinking_event(thinking))
    events += [
        _assistant_tool_use_event(),
        _user_tool_result_event(is_error=False),
        _result_event(result_text, usage=usage, total_cost_usd=total_cost_usd),
    ]
    return _stream(events)


# ---------------------------------------------------------------------------
# Generators — generalize over the .result payload and interleaved thinking text
# ---------------------------------------------------------------------------
_payloads = st.dictionaries(
    keys=st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
    values=st.integers(min_value=-1000, max_value=1000),
    min_size=1,
    max_size=4,
)


@settings(max_examples=40, deadline=None)
@given(
    payload=_payloads,
    # An Explorer/Thinker turn emits a substantial thinking block before the
    # final answer — generalized here, and always large enough that the unfixed
    # "largest balanced JSON object" backstop latches onto it instead of the
    # terminal result event.
    thinking_pad=st.integers(min_value=400, max_value=900),
    in_tok=st.integers(min_value=1, max_value=10000),
    out_tok=st.integers(min_value=1, max_value=10000),
)
@patch("src.backends.claude_code.subprocess.run")
def test_agentic_turn_with_real_tool_result_succeeds(
    mock_run, payload, thinking_pad, in_tok, out_tok
):
    """Bug condition: tools=True + a real tool_result in the stream must succeed.

    On the FIXED adapter: parse the stream, confirm the real ``tool_result`` via
    ``detect_tool_result``, read ``.result``/``usage`` from the terminal result
    event, and return ``{output, raw, usage, usage_source}`` with no raise.

    On the UNFIXED adapter: this FAILS (bug confirmed) — extraction recovers an
    interior event from the JSONL, so ``output``/``usage`` are wrong (or the
    probe raises), never the real answer.
    """
    result_text = json.dumps(payload)
    usage = {"input_tokens": in_tok, "output_tokens": out_tok}
    stdout = _stream_with_tool_result(
        result_text,
        thinking="x" * thinking_pad,
        usage=usage,
        total_cost_usd=0.01,
    )
    mock_run.return_value = _ok(stdout)

    # No false-positive RuntimeError, and the real answer/usage come back.
    result = ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)

    assert set(result) == {"output", "raw", "usage", "usage_source"}
    # .result must be read from the TERMINAL result event (not an interior one).
    assert result["output"] == payload
    assert result["raw"] == result_text
    # usage must be the real metered counts from the terminal result event.
    assert result["usage_source"] == "real"
    assert result["usage"]["input_tokens"] == in_tok
    assert result["usage"]["output_tokens"] == out_tok


# ---------------------------------------------------------------------------
# Edge — no terminal result event (Task 3.3, "No result event" decision)
# ---------------------------------------------------------------------------
@patch("src.backends.claude_code.subprocess.run")
def test_no_result_event_raises_valueerror(mock_run):
    """A stream missing the terminal ``{"type":"result"}`` event -> ``ValueError``.

    A completed ``stream-json --verbose`` turn is required by the format to end
    with exactly one terminal result event; its absence is a malformed/truncated
    stream. ``_envelope_from`` returns ``None``, ``_extract_raw`` returns ``""``,
    and the shared ``parse_json_output("")`` backstop raises ``ValueError`` — it
    must NOT recover an interior event (the thinking/text block) as a bogus
    answer.

    **Validates: Requirements 2.4, 3.3**
    """
    stdout = _stream([
        _system_event(),
        _assistant_thinking_event("x" * 600),
        _assistant_text_event("interior answer that must NOT be recovered"),
    ])
    mock_run.return_value = _ok(stdout)

    with pytest.raises(ValueError):
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)
