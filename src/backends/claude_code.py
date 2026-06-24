"""Claude Code backend adapter — ``claude -p`` subprocesses.

Wraps the public ``claude -p`` CLI as an :class:`src.backends.base.Invoker`, so
the dream-cycle orchestrator can run on Claude Code with no orchestrator change.
Like :class:`src.backends.kiro.KiroInvoker`, it drives the shared
:class:`src.backends.agentic_cli.AgenticCliInvoker` template and overrides only
the Claude-Code-specific hooks; the subprocess mechanics (spawn, timeout/exit
mapping, metrics JSONL, raw-debug dump, temp-config cleanup, and the
``parse_json_output`` backstop) are inherited unchanged so failure-mode parity
is structural (Req 8).

This sub-task (4.1) implements **command construction, Invoker conformance,
system-prompt delivery, and ``.result`` extraction**:

* Command surface (Req 2): ``claude -p <user_message> --model <id>
  --output-format json``, ``[--effort <level>]``, ``[--json-schema <file>]``,
  built from the **public** Claude Code CLI flag surface only — it depends on
  none of the enterprise-managed provider wrapper's surface (``--aws-profile``, Bedrock
  routing, ``--claude-help``). The ``claude`` binary path is configurable via
  ``CLAUDE_CLI`` (default ``claude``), mirroring ``KIRO_CLI`` (Req 2.7), and
  auth/provider routing is left to out-of-band environment (Req 2.8).
* System-prompt delivery (Req 3): natively via ``--system-prompt-file``
  (preferred) or ``--append-system-prompt``, with a prepend-to-user-message
  fallback. Selectable at construction via ``system_prompt_delivery``.
* Final-text extraction (Req 2.3): the envelope ``.result`` field, then parsed
  with the shared ``parse_json_output`` backstop (Req 9.1).

This sub-task (4.2) implements **MCP attach, fail-loud, and tool-less
enforcement** on top of 4.1's command surface, via the extension points
:meth:`_tool_flags`, :meth:`_run_probe`, and :meth:`_envelope_from`:

* tools=True (Explorer/Thinker — Req 4): emit ``--mcp-config <temp json>``
  ``--strict-mcp-config``, where the temp JSON declares the Second Brain MCP
  server (``python -m src.mcp_server`` with ``cwd`` = repo root, mirroring
  :data:`src.backends.kiro._SECOND_BRAIN_MCP`). The
  :class:`~src.backends.mcp_probe.MCPStartupProbe` instruction is folded into
  the agentic turn so the model issues one trivial tool call, then
  :meth:`_run_probe` confirms a tool *result* actually returned — raising
  :class:`RuntimeError` on probe failure or an ``is_error`` envelope, never
  trusting process-attach alone (Req 5).
* tools=False (evaluators/Express — Req 6): pass ``--strict-mcp-config``
  **without** ``--mcp-config`` (so no MCP servers load at all) plus ``--tools
  ""`` (built-in tools off). ``--tools ""`` alone is insufficient — per the
  public Claude Code reference it disables only built-in tools, not MCP tools —
  so the ``--strict-mcp-config``-without-``--mcp-config`` form is what
  guarantees no MCP tools load. The probe is skipped (Req 5.5).

Real-usage capture (Req 7) and the full ``is_error``/failure-mode mapping
(Req 8) are completed in sub-task 4.3 via the :meth:`_extract_usage` override
(tolerate-and-warn: real ``usage``/``total_cost_usd`` → ``usage_source="real"``;
a parseable turn with no ``usage`` → ``usage=None``/``"estimate"`` + a loud
warning, never a raise). The failure-mode rows are mapped across the base and
this adapter: timeout → :class:`TimeoutError` and non-zero exit →
:class:`RuntimeError` (base :class:`~src.backends.agentic_cli.AgenticCliInvoker`),
``is_error`` → :class:`RuntimeError` (:meth:`_run_probe`), and unrecoverable
JSON → :class:`ValueError` (the shared ``parse_json_output`` backstop). See
``docs/MODEL-BACKENDS.md`` and the design sections "2. ClaudeCodeInvoker" and
"4. The MCP_Startup_Probe".

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8,
3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 5.2, 5.3, 5.5, 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4,
8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 9.3, 21.1
"""

