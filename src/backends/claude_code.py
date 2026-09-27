"""Claude Code subprocess adapter with isolated read-only MCP discovery tools.

System prompts use native file or append delivery. Tool calls load only the
read-only Second Brain MCP server, disable all built-in tools and set database
transactions read-only. Metered response failures preserve provider usage."""

from __future__ import annotations

import json
import logging
import re
import os
import subprocess  # noqa: F401 — kept so tests can patch src.backends.claude_code.subprocess.run
import sys
import tempfile
from typing import Optional

from src.backends.agentic_cli import AgenticCliInvoker
from src.backends.base import (write_private_text, parse_json_output as _parse_json_output,
                               corpus_role_for_stage, assert_corpus_route, CorpusRoutingError)
from src.backends.mcp_probe import MCPStartupProbe

logger = logging.getLogger(__name__)

# Configurable binary path, mirroring KiroInvoker's KIRO_CLI (Req 2.7). Defaults
# to the bare ``claude`` on PATH so the adapter runs against a public Claude Code
# install or the ASBX wrapper without code changes.
# Absolute default: under launchd PATH lacks ~/.toolbox/bin, and a bare "claude"
# raised FileNotFoundError at 02:00 (2026-09-17). Same convention as KIRO_CLI.
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", os.path.expanduser("~/.toolbox/bin/claude"))

# How the system prompt is delivered to the CLI. "file" (preferred) writes the
# prompt to a temp file and passes --system-prompt-file; "append" passes it
# inline via --append-system-prompt.
_VALID_DELIVERY = ("file", "append")

# Resolve the repository root independently of the process working directory.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Dedicated read-only server with database writes disabled as an additional
# boundary. Strict MCP configuration excludes ambient user/project servers.
_SECOND_BRAIN_MCP = {
    "second-brain": {
        "command": sys.executable,
        "args": ["-m", "src.mcp_readonly"],
        "env": {"PGOPTIONS": "-c default_transaction_read_only=on"},
        "cwd": _REPO_ROOT,
    }
}

