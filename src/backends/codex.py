"""Codex subprocess adapter retained for future explicit qualification.

Configured corpus profiles do not select it. Corpus tool access is denied until
an enforceable read-only boundary is implemented. Native file/inline instruction
delivery and JSON event usage parsing remain available for synthetic tests."""

from __future__ import annotations

import json
import logging
import os
import subprocess  # noqa: F401 — kept so tests can patch src.backends.codex.subprocess.run
import sys
import tempfile
from typing import Any, Optional

from src.backends.agentic_cli import AgenticCliInvoker
from src.backends.base import write_private_text, parse_json_output as _parse_json_output
from src.backends.mcp_probe import MCPStartupProbe

logger = logging.getLogger(__name__)

# Configurable binary path, mirroring KiroInvoker's KIRO_CLI and
# ClaudeCodeInvoker's CLAUDE_CLI (Req 11 / portability). Defaults to the bare
# ``codex`` on PATH so the adapter runs against any Codex install without code
# changes; auth/provider routing is supplied out-of-band via environment.
CODEX_CLI = os.environ.get("CODEX_CLI", "codex")

# Repo root for the MCP server's cwd — dirname twice from src/backends/*.py,
# identical to ClaudeCodeInvoker's and KiroInvoker's computation so all three
# adapters launch the same ``python -m src.mcp_server`` from the same working
# directory (Req 13.2). Repo root is dirname THREE times from src/backends/codex.py.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Second Brain MCP server identity for Codex's ``mcp_servers.<name>`` config
# (Req 13.1, 13.2, 13.3). Mirrors src.backends.kiro._SECOND_BRAIN_MCP /
# claude_code._SECOND_BRAIN_MCP: launch the server with the *current* interpreter
# (sys.executable) so it runs inside the same venv, ``python -m src.mcp_server``
# with cwd = repo root, and ``required=true`` so Codex hard-fails at startup when
# the server can't start (the PRIMARY fail-loud guard — Req 14.4).
_MCP_SERVER_NAME = "second_brain"
_MCP_SERVER_COMMAND = sys.executable
_MCP_SERVER_ARGS = ["-m", "src.mcp_server"]

# How the system prompt is delivered to the CLI (Req 12.1, 12.3):
#   "model_instructions_file" (preferred) — write the prompt to a temp file and
#       pass ``-c model_instructions_file=<file>``; the file form sidesteps the
#       TOML/JSON value parsing that ``-c key=value`` applies to inline values,
#       so an arbitrary system prompt (multi-line, leading brace/number, etc.)
#       round-trips intact. This is the Codex analogue of Claude Code's
#       ``--system-prompt-file``.
#   "developer_instructions" / "instructions" — inline ``-c <key>=<prompt>``.
_VALID_DELIVERY = (
    "model_instructions_file",
    "developer_instructions",
    "instructions",
)

# Where the final assistant text comes from (Req 11.3):
#   "output-last-message" (preferred) — pass ``--output-last-message <file>`` and
#       read the final message back from that file.
#   "json" — pass ``--json`` and recover the last ``agent_message`` event from
#       the emitted event stream.
_VALID_FINAL_SOURCE = ("output-last-message", "json")

__all__ = ["CodexInvoker", "CODEX_CLI"]


