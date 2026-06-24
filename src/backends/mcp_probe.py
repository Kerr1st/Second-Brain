"""Shared MCP startup probe for agentic-CLI backends.

When an agentic stage (Explorer/Thinker, ``tools=True``) runs, the worst silent
failure is the one that caused the June 8–12 zero-candidate outage: the MCP
server *process attaches* but its tools are never actually usable — the model
runs blind and emits a confident, tool-less answer. ``--require-mcp-startup``
(Kiro) and ``mcp_servers.<n>.required=true`` (Codex) catch a server that fails
to *start*, but they cannot catch a server that started yet is unreachable (a
Codex sandbox blocking network/DB access) or tools that attached but returned
nothing usable. Process-attach alone is insufficient evidence (Req 5.4, 14.3).

The :class:`MCPStartupProbe` closes that gap. Folded into the agentic turn, it
instructs the stage to begin with one trivial, read-only tool call
(``memory_search`` with a fixed nonsense query, ``limit=1``) and then confirms
success **only** when a tool *result* actually came back through the CLI
envelope / ``--json`` event stream — not when a server merely spawned. On no
confirmed result it raises :class:`RuntimeError` naming the probe and the
backend, so the orchestrator treats it as an infrastructure failure (parity with
Kiro's ``--require-mcp-startup`` exit-3 behavior), never a fabricated verdict
(Req 5.1, 5.2, 14.1, 14.2).

The probe is **stateless**: it runs only when ``tools=True`` (Req 5.5, 14.5) and
holds no cross-invocation state, so each fresh ``invoke()`` subprocess performs
its own probe and a per-process attach/sandbox-block failure can never be masked
by a stale "healthy" result (Req 5.6, 14.5).

Both :class:`~src.backends.claude_code.ClaudeCodeInvoker` and
:class:`~src.backends.codex.CodexInvoker` reuse this helper from their
``_run_probe`` hook (see :class:`src.backends.agentic_cli.AgenticCliInvoker`).
For Claude Code the confirming signal is a completed tool-use with a result in
the JSON envelope/transcript; for Codex it is a completed tool call in the
``--json`` events. See ``docs/MODEL-BACKENDS.md``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --- Event-type classification ------------------------------------------------
# A tool RESULT (the tool ran and returned) is what confirms reachability. We
# match these against a lower-cased ``type`` field. Both ecosystems are covered:
# Anthropic message content blocks (``tool_result``) and Codex ``--json`` events
# (``mcp_tool_call_end`` / ``function_call_output`` / ``tool_call_output``).
_RESULT_TYPE_MARKERS = (
    "tool_result",
    "function_call_output",
    "tool_call_output",
)

# A tool's RESULT event for an MCP call commonly ends the call (``..._end``);
# pair that suffix with "tool" to avoid matching unrelated "_end" events.
_RESULT_END_SUFFIX = "_end"

# Intent/attach-only signals that MUST NOT count as confirmation: the model
# *deciding* to call a tool, the *start* of a call, or a server merely
# connecting/listing. These are exactly the process-attach evidence Req 5.4 /
# 14.3 reject as insufficient.
_NON_CONFIRMING_TYPES = frozenset(
    {
        "tool_use",
        "tool_call",
        "tool_call_begin",
        "mcp_tool_call_begin",
        "function_call",
        "mcp_server_connected",
        "mcp_connected",
        "mcp_list_tools",
        "mcp_list_tools_response",
    }
)


def _is_error_block(block: dict) -> bool:
    """Whether a tool-result block reports an error (so it does not confirm)."""
    if block.get("is_error") is True:
        return True
    status = str(block.get("status", "")).lower()
    return status in {"error", "failed", "failure"}


def _is_confirmed_result_block(block: dict) -> bool:
    """True iff ``block`` is a *completed, non-error tool result* event.

    Recognizes Anthropic ``tool_result`` content blocks and Codex tool-call
    output/end events. A bare ``tool_use`` / ``*_begin`` / server-connected event
    is intent or attach evidence only and never confirms (process-attach alone is
    insufficient — Req 5.4, 14.3).
    """
    raw_type = block.get("type")
    if not isinstance(raw_type, str):
        return False
    t = raw_type.lower()

    if t in _NON_CONFIRMING_TYPES:
        return False

    matched = any(marker in t for marker in _RESULT_TYPE_MARKERS) or (
        "tool" in t and t.endswith(_RESULT_END_SUFFIX)
    )
    if not matched:
        return False
    return not _is_error_block(block)


def _scan(node: Any) -> bool:
    """Recursively scan a parsed JSON value for a confirmed tool-result block."""
    if isinstance(node, dict):
        if _is_confirmed_result_block(node):
            return True
        return any(_scan(value) for value in node.values())
    if isinstance(node, list):
        return any(_scan(item) for item in node)
    return False


def _scan_text(raw: str) -> bool:
    """Scan raw CLI text for a confirmed tool result.

    Tolerates a ``--json`` event stream (one JSON object per line) as well as a
    single JSON envelope; non-JSON lines (prose, progress markers) are skipped.
    """
    for line in raw.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if _scan(obj):
            return True
    return False


def detect_tool_result(
    *,
    parsed: Any = None,
    events: Any = None,
    raw: Optional[str] = None,
) -> bool:
    """Return True iff any input shows a *completed tool result* came back.

    Inspects, in order, the parsed envelope (Claude ``.result`` / transcript),
    an explicit event list (Codex ``--json`` events), and finally the raw CLI
    text (a JSONL event stream). The signal is "a tool result actually
    returned", never "a server process attached".
    """
    if parsed is not None and _scan(parsed):
        return True
    if events is not None and _scan(events):
        return True
    if raw and _scan_text(raw):
        return True
    return False


class MCPStartupProbe:
    """Shared, stateless MCP reachability probe for agentic invocations.

    Used only on ``tools=True`` calls. Holds no cross-invocation state, so it can
    never mask a per-process attach/sandbox-block failure with a stale healthy
    result (Req 5.6, 14.5). Both agentic-CLI adapters call :meth:`run` from their
    ``_run_probe`` hook.
    """

    #: Name used in the raised error so failures are unmistakably the probe.
    PROBE_NAME = "MCP_Startup_Probe"
    #: The trivial, read-only tool the probe exercises (reaches Postgres).
    TOOL = "memory_search"
    #: A fixed nonsense query — the probe only cares that the call *returns*.
    QUERY = "__mcp_startup_probe__"
    #: Smallest possible result set.
    LIMIT = 1

    @classmethod
    def instruction(cls) -> str:
        """The instruction folded into an agentic turn to elicit one tool call.

        Phrased as a transparent, natural-language request: it explains *why*
        the check exists and asks the stage to begin with a single trivial,
        read-only tool call so that — when MCP tools are genuinely reachable — a
        tool *result* appears in the transcript/event stream for :meth:`run` to
        confirm. Earlier wording ("Before anything else … Disregard whatever it
        returns …") read like a prompt-injection and was refused outright by
        safety-tuned models, so the model never issued the call (Finding 2). The
        request retains the ``PROBE_NAME``/``TOOL``/``QUERY``/``LIMIT`` tokens so
        it stays self-describing, but drops the imperative/secrecy framing. It is
        generic on purpose — both the Claude Code and Codex adapters prepend it.
        """
        return (
            "To confirm your memory tools are connected and working, please "
            f"begin by using the `{cls.TOOL}` tool once to look up "
            f"\"{cls.QUERY}\" (limit={cls.LIMIT}). It's fine if it finds nothing "
            f"— this is just a quick startup check ({cls.PROBE_NAME}). After "
            "that, go ahead and complete the task normally."
        )

    @classmethod
    def run(
        cls,
        *,
        backend: str,
        needs_tools: bool,
        parsed: Any = None,
        events: Any = None,
        raw: Optional[str] = None,
    ) -> bool:
        """Confirm MCP tools were reachable, or fail loudly.

        Args:
            backend: Backend label, named in the error (e.g. ``"claude_code"``).
            needs_tools: The invocation's ``tools`` flag. When ``False`` the
                probe is skipped entirely (Req 5.5, 14.5) and returns ``False``.
            parsed: Parsed envelope/transcript to inspect (Claude Code).
            events: Explicit ``--json`` event list to inspect (Codex).
            raw: Raw CLI text to inspect as a fallback (JSONL event stream).

        Returns:
            ``True`` when a tool result was confirmed; ``False`` when the probe
            was skipped because ``needs_tools`` is ``False``.

        Raises:
            RuntimeError: When ``needs_tools`` is ``True`` but no completed tool
                result was confirmed (the tools did not attach or are
                unreachable). The message names the probe and the backend.
        """
        if not needs_tools:
            return False

        confirmed = detect_tool_result(parsed=parsed, events=events, raw=raw)
        if not confirmed:
            logger.error(
                "%s failed for backend %r: no MCP tool result returned through the "
                "CLI envelope/event stream (process-attach alone is insufficient).",
                cls.PROBE_NAME,
                backend,
            )
            raise RuntimeError(
                f"{cls.PROBE_NAME} failed for backend {backend!r}: the agentic "
                "stage requested tools but no MCP tool result was confirmed in the "
                "CLI envelope/event stream. The MCP tools did not attach or are "
                "unreachable (e.g. a sandbox blocking network/database access); "
                "process-attach alone is not sufficient evidence."
            )
        return True


__all__ = [
    "MCPStartupProbe",
    "detect_tool_result",
]