# Print mode cannot ask a person to approve tools. Grant only the explicit
# read-only server endpoints, never a wildcard or a permission bypass mode.
_READONLY_MCP_TOOLS = (
    "mcp__second-brain__memory_search",
    "mcp__second-brain__memory_read",
    "mcp__second-brain__memory_list",
    "mcp__second-brain__memory_recent_activity",
    "mcp__second-brain__memory_graph",
)

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
            or ``"append"`` (``--append-system-prompt``).
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
        ``config_path``; the inline ``"append"`` mode returns ``None``.
        """
        if self._system_prompt_delivery == "file":
            return system_prompt
        return None

    def _config_path(self, name: str) -> str:
        return os.path.join(tempfile.gettempdir(), f"{name}_system_prompt.txt")

    def _write_config(self, path: str, config) -> None:
        """Write the system-prompt temp file as raw text (not JSON)."""
        write_private_text(path, config)

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
            "--model", self._cli_model_id(),
            "--output-format", "stream-json", "--verbose",
            "--bare", "--no-session-persistence",
            "--disable-slash-commands", "--setting-sources", "",
        ]
        if self._system_prompt_delivery == "file":
            cmd += ["--system-prompt-file", config_path]
        elif self._system_prompt_delivery == "append":
            cmd += ["--append-system-prompt", system_prompt]

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

    def _cli_model_id(self) -> str:
        """Use the canonical version ID for Claude's model allowlist/mapping.

        The ASBX wrapper injects modelOverrides and availableModels. Claude
        evaluates that allowlist against canonical IDs, before resolving the
        configured Bedrock profile. Supplying the profile ID itself can fall
        back to the default model. Only remove recognized provider wrapping;
        preserve the exact family/version and the original provenance model.
        Custom inference-profile ARNs and aliases remain explicit and unchanged.
        """
        if re.fullmatch(r"(?:(?:global|us|eu|apac)\.)?anthropic\.claude-[a-z0-9-]+(?:-v\d+:\d+)?", self.model):
            return self._canonical_model_id(self.model)
        return self.model

    def _prompt_input(self, system_prompt: str, user_message: str, needs_tools: bool = False) -> str:
        """The ``-p`` prompt argument.

        On tool-using calls the :class:`~src.backends.mcp_probe.MCPStartupProbe`
        instruction is prepended so the stage opens with one trivial, read-only
        tool call — the tool *result* that comes back is what :meth:`_run_probe`
        later confirms. The system prompt uses its own native delivery flag.
        """
        parts: list[str] = []
        if needs_tools:
            parts.append(MCPStartupProbe.instruction())
        parts.append(user_message)
        return "\n\n".join(parts)

    def _tool_flags(self, *, name: str, needs_tools: bool) -> list:
        """MCP-attach / tool-less flags (Req 4, 6).

        tools=True: ``--mcp-config <temp json>`` ``--strict-mcp-config`` — the
        temp JSON declares the Second Brain MCP server (``python -m
        src.mcp_readonly``, cwd = repo root) and ``--strict-mcp-config`` ignores
        any ambient user/project MCP config so only ours loads. The five exact
        read-only endpoint names receive ``--allowedTools`` grants so print
        mode can execute them without a person answering a permission prompt.

        tools=False: ``--strict-mcp-config`` WITHOUT ``--mcp-config`` (no MCP
        servers load at all) plus ``--tools ""`` (built-in tools off). ``--tools
        ""`` alone is insufficient — it disables only built-in tools, not MCP
        tools — so the no-``--mcp-config`` form is what guarantees tool-less
        operation (Req 6.2, 6.3).
        """
        if not needs_tools:
            return ["--strict-mcp-config", "--tools", ""]
        config_path = self._write_mcp_config(name)
        return ["--mcp-config", config_path, "--strict-mcp-config", "--tools", "",
                "--allowedTools", *_READONLY_MCP_TOOLS]

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
        write_private_text(path, json.dumps({"mcpServers": _SECOND_BRAIN_MCP}))
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

    @staticmethod
    def _canonical_model_id(model: str) -> str:
        """Only provider/profile wrapping aliases; never collapse model versions."""
        value = model.strip().lower()
        value = re.sub(r"^(?:global|us|eu|apac)\.", "", value)
        value = re.sub(r"^anthropic\.", "", value)
        return re.sub(r"-v\d+:\d+$", "", value)

    def _note_model_honored(self, result, metrics: dict) -> None:
        super()._note_model_honored(result, metrics)
        role = corpus_role_for_stage(metrics.get("stage"))
        if not role:
            return
        observed = set()
        for event in self._events_from(result.stdout or "") or []:
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("modelUsage"), dict):
                observed.update(m for m in event["modelUsage"] if isinstance(m, str) and m)
            message = event.get("message")
            if event.get("type") == "assistant" and isinstance(message, dict) and isinstance(message.get("model"), str):
                observed.add(message["model"])
        metrics["served_models"] = sorted(observed)
        metrics["model_honored"] = None if not observed else True
        if not observed:
            return  # Unreported is unknown; qualification must separately verify routing.
        try:
            for served in observed:
                assert_corpus_route(self.BACKEND_NAME, served, role)
                if self._canonical_model_id(served) != self._canonical_model_id(self.model):
                    raise CorpusRoutingError("Served corpus model differs from the requested model")
        except CorpusRoutingError as exc:
            metrics["model_honored"] = False
            metrics["error"] = "model_not_honored"
            if len(observed) == 1:
                exc.usage_model = metrics["usage_model"] = next(iter(observed))
            elif (self._envelope_from(result) or {}).get("total_cost_usd") is None:
                # Aggregate token counts cannot price a mixed-model fallback.
                exc.usage_pricing_unknown = True
            raise

    def _extract_usage(self, *, parsed, raw, result, metrics):
        """Preserve reported usage on every response, including failed turns.

        Missing usage is returned as unknown; the shared corpus metering boundary
        then stops the run before another call.
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

        return self._warn_missing_usage("missing usage object")

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