class CodexInvoker(AgenticCliInvoker):
    """Invoke ``codex exec`` as isolated non-interactive subprocesses.

    The Codex backend adapter — a metered, MCP-capable agentic CLI that reports
    real token usage and offers native structured output. Implements the
    :class:`src.backends.base.Invoker` contract via
    :class:`src.backends.agentic_cli.AgenticCliInvoker`.

    Args:
        model: The Codex model id (required; metered backend, no default — a
            blank id would silently bill an unintended default).
        system_prompt_delivery: One of ``"model_instructions_file"`` (default,
            ``-c model_instructions_file=<temp file>``),
            ``"developer_instructions"`` / ``"instructions"`` (inline ``-c``
            config values).
        final_message_source: One of ``"output-last-message"`` (default,
            ``--output-last-message <temp file>``) or ``"json"`` (recover the
            final ``agent_message`` from the ``--json`` event stream).
    """

    BACKEND_NAME = "codex"
    DEFAULT_MODEL = ""

    # JSON recovery backstop is shared from base.py; kept as an explicit entry
    # point for symmetry with the other adapters.
    parse_json_output = staticmethod(_parse_json_output)

    def __init__(
        self,
        model: str | None = None,
        *,
        system_prompt_delivery: str = "model_instructions_file",
        final_message_source: str = "output-last-message",
    ):
        super().__init__(model)
        # Metered backend: a blank model id would silently bill an unintended
        # default, so reject it at construction (design "Error Handling").
        if not self.model:
            raise ValueError(
                "CodexInvoker requires a non-empty model id "
                "(e.g. CodexInvoker(model='gpt-5-codex'))."
            )
        if system_prompt_delivery not in _VALID_DELIVERY:
            raise ValueError(
                f"system_prompt_delivery must be one of {_VALID_DELIVERY}, "
                f"got {system_prompt_delivery!r}."
            )
        if final_message_source not in _VALID_FINAL_SOURCE:
            raise ValueError(
                f"final_message_source must be one of {_VALID_FINAL_SOURCE}, "
                f"got {final_message_source!r}."
            )
        self._system_prompt_delivery = system_prompt_delivery
        self._final_message_source = final_message_source
        # Temp files written this process (the ``--output-last-message`` file and,
        # in later sub-tasks, the MCP config), drained in invoke's ``finally``.
        # The base only cleans the single system-prompt config_path, so anything
        # else is tracked and cleaned here.
        self._pending_temp_files: list[str] = []
        # Path of the current call's ``--output-last-message`` file, set in
        # :meth:`_build_command` and read back in :meth:`_extract_raw`.
        self._pending_last_message: Optional[str] = None

    def invoke(self, *args, **kwargs) -> dict:
        """Run one ``codex exec`` turn, guaranteeing temp-file cleanup.

        Thin wrapper over :meth:`AgenticCliInvoker.invoke` that drains any temp
        artifacts (the ``--output-last-message`` file; MCP config in later
        sub-tasks) in a ``finally`` regardless of system-prompt delivery mode —
        the base cleans only the system-prompt file. Invocations are sequential
        per cached invoker, so the pending list is per-call in practice; it is
        cleared on entry to be safe against a prior partial run.
        """
        self._pending_temp_files = []
        self._pending_last_message = None
        try:
            return super().invoke(*args, **kwargs)
        finally:
            self._cleanup_temp_files()

    # --- system-prompt delivery (temp file for the preferred mode) ---------
    def _build_config(self, name: str, system_prompt: str, needs_tools: bool):
        """Write the system prompt to a temp file only for file-based delivery.

        Returning the prompt string makes the base write it (via
        :meth:`_write_config`) and pass its path to :meth:`_build_command` as
        ``config_path``; the inline modes need no temp file and
        return ``None``.
        """
        if self._system_prompt_delivery == "model_instructions_file":
            return system_prompt
        return None

    def _config_path(self, name: str) -> str:
        return os.path.join(tempfile.gettempdir(), f"{name}_instructions.txt")

    def _write_config(self, path: str, config) -> None:
        """Write the system-prompt temp file as raw text (not JSON)."""
        write_private_text(path, config)

    # --- command construction (Req 11) -------------------------------------
    def _build_command(
        self,
        *,
        name,
        config_path,
        system_prompt,
        user_message,
        needs_tools,
        effort,
        output_schema: str | None = None,
        **_adapter_opts,
    ) -> list:
        """Construct the ``codex exec`` argv from the documented surface (Req 11).

        ``codex exec <prompt_input> -m <id>``
        ``[-c model_instructions_file=<f> | -c developer_instructions=<s> | -c instructions=<s>]``
        ``[-c model_reasoning_effort=<level>]`` ``[--output-schema <file>]``
        ``[--output-last-message <file> | --json]`` ``[<tool/sandbox flags>]``.
        """
        prompt_input = self._prompt_input(system_prompt, user_message, needs_tools)
        cmd = [CODEX_CLI, "exec", prompt_input, "-m", self.model]

        # Native system-prompt delivery keeps instructions separate from input.
        cmd += self._system_prompt_args(config_path, system_prompt)

        # Reasoning effort as provenance only — level names are NOT assumed
        # equivalent across backends (Req 21.2/21.3); the metrics line records it
        # as-is.
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]

        # Native structured output when a stage requests it (Req 11.4); the
        # shared parse_json_output stays the backstop (Req 18.2).
        if output_schema:
            cmd += ["--output-schema", output_schema]

        cmd += self._final_text_args(name)
        cmd += self._tool_and_sandbox_args(name=name, needs_tools=needs_tools)
        return cmd

    def _prompt_input(
        self, system_prompt: str, user_message: str, needs_tools: bool = False
    ) -> str:
        """The positional prompt argument for ``codex exec``.

        On tool-using calls the
        :class:`~src.backends.mcp_probe.MCPStartupProbe` instruction is prepended
        so the stage opens with one trivial, read-only tool call — the tool
        *result* that comes back is what :meth:`_run_probe` later confirms as the
        SECONDARY sandbox-reachability check (``required=true`` is the PRIMARY
        startup guard; process-attach alone is insufficient — Req 14.3, 14.4).
        Native system instructions remain separate from the user message.
        """
        parts: list[str] = []
        if needs_tools:
            parts.append(MCPStartupProbe.instruction())
        parts.append(user_message)
        return "\n\n".join(parts)

    def _system_prompt_args(self, config_path: Optional[str], system_prompt: str) -> list:
        """Native system-prompt delivery flags (Req 12.1).

        ``model_instructions_file`` points at the temp file the base wrote;
        ``developer_instructions`` / ``instructions`` pass the prompt inline as a
        ``-c`` config value.
        """
        mode = self._system_prompt_delivery
        if mode == "model_instructions_file":
            return ["-c", f"model_instructions_file={config_path}"]
        if mode == "developer_instructions":
            return ["-c", f"developer_instructions={system_prompt}"]
        if mode == "instructions":
            return ["-c", f"instructions={system_prompt}"]
        raise ValueError("Unsupported native system-prompt delivery mode")

    def _final_text_args(self, name: str) -> list:
        """Final-text source flags (Req 11.3).

        ``output-last-message`` (preferred) writes the final assistant message to
        a temp file (tracked for cleanup) and reads it back in
        :meth:`_extract_raw`; ``json`` emits the event stream that
        :meth:`_extract_raw` scans for the final ``agent_message``.
        """
        if self._final_message_source == "output-last-message":
            path = self._last_message_path(name)
            write_private_text(path, "")
            self._pending_last_message = path
            self._pending_temp_files.append(path)
            return ["--output-last-message", path]
        return ["--json"]

    def _last_message_path(self, name: str) -> str:
        """Filesystem path for the temp ``--output-last-message`` file."""
        return os.path.join(tempfile.gettempdir(), f"{name}_last_message.txt")

    def _tool_and_sandbox_args(self, *, name: str, needs_tools: bool) -> list:
        """MCP-server + sandbox flags (Req 13, 15).

        tools=True (agentic Explorer/Thinker): attach the Second Brain MCP server
        and open the agentic sandbox so the server can reach Postgres/Bedrock —

        * ``-c mcp_servers.second_brain.command=<python>``
          ``-c mcp_servers.second_brain.args=["-m","src.mcp_server"]``
          ``-c mcp_servers.second_brain.cwd=<repo root>``
          ``-c mcp_servers.second_brain.required=true`` (Req 13.1/13.2/13.3) —
          ``required=true`` is the PRIMARY fail-loud guard: Codex hard-fails at
          startup (non-zero exit → :class:`RuntimeError` via the base) when the
          server can't start, before any model call (Req 14.4).
        * ``--sandbox workspace-write`` plus
          ``-c sandbox_workspace_write.network_access=true`` so the server's
          network/DB access is not silently blocked (Req 13.4).

        tools=False (evaluators/Express): emit **no** ``mcp_servers`` config (no
        MCP servers load at all) and run ``--sandbox read-only`` (Req 15.1,
        15.2). The probe is skipped in :meth:`_run_probe` (Req 15.3).

        Config values are JSON-encoded, which is valid TOML for the scalar/array
        shapes Codex's ``-c key=value`` parser accepts, so an absolute path with
        spaces or special characters round-trips intact.
        """
        if not needs_tools:
            return ["--sandbox", "read-only"]

        prefix = f"mcp_servers.{_MCP_SERVER_NAME}"
        return [
            "-c", f"{prefix}.command={json.dumps(_MCP_SERVER_COMMAND)}",
            "-c", f"{prefix}.args={json.dumps(_MCP_SERVER_ARGS)}",
            "-c", f"{prefix}.cwd={json.dumps(_REPO_ROOT)}",
            "-c", f"{prefix}.required=true",
            "--sandbox", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
        ]

    # --- usage capture (Req 16/17/18) -------------------------------------
    def _extract_usage(self, *, parsed, raw, result, metrics):
        """Preserve event usage; absent usage makes corpus metering stop the run."""
        usage = self._usage_from_events(self._events_from(result.stdout or ""))
        if usage is not None:
            # Record real counts in the metrics line (the shared JSONL writer is
            # extended to carry real token counts when the backend reports them).
            in_tok = usage.get("input_tokens")
            out_tok = usage.get("output_tokens")
            if in_tok is not None:
                metrics["real_input_tokens"] = in_tok
            if out_tok is not None:
                metrics["real_output_tokens"] = out_tok
            cost = usage.get("total_cost_usd")
            if cost is not None:
                metrics["total_cost_usd"] = cost
            return usage, "real"

        return self._warn_missing_usage("missing usage events")

    def _usage_from_events(self, events: Optional[list]) -> Optional[dict]:
        """Return the real token-usage dict from a ``--json`` event list, or ``None``.

        Scans the parsed event stream for a usage-bearing event and returns the
        *last* one seen (Codex emits cumulative ``token_count`` events; the final
        carries the turn total). Returns ``None`` when no usage event is present
        — the tolerate-and-warn signal.
        """
        if not events:
            return None
        found: Optional[dict] = None
        for obj in events:
            usage = self._usage_from_event(obj)
            if usage is not None:
                found = usage
        return found

    @classmethod
    def _usage_from_event(cls, obj) -> Optional[dict]:
        """Extract a token-usage dict from one Codex ``--json`` event, or ``None``.

        Tolerates the documented usage-event shapes: a ``turn.completed`` event
        carrying a ``usage`` object, a ``token_count`` event carrying
        ``info.total_token_usage`` (or token fields directly on ``info``), and
        the ``{"msg": {...}}`` / ``{"item": {...}}`` wrapper variants. ``usage``
        copies the matched dict and folds in ``total_cost_usd`` when the event
        reports it.
        """
        if not isinstance(obj, dict):
            return None
        for scope in (obj, obj.get("msg"), obj.get("item")):
            if not isinstance(scope, dict):
                continue
            # turn.completed (or any event) carrying an explicit usage object.
            u = scope.get("usage")
            if cls._is_usage_dict(u):
                return cls._build_usage(scope, u)
            # token_count event carrying an info wrapper.
            info = scope.get("info")
            if isinstance(info, dict):
                total = info.get("total_token_usage")
                if cls._is_usage_dict(total):
                    return cls._build_usage(scope, total)
                if cls._is_usage_dict(info):
                    return cls._build_usage(scope, info)
        return None

    @staticmethod
    def _is_usage_dict(d) -> bool:
        """True when ``d`` looks like a token-usage payload (has token counts)."""
        return isinstance(d, dict) and (
            "input_tokens" in d or "output_tokens" in d
        )

    @staticmethod
    def _build_usage(scope: dict, usage: dict) -> dict:
        """Copy the matched usage dict, folding in ``total_cost_usd`` if present."""
        out = dict(usage)
        cost = scope.get("total_cost_usd")
        if cost is not None:
            out["total_cost_usd"] = cost
        return out

    def _run_probe(self, *, name, needs_tools, result, parsed, raw) -> None:
        """Fail-loud SECONDARY MCP reachability check for tool-using calls.

        ``required=true`` (see :meth:`_tool_and_sandbox_args`) is the PRIMARY
        guard — it makes Codex hard-fail at startup when the server can't start,
        surfacing as a non-zero exit → :class:`RuntimeError` in the base. The
        probe is the SECONDARY check for the failure ``required=true`` cannot
        catch: the server *starts* but the sandbox blocks its network/database
        access, so it attached yet is unreachable. The probe confirms a tool
        *result* actually came back through the ``--json`` event stream / raw CLI
        output (process-attach alone is insufficient — Req 14.2, 14.3).

        Skipped entirely when ``needs_tools`` is ``False`` (Req 15.3) and never
        cached: it inspects only *this* subprocess's fresh output (Req 14.5).
        """
        stdout = result.stdout or ""
        MCPStartupProbe.run(
            backend=self.BACKEND_NAME,
            needs_tools=needs_tools,
            events=self._events_from(stdout),
            raw=stdout,
        )

    @staticmethod
    def _events_from(stdout: str) -> Optional[list]:
        """Parse a ``--json`` event stream (one JSON object per line) into a list.

        Non-JSON lines (human-readable progress in the default ``codex exec``
        output) are skipped. Returns ``None`` when nothing parses, so the probe
        falls back to scanning the raw text.
        """
        events: list[Any] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return events or None

    # --- final-text extraction (Req 11.3) ----------------------------------
    def _extract_raw(self, result) -> str:
        """Final text from the ``--output-last-message`` file or ``--json`` events.

        For ``output-last-message``: read the temp file Codex wrote; fall back to
        stdout if it is missing or empty. For ``json``: recover the last
        ``agent_message`` event from the stream; fall back to raw stdout. In both
        cases the shared ``parse_json_output`` backstop then runs on the returned
        text, and an unrecoverable payload surfaces as :class:`ValueError`
        (Req 18.3).
        """
        if self._final_message_source == "output-last-message":
            path = self._pending_last_message
            if path and os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    text = ""
                if text.strip():
                    return text
            return result.stdout or ""

        # json event stream
        text = self._final_text_from_events(result.stdout or "")
        return text if text is not None else (result.stdout or "")

    def _final_text_from_events(self, stdout: str) -> Optional[str]:
        """Recover the final ``agent_message`` text from a ``--json`` event stream.

        The stream is one JSON object per line; non-JSON lines (prose, progress
        markers) are skipped. Returns the *last* agent message seen (the final
        assistant turn), or ``None`` when no agent message is present.
        """
        final: Optional[str] = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            text = self._agent_message_text(obj)
            if text is not None:
                final = text
        return final

    @staticmethod
    def _agent_message_text(obj) -> Optional[str]:
        """Extract agent-message text from one Codex ``--json`` event, or ``None``.

        Tolerates the documented event shapes: the newer
        ``{"type": "item.completed", "item": {"type": "agent_message", ...}}``,
        the ``{"msg": {"type": "agent_message", ...}}`` wrapper, and a bare
        ``{"type": "agent_message", ...}``. The text lives in ``text`` or
        ``message`` depending on the shape.
        """
        if not isinstance(obj, dict):
            return None

        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            t = item.get("text") or item.get("message")
            if isinstance(t, str):
                return t

        msg = obj.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message":
            t = msg.get("message") or msg.get("text")
            if isinstance(t, str):
                return t

        if obj.get("type") == "agent_message":
            t = obj.get("message") or obj.get("text")
            if isinstance(t, str):
                return t
        return None

    # --- temp-file cleanup --------------------------------------------------
    def _cleanup_temp_files(self) -> None:
        """Remove every temp file written this call. Never raises."""
        while self._pending_temp_files:
            path = self._pending_temp_files.pop()
            try:
                os.remove(path)
            except OSError:
                pass