from __future__ import annotations

import json
import logging
import os
import subprocess  # noqa: F401 — kept so tests can patch src.backends.claude_code.subprocess.run
import sys
import tempfile
from typing import Optional

from src.backends.agentic_cli import AgenticCliInvoker
from src.backends.base import parse_json_output as _parse_json_output
from src.backends.mcp_probe import MCPStartupProbe

logger = logging.getLogger(__name__)

# Configurable binary path, mirroring KiroInvoker's KIRO_CLI (Req 2.7). Defaults
# to the bare ``claude`` on PATH so the adapter runs against a public Claude Code
# install or the enterprise-managed wrapper without code changes.
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")

# How the system prompt is delivered to the CLI. "file" (preferred) writes the
# prompt to a temp file and passes --system-prompt-file; "append" passes it
# inline via --append-system-prompt; "prepend" is the no-native-support fallback
# that folds it into the user message (Req 3.3).
_VALID_DELIVERY = ("file", "append", "prepend")

# Repo root for the MCP server's cwd — dirname twice from src/backends/*.py,
# identical to KiroInvoker's computation so both adapters launch the same
# ``python -m src.mcp_server`` from the same working directory (Req 4.2). Repo
# root is dirname THREE times from src/backends/claude_code.py.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Second Brain MCP server entry for Claude Code's ``--mcp-config`` file. Mirrors
# src.backends.kiro._SECOND_BRAIN_MCP: launch the server with the *current*
# interpreter (sys.executable) so it runs inside the same venv, ``python -m
# src.mcp_server`` with cwd = repo root (Req 4.1, 4.2). Claude Code's
# --mcp-config consumes the standard ``{"mcpServers": {...}}`` shape.
_SECOND_BRAIN_MCP = {
    "second-brain": {
        "command": sys.executable,
        "args": ["-m", "src.mcp_server"],
        "cwd": _REPO_ROOT,
    }
}

__all__ = ["ClaudeCodeInvoker", "CLAUDE_CLI"]


