"""Preservation property tests for the stream-json probe fix.

Spec: .kiro/specs/claude-code-stream-json-probe-fix/ (Task 2).

Property 2 (Preservation) — for every input where ``isBugCondition`` is FALSE,
the fixed ``ClaudeCodeInvoker`` must produce the same observable result as the
original: ``F(X) == F'(X)``. These tests are written observation-first against
the UNFIXED adapter and MUST PASS on it, capturing the baseline contract to
preserve. The SAME tests are re-run after the fix (task 3.8) to prove no
regression.

``NOT isBugCondition(X)`` covers:
  * ``tools=True`` with no ``tool_result`` returned  → still ``RuntimeError``
    from ``MCP_Startup_Probe`` (Req 3.1).
  * ``tools=False`` evaluator/Express calls           → unchanged parse / usage /
    no-probe / native delivery / tool-less flags       (Req 3.2).
  * every failure mode (timeout, non-zero exit, ``is_error``, unrecoverable
    JSON, missing usage)                               → unchanged mapping
                                                          (Req 3.3).
  * public-flags only / whole-dict usage copy          (Req 3.4).
  * Codex + shared base behavior (PROBE_NAME retained) (Req 3.5).

Fixture note: these tests use the *single-object* result envelope. On the
unfixed adapter that is the live ``--output-format json`` envelope; it is also a
valid single-line JSONL stream, so the fixed adapter (which reads the terminal
``{"type":"result"}`` event of the JSONL stream) recovers the very same object —
which is exactly why the preserved behavior must hold across the fix.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""

from __future__ import annotations

import json
import string
import subprocess as _sp

import pytest
from hypothesis import given, settings, strategies as st
from unittest.mock import MagicMock, patch

from src.backends.base import parse_json_output
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.mcp_probe import MCPStartupProbe


# ---------------------------------------------------------------------------
# Fixture helpers — the single-object result envelope (current json format,
# and a valid one-line JSONL stream after the fix).
# ---------------------------------------------------------------------------
def _ok(stdout: str):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _envelope(
    result_text: str,
    *,
    is_error: bool = False,
    usage: dict | None = None,
    total_cost_usd=None,
    transcript: list | None = None,
) -> str:
    """A ``{"type":"result", ...}`` envelope with the given ``.result`` text.

    ``transcript`` (when given) carries the tool-use / tool-result content
    blocks the probe scans; omitting a ``tool_result`` block models a genuine
    MCP failure (Req 3.1).
    """
    env = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result_text,
    }
    if usage is not None:
        env["usage"] = usage
    if total_cost_usd is not None:
        env["total_cost_usd"] = total_cost_usd
    if transcript is not None:
        env["transcript"] = transcript
    return json.dumps(env)


def _envelope_with_tool_result(result_text: str, **extra) -> str:
    """Envelope whose transcript contains a completed, non-error tool result —
    the signal the probe confirms so a ``tools=True`` turn succeeds."""
    return _envelope(
        result_text,
        transcript=[
            {"type": "tool_use", "name": "memory_search"},
            {"type": "tool_result", "is_error": False, "content": "[]"},
        ],
        **extra,
    )


def _last_cmd(mock_run) -> list:
    return mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
_keys = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6)
_payloads = st.dictionaries(
    keys=_keys,
    values=st.integers(min_value=-1000, max_value=1000),
    min_size=1,
    max_size=4,
)
# Prose with no recoverable JSON. ``ascii_letters`` alone can spell the bare
# JSON literals ``true``/``false``/``null`` (which ``json.loads`` — and thus the
# shared ``parse_json_output`` backstop — parse successfully), so an unfiltered
# alphabet would occasionally emit a parseable token and defeat the intent. The
# ``.filter`` rejects any value ``parse_json_output`` could recover, leaving only
# genuine prose for which ``parse_json_output`` raises ``ValueError``.
def _parse_recovers(text: str) -> bool:
    try:
        parse_json_output(text)
        return True
    except ValueError:
        return False


_prose = st.text(
    alphabet=string.ascii_letters + " .,;:!?", min_size=1, max_size=60
).filter(lambda s: not _parse_recovers(s))
_extra_usage_keys = st.text(
    alphabet=string.ascii_lowercase + "_", min_size=1, max_size=12
).filter(lambda k: k not in {"input_tokens", "output_tokens", "total_cost_usd"})


# ===========================================================================
# Req 3.1 — genuine MCP failure (tools=True, no tool_result) STILL raises
# ===========================================================================
@settings(max_examples=40, deadline=None)
@given(payload=_payloads, include_tool_use=st.booleans())
@patch("src.backends.claude_code.subprocess.run")
def test_tools_true_without_tool_result_still_raises(
    mock_run, payload, include_tool_use
):
    """For all tools=True turns where no tool *result* came back, the probe
    raises RuntimeError naming the probe + backend (Req 3.1)."""
    transcript = (
        [{"type": "tool_use", "name": "memory_search"}] if include_tool_use else None
    )
    mock_run.return_value = _ok(_envelope(json.dumps(payload), transcript=transcript))

    with pytest.raises(RuntimeError) as exc:
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)

    msg = str(exc.value)
    assert MCPStartupProbe.PROBE_NAME in msg
    assert "claude_code" in msg


# ===========================================================================
# Req 3.2 — tool-less (tools=False) behavior unchanged
# ===========================================================================
@settings(max_examples=40, deadline=None)
@given(payload=_payloads)
@patch("src.backends.claude_code.subprocess.run")
def test_tools_false_parses_result_no_probe_native_delivery(mock_run, payload):
    """tools=False: parsed ``.result`` returned, no probe, native
    ``--system-prompt-file`` delivery, ``--strict-mcp-config`` + ``--tools ""``,
    and no ``--mcp-config`` (Req 3.2)."""
    result_text = json.dumps(payload)
    mock_run.return_value = _ok(_envelope(result_text))

    result = ClaudeCodeInvoker(model="m").invoke("sys", "user data", tools=False)

    assert set(result) == {"output", "raw", "usage", "usage_source"}
    assert result["output"] == payload
    assert result["raw"] == result_text

    cmd = _last_cmd(mock_run)
    # tool-less flag shape (Req 6 preserved)
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" not in cmd
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    # native system-prompt delivery (default "file")
    assert "--system-prompt-file" in cmd
    # no probe instruction folded into the -p positional, which stays the bare msg
    assert cmd[2] == "user data"
    assert MCPStartupProbe.PROBE_NAME not in cmd[2]


@settings(max_examples=40, deadline=None)
@given(
    payload=_payloads,
    in_tok=st.integers(min_value=0, max_value=100000),
    out_tok=st.integers(min_value=0, max_value=100000),
)
@patch("src.backends.claude_code.subprocess.run")
def test_tools_false_real_usage_when_present(mock_run, payload, in_tok, out_tok):
    """tools=False: real usage captured from the envelope when present (Req 3.2)."""
    usage = {"input_tokens": in_tok, "output_tokens": out_tok}
    mock_run.return_value = _ok(_envelope(json.dumps(payload), usage=usage))

    result = ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)

    assert result["usage_source"] == "real"
    assert result["usage"]["input_tokens"] == in_tok
    assert result["usage"]["output_tokens"] == out_tok


# ===========================================================================
# Req 3.3 — failure-mode parity
# ===========================================================================
@settings(max_examples=30, deadline=None)
@given(timeout=st.integers(min_value=1, max_value=600), tools=st.booleans())
@patch("src.backends.claude_code.subprocess.run")
def test_timeout_maps_to_timeouterror(mock_run, timeout, tools):
    """A subprocess timeout maps to TimeoutError (Req 3.3)."""
    mock_run.side_effect = _sp.TimeoutExpired(cmd="claude", timeout=timeout)
    with pytest.raises(TimeoutError):
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=tools, timeout=timeout)


@settings(max_examples=30, deadline=None)
@given(code=st.integers(min_value=1, max_value=255), tools=st.booleans())
@patch("src.backends.claude_code.subprocess.run")
def test_nonzero_exit_maps_to_runtimeerror(mock_run, code, tools):
    """A non-zero exit code maps to RuntimeError (Req 3.3)."""
    mock_run.return_value = MagicMock(returncode=code, stdout="", stderr="boom")
    with pytest.raises(RuntimeError):
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=tools)


@settings(max_examples=40, deadline=None)
@given(payload=_payloads, tools=st.booleans())
@patch("src.backends.claude_code.subprocess.run")
def test_is_error_envelope_maps_to_runtimeerror_even_tools_false(
    mock_run, payload, tools
):
    """An ``is_error`` envelope raises RuntimeError regardless of tool use
    (Req 3.3); the parseable ``.result`` ensures is_error — not ValueError —
    wins the failure-mode race."""
    mock_run.return_value = _ok(_envelope(json.dumps(payload), is_error=True))
    with pytest.raises(RuntimeError) as exc:
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=tools)
    assert "is_error" in str(exc.value)


@settings(max_examples=40, deadline=None)
@given(prose=_prose)
@patch("src.backends.claude_code.subprocess.run")
def test_unrecoverable_result_maps_to_valueerror(mock_run, prose):
    """An envelope ``.result`` with no recoverable JSON maps to ValueError
    (Req 3.3)."""
    mock_run.return_value = _ok(_envelope(prose))
    with pytest.raises(ValueError):
        ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)


@settings(max_examples=40, deadline=None)
@given(payload=_payloads)
@patch("src.backends.claude_code.subprocess.run")
def test_missing_usage_tolerate_and_warn_no_raise(mock_run, payload):
    """A parseable turn with no ``usage`` tolerates-and-warns: usage=None /
    usage_source='estimate', result preserved, never raises (Req 3.3)."""
    mock_run.return_value = _ok(_envelope(json.dumps(payload)))

    result = ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)

    assert result["output"] == payload
    assert result["usage"] is None
    assert result["usage_source"] == "estimate"


# ===========================================================================
# Req 3.4 — public flags only / no enterprise-managed-only branch / whole-dict usage copy
# ===========================================================================
@settings(max_examples=40, deadline=None)
@given(payload=_payloads, tools=st.booleans())
@patch("src.backends.claude_code.subprocess.run")
def test_command_uses_only_public_flags(mock_run, payload, tools):
    """The constructed command never contains enterprise-managed-wrapper-only flags (Req 3.4)."""
    stdout = (
        _envelope_with_tool_result(json.dumps(payload))
        if tools
        else _envelope(json.dumps(payload))
    )
    mock_run.return_value = _ok(stdout)
    ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=tools)

    cmd = _last_cmd(mock_run)
    for forbidden in ("--aws-profile", "--claude-help"):
        assert forbidden not in cmd


@settings(max_examples=40, deadline=None)
@given(
    payload=_payloads,
    in_tok=st.integers(min_value=0, max_value=100000),
    out_tok=st.integers(min_value=0, max_value=100000),
    extra=st.dictionaries(
        keys=_extra_usage_keys, values=st.integers(min_value=0, max_value=999),
        max_size=3,
    ),
    cost=st.floats(min_value=0, max_value=10, allow_nan=False, allow_infinity=False),
)
@patch("src.backends.claude_code.subprocess.run")
def test_usage_dict_copied_whole_with_no_asbx_branch(
    mock_run, payload, in_tok, out_tok, extra, cost
):
    """The entire ``usage`` dict is copied (enterprise-managed-divergent extra fields ride
    along) and ``total_cost_usd`` is folded in — no enterprise-managed-only branch (Req 3.4)."""
    usage = {"input_tokens": in_tok, "output_tokens": out_tok, **extra}
    mock_run.return_value = _ok(
        _envelope(json.dumps(payload), usage=usage, total_cost_usd=cost)
    )

    result = ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=False)

    assert result["usage_source"] == "real"
    for key, value in usage.items():
        assert result["usage"][key] == value
    assert result["usage"]["total_cost_usd"] == cost


# ===========================================================================
# Req 3.5 — shared probe instruction retains the tokens Codex + base depend on
# ===========================================================================
def test_shared_instruction_retains_probe_tokens():
    """The shared instruction retains PROBE_NAME / TOOL / QUERY, the contract
    that keeps Codex and the shared base unchanged (Req 3.5)."""
    instruction = MCPStartupProbe.instruction()
    assert MCPStartupProbe.PROBE_NAME in instruction
    assert MCPStartupProbe.TOOL in instruction
    assert MCPStartupProbe.QUERY in instruction