class ClaudeCodeInvoker(AgenticCliInvoker):
    """Invoke ``claude -p`` as isolated non-interactive subprocesses.

    The Claude Code backend adapter — a metered, MCP-capable agentic CLI that
    reports real token usage and offers native structured output. Implements the
    :class:`src.backends.base.Invoker` contract via
    :class:`src.backends.agentic_cli.AgenticCliInvoker`.

    Args:
        model: The Claude model id (required; metered backend, no default).
        system_prompt_delivery: One of ``"file"`` (default, ``--system-prompt-file``),
            ``"append"`` (``--append-system-prompt``), or ``"prepend"`` (fold the
            system prompt into the user message — the fallback for Req 3.3).
    """

    BACKEND_NAME = "claude_code"
    DEFAULT_MODEL = ""

    # JSON recovery backstop is shared from base.py; kept as an explicit entry
    # point for symmetry with KiroInvoker.
    parse_json_output = staticmethod(_parse_json_output)

    def __init__(self, model: str | None = None, *, system_prompt_delivery: str = "file"):
        super().__init__(model)
        # Metered backend: a blank model id would silently bill an unintended
        # default, so reject it at construction (design "Error Handling").
        if not self.model:
            raise ValueError(
                "ClaudeCodeInvoker requires a non-empty model id "
                "(e.g. ClaudeCodeInvoker(model='claude-sonnet-4'))."
            )
        if system_prompt_delivery not in _VALID_DELIVERY:
            raise ValueError(
                f"system_prompt_delivery must be one of {_VALID_DELIVERY}, "
                f"got {system_prompt_delivery!r}."
            )
        self._system_prompt_delivery = system_prompt_delivery
        # Temp ``--mcp-config`` files written this process, drained in invoke's
        # finally. The base only cleans the single system-prompt config_path (and
        # only when it is non-None), so the MCP config — which exists for any
        # delivery mode on a tools=True call — is tracked and cleaned here.
        self._pending_mcp_configs: list[str] = []

    def invoke(self, *args, **kwargs) -> dict:
        """Run one ``claude -p`` turn, guaranteeing temp ``--mcp-config`` cleanup.

        Thin wrapper over :meth:`AgenticCliInvoker.invoke` that drains any MCP
        config temp file in a ``finally`` regardless of system-prompt delivery
        mode (the base cleans only the system-prompt file). Invocations are
        sequential per cached invoker, so the pending list is per-call in
        practice; it is cleared on entry to be safe against a prior partial run.
        """
        self._pending_mcp_configs.clear()
        try:
            return super().invoke(*args, **kwargs)
        finally:
            self._cleanup_mcp_configs()

    # --- Claude-Code-specific hooks ----------------------------------------
    def _build_config(self, name: str, system_prompt: str, needs_tools: bool):
        """Write the system prompt to a temp file only for native-file delivery.

        Returning the prompt string makes the base write it (via
        :meth:`_write_config`) and pass its path to :meth:`_build_command` as
        ``config_path``; the ``"append"`` / ``"prepend"`` modes need no temp
        file and return ``None``.
        """
        if self._system_prompt_delivery == "file":
            return system_prompt
        return None

    def _config_path(self, name: str) -> str:
        return os.path.join(tempfile.gettempdir(), f"{name}_system_prompt.txt")

    def _write_config(self, path: str, config) -> None:
        """Write the system-prompt temp file as raw text (not JSON)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(config)

    def _build_command(
        self,
        *,
        name,
        config_path,
        system_prompt,
        user_message,
        needs_tools,
        effort,
        json_schema: str | None = None,
        **_adapter_opts,
    ) -> list:
        """Construct the ``claude -p`` argv from the public flag surface (Req 2).

        ``claude -p <prompt_input> --model <id> --output-format stream-json --verbose``
        ``[--system-prompt-file <f> | --append-system-prompt <s>]``
        ``[--effort <level>]`` ``[--json-schema <file>]`` ``[<tool flags>]``.
        """
        prompt_input = self._prompt_input(system_prompt, user_message, needs_tools)
        cmd = [
            CLAUDE_CLI, "-p", prompt_input,
            "--model", self.model,
            "--output-format", "stream-json", "--verbose",
        ]
        if self._system_prompt_delivery == "file":
            cmd += ["--system-prompt-file", config_path]
        elif self._system_prompt_delivery == "append":
            cmd += ["--append-system-prompt", system_prompt]
        # "prepend": already folded into prompt_input; no native flag.

        if effort:
            # Provenance only — level names are NOT assumed equivalent across
            # backends (Req 21.1/21.3); the metrics line records it as-is.
            cmd += ["--effort", effort]

        # Native structured output when a stage requests it (Req 2.4); the
        # shared parse_json_output stays the backstop (Req 9.2).
        if json_schema:
            cmd += ["--json-schema", json_schema]

        cmd += self._tool_flags(name=name, needs_tools=needs_tools)
        return cmd

    def _prompt_input(self, system_prompt: str, user_message: str, needs_tools: bool = False) -> str:
        """The ``-p`` prompt argument.

        On tool-using calls the :class:`~src.backends.mcp_probe.MCPStartupProbe`
        instruction is prepended so the stage opens with one trivial, read-only
        tool call — the tool *result* that comes back is what :meth:`_run_probe`
        later confirms (process-attach alone is insufficient — Req 5.4). The
        system prompt is folded in only for the ``"prepend"`` fallback (Req 3.3);
        otherwise the user message is kept verbatim and distinct from the system
        prompt (Req 3.2).
        """
        parts: list[str] = []
        if needs_tools:
            parts.append(MCPStartupProbe.instruction())
        if self._system_prompt_delivery == "prepend" and system_prompt:
            parts.append(system_prompt)
        parts.append(user_message)
        return "\n\n".join(parts)

    def _tool_flags(self, *, name: str, needs_tools: bool) -> list:
        """MCP-attach / tool-less flags (Req 4, 6).

        tools=True: ``--mcp-config <temp json>`` ``--strict-mcp-config`` — the
        temp JSON declares the Second Brain MCP server (``python -m
        src.mcp_server``, cwd = repo root) and ``--strict-mcp-config`` ignores
        any ambient user/project MCP config so only ours loads.

        tools=False: ``--strict-mcp-config`` WITHOUT ``--mcp-config`` (no MCP
        servers load at all) plus ``--tools ""`` (built-in tools off). ``--tools
        ""`` alone is insufficient — it disables only built-in tools, not MCP
        tools — so the no-``--mcp-config`` form is what guarantees tool-less
        operation (Req 6.2, 6.3).
        """
        if not needs_tools:
            return ["--strict-mcp-config", "--tools", ""]
        config_path = self._write_mcp_config(name)
        return ["--mcp-config", config_path, "--strict-mcp-config"]

    def _mcp_config_path(self, name: str) -> str:
        """Filesystem path for the temp ``--mcp-config`` JSON file."""
        return os.path.join(tempfile.gettempdir(), f"{name}_mcp_config.json")

    def _write_mcp_config(self, name: str) -> str:
        """Write the Second Brain MCP server config and track it for cleanup.

        Claude Code's ``--mcp-config`` consumes the standard ``{"mcpServers":
        {...}}`` shape; the server entry launches ``python -m src.mcp_server``
        with cwd = repo root (Req 4.1, 4.2). The path is appended to
        :attr:`_pending_mcp_configs` so :meth:`invoke`'s ``finally`` removes it
        regardless of system-prompt delivery mode.
        """
        path = self._mcp_config_path(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": _SECOND_BRAIN_MCP}, f)
        self._pending_mcp_configs.append(path)
        return path

    def _cleanup_mcp_configs(self) -> None:
        """Remove every temp ``--mcp-config`` file written this call. Never raises."""
        while self._pending_mcp_configs:
            path = self._pending_mcp_configs.pop()
            try:
                os.remove(path)
            except OSError:
                pass

    def _run_probe(self, *, name, needs_tools, result, parsed, raw) -> None:
        """Fail-loud MCP enforcement for tool-using calls (Req 5, 8.3).

        Raises :class:`RuntimeError` when the envelope reports ``is_error`` (an
        infrastructure failure the orchestrator must never read as a verdict),
        or — on ``tools=True`` — when the
        :class:`~src.backends.mcp_probe.MCPStartupProbe` cannot confirm a tool
        *result* actually returned (the tools did not attach or are
        unreachable). The probe is skipped entirely when ``needs_tools`` is
        ``False`` (Req 5.5) and is never cached: it inspects only *this*
        subprocess's fresh output.

        The probe scans the full ``stream-json`` event list and raw stdout (not
        the narrowed ``.result`` payload, nor the terminal result event) so a
        completed tool-use/tool-result block in the transcript is visible. The
        ``tool_result`` blocks live in the assistant/user message events, **not**
        in the terminal ``{"type":"result"}`` event, so the event list — not the
        envelope — is what ``detect_tool_result`` must recurse into; the terminal
        ``result`` event is correctly ignored (its type is not a ``tool_result``
        marker).
        """
        envelope = self._envelope_from(result)
        if isinstance(envelope, dict) and envelope.get("is_error") is True:
            raise RuntimeError(
                f"Claude Code envelope reported is_error for backend "
                f"{self.BACKEND_NAME!r}: the CLI signalled a failed turn "
                "(infrastructure failure, not a model verdict)."
            )
        MCPStartupProbe.run(
            backend=self.BACKEND_NAME,
            needs_tools=needs_tools,
            events=self._events_from(result.stdout or ""),
            raw=result.stdout or "",
        )

    def _extract_usage(self, *, parsed, raw, result, metrics):
        """Capture real token usage, tolerating-and-warning when absent (Req 7).

        On a successful turn whose envelope carries a ``usage`` object, populate
        ``usage`` from the envelope's ``usage`` fields plus ``total_cost_usd``
        and report ``usage_source="real"`` (Req 7.1) — Claude Code is a metered
        backend that reports real counts, unlike Kiro's char/4 estimate. Real
        counts are also folded into the metrics line so cross-backend cost
        analysis never silently mixes estimates with measurements.

        On a successful, parseable turn whose envelope omits ``usage`` (a
        metering gap, not an infrastructure failure), keep ``output``/``raw``,
        return ``usage=None`` / ``usage_source="estimate"`` (the char/4 fallback
        KiroInvoker uses), and emit a loud warning — but **never raise, discard
        the result, or route into the failure/retry/abort path** (Req 7.2, 7.3).
        That cardinal split (telemetry gap ≠ infrastructure failure) is what
        preserves the failure-mode parity of Req 8.
        """
        envelope = self._envelope_from(result)
        if isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict):
            usage = dict(envelope["usage"])
            cost = envelope.get("total_cost_usd")
            if cost is not None:
                usage["total_cost_usd"] = cost
            # Record real counts in the metrics line (design: the JSONL writer is
            # extended to carry real token counts when the backend reports them).
            in_tok = usage.get("input_tokens")
            out_tok = usage.get("output_tokens")
            if in_tok is not None:
                metrics["real_input_tokens"] = in_tok
            if out_tok is not None:
                metrics["real_output_tokens"] = out_tok
            if cost is not None:
                metrics["total_cost_usd"] = cost
            return usage, "real"

        # Tolerate-and-warn: a parseable payload reached this point (the base
        # already raised ValueError if no JSON was recoverable), so the turn
        # succeeded — only the telemetry is missing.
        logger.warning(
            "ClaudeCodeInvoker: backend %r is metered and real token usage was "
            "expected, but the response envelope contained no 'usage' field. "
            "Preserving the result and falling back to usage=None / "
            "usage_source='estimate' (char/4). This is a metering gap, not an "
            "infrastructure failure — not raising.",
            self.BACKEND_NAME,
        )
        return None, "estimate"

    def _extract_raw(self, result) -> str:
        """Final text = the terminal result event's ``.result`` field (Req 2.3, 2.4).

        Locates the terminal ``{"type":"result"}`` event of the ``stream-json
        --verbose`` stream (via :meth:`_envelope_from`) and returns its
        ``result`` value (a ``str`` as-is; otherwise ``json.dumps``).

        When there is **no** result event (a malformed/truncated stream) or it
        carries no ``result``, return ``""`` — **not** full JSONL stdout — so the
        shared ``parse_json_output("")`` backstop raises ``ValueError`` (Req 9.3,
        "No result event" decision). Returning stdout would let the backstop
        recover an interior event (e.g. a long thinking/text block) as a bogus
        answer.

        When ``--json-schema`` was requested, Claude Code places the
        schema-validated payload in the result event's ``.result`` directly, so
        extracting ``.result`` here and parsing it via the shared
        ``parse_json_output`` backstop *prefers* the schema-validated payload and
        only falls back to the backstop's recovery heuristics for prose-wrapped
        output (Req 9.2).
        """
        envelope = self._envelope_from(result)
        if isinstance(envelope, dict) and "result" in envelope:
            res = envelope["result"]
            return res if isinstance(res, str) else json.dumps(res)
        return ""

    def _envelope_from(self, result):
        """Return the terminal ``{"type":"result"}`` event of the stream, or ``None``.

        With ``--output-format stream-json --verbose`` the CLI emits a JSONL
        event stream; the one object the rest of the adapter reasons over is the
        terminal ``{"type":"result", …}`` event, which carries ``.result`` /
        ``is_error`` / ``usage``. Parse the stream via :meth:`_events_from` and
        return the **last** event whose ``type == "result"``; return ``None`` when
        no result event exists (a malformed/truncated stream). The shared
        ``parse_json_output`` backstop is deliberately NOT run over full stdout —
        that would recover an interior event as a bogus answer.

        Shared by :meth:`_extract_raw`, usage capture, and ``is_error`` mapping,
        so the envelope is recovered one consistent way.
        """
        events = self._events_from(result.stdout or "")
        if not events:
            return None
        terminal = None
        for event in events:
            if isinstance(event, dict) and event.get("type") == "result":
                terminal = event
        return terminal

    @staticmethod
    def _events_from(stdout: str) -> Optional[list]:
        """Parse the ``stream-json`` event stream (one JSON object per line) into a list.

        Non-JSON lines are skipped. Returns ``None`` when nothing parses, so
        callers can fall back appropriately (mirrors
        :meth:`src.backends.codex.CodexInvoker._events_from`).
        """
        events: list = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return events or None
