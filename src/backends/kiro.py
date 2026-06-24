"""Kiro backend adapter — kiro-cli chat subprocesses.

Handles agent config creation, process spawning, timeout enforcement,
output parsing, and error handling. Used by the DreamCycleOrchestrator
for all agent invocations on the laptop (all-Kiro) profile.

Each invocation writes a temporary agent config to ~/.kiro/agents/ with
the system prompt in the "prompt" field and MCP servers (if needed) in
the "mcpServers" field, then invokes kiro-cli chat --no-interactive.

The shared subprocess mechanics (spawn, timeout/exit mapping, the per-call
metrics JSONL writer, raw-output debug dump, temp-config cleanup, and the JSON
recovery backstop) live in :class:`src.backends.agentic_cli.AgenticCliInvoker`.
``KiroInvoker`` overrides only the Kiro-specific hooks and keeps its exact
command surface (``--no-interactive --agent --model``, ``--trust-all-tools
--require-mcp-startup``, the MCP-startup exit-3 retry, and
``usage=None``/``usage_source="estimate"``). See ``docs/MODEL-BACKENDS.md``.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 20.1, 20.2, 20.3
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: F401 — kept so tests can patch src.backends.kiro.subprocess.run
import sys
import time  # noqa: F401 — kept so tests can patch src.backends.kiro.time.sleep
import uuid

from src.backends.agentic_cli import (
    AgenticCliInvoker,
    _write_metrics_line,
)
from src.backends.base import parse_json_output as _parse_json_output

logger = logging.getLogger(__name__)

KIRO_CLI = os.environ.get("KIRO_CLI", os.path.expanduser("~/.local/bin/kiro-cli"))
AGENTS_DIR = os.path.expanduser("~/.kiro/agents")
DEFAULT_MODEL = "claude-opus-4.8"

# --- Phase 0 LLM-call instrumentation (see docs/FABLE5-THINKER-PLAN.md) ---
# METRICS_DIR stays defined here (not just re-exported) so the test-suite and
# launchd env keep monkeypatching/overriding src.backends.kiro.METRICS_DIR, and
# KiroInvoker's _metrics_dir() reads this module global at call time. Repo root
# is dirname THREE times from src/backends/kiro.py (the file sits one level deeper
# than the original src/agent_invoker.py, which used two).
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
METRICS_DIR = os.environ.get(
    "KIRO_LLM_METRICS_DIR", os.path.join(_REPO_ROOT, "logs", "llm_metrics")
)


def _record_call_metrics(metrics: dict, run_id: str | None) -> None:
    """Append one JSONL record of an LLM call's metrics to this module's
    METRICS_DIR. Never raises. Kept here (reading the module-global METRICS_DIR
    at call time) so monkeypatching ``src.backends.kiro.METRICS_DIR`` works and
    the agent_invoker shim can re-export it unchanged."""
    _write_metrics_line(metrics, run_id, METRICS_DIR)


# Re-exported for back-compat: the agent_invoker shim and the test-suite import
# these from src.backends.kiro.
__all__ = [
    "KiroInvoker",
    "KIRO_CLI",
    "AGENTS_DIR",
    "DEFAULT_MODEL",
    "METRICS_DIR",
    "_SECOND_BRAIN_MCP",
    "_record_call_metrics",
]

# MCP server config for the Second Brain — Explorer and Thinker need this
# to query memories via memory_search, memory_read, etc.
_SECOND_BRAIN_MCP = {
    "second-brain": {
        "command": sys.executable,
        "args": ["-m", "src.mcp_server"],
        "cwd": _REPO_ROOT,
    }
}


class KiroInvoker(AgenticCliInvoker):
    """Invoke ``kiro-cli chat --no-interactive`` as isolated subprocesses.

    The Kiro backend adapter — routes to Amazon Q ($0 metered). Implements the
    :class:`src.backends.base.Invoker` contract via
    :class:`src.backends.agentic_cli.AgenticCliInvoker`; returns ``usage=None``
    because kiro-cli exposes no real token usage (only char/4 estimates in the
    metrics log). See ``docs/MODEL-BACKENDS.md``.
    """

    BACKEND_NAME = "kiro"
    DEFAULT_MODEL = DEFAULT_MODEL

    # JSON recovery backstop is shared from base.py; kept as an explicit entry
    # point because the agent_invoker shim re-exports it as
    # AgentInvoker.parse_json_output, which the test-suite and older callers use.
    parse_json_output = staticmethod(_parse_json_output)

    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        tools: bool = False,
        timeout: int = 300,
        effort: str | None = None,
        stage: str | None = None,
        run_id: str | None = None,
        mcp_startup_retries: int = 2,
        mcp_startup_backoff: float = 3.0,
    ) -> dict:
        """Invoke kiro-cli chat --no-interactive as a subprocess.

        Creates a temporary agent config with the system prompt, invokes
        kiro-cli chat, and parses the JSON output.

        Args:
            system_prompt: The agent's role prompt (written to agent config).
            user_message: Input data passed as the chat input argument.
            tools: When True, the Second Brain MCP server is included in the
                agent config so the agent can use memory tools. Evaluators
                pass False (no tool access).
            timeout: Max seconds before killing the process (default 300).
            mcp_startup_retries: Extra attempts when MCP startup fails (exit 3),
                used only for tool-using calls.
            mcp_startup_backoff: Initial backoff (seconds) between MCP retries.

        Returns:
            dict with 'output' (parsed JSON), 'raw' (raw stdout text),
            'usage' (None — kiro-cli reports no real usage), and
            'usage_source' ("estimate").

        Raises:
            TimeoutError: If the subprocess exceeds the timeout.
            RuntimeError: If the subprocess exits with a non-zero code.
        """
        return super().invoke(
            system_prompt,
            user_message,
            tools=tools,
            timeout=timeout,
            effort=effort,
            stage=stage,
            run_id=run_id,
            mcp_startup_retries=mcp_startup_retries,
            mcp_startup_backoff=mcp_startup_backoff,
        )

    # --- Kiro-specific hooks ------------------------------------------------
    def _make_invocation_name(self) -> str:
        return f"dream_cycle_{uuid.uuid4().hex[:8]}"

    def _config_path(self, name: str) -> str:
        return os.path.join(AGENTS_DIR, f"{name}.json")

    def _build_config(self, name: str, system_prompt: str, needs_tools: bool):
        return {
            "name": name,
            "prompt": system_prompt,
            "mcpServers": _SECOND_BRAIN_MCP if needs_tools else {},
            "tools": ["*"] if needs_tools else [],
        }

    def _build_command(
        self,
        *,
        name,
        config_path,
        system_prompt,
        user_message,
        needs_tools,
        effort,
        **_adapter_opts,
    ) -> list:
        cmd = [
            KIRO_CLI, "chat",
            "--no-interactive",
            "--agent", name,
            "--model", self.model,
        ]
        if needs_tools:
            cmd.append("--trust-all-tools")
            # Fail loudly (exit 3) if an MCP server can't start, instead of
            # silently running tool-less (root cause of the Jun 8–12 zero-candidate
            # runs). See docs/FABLE5-THINKER-PLAN.md.
            cmd.append("--require-mcp-startup")
        if effort:
            cmd += ["--effort", effort]
        # User message is the positional INPUT argument
        cmd.append(user_message)
        return cmd

    def _adapter_metrics_defaults(self) -> dict:
        return {"mcp_startup_retries": 0}

    def _metrics_dir(self) -> str:
        # Read this module's global at call time so monkeypatching
        # src.backends.kiro.METRICS_DIR (tests) and the launchd env override both
        # take effect — identical to the original inline behavior.
        return METRICS_DIR

    def _extract_usage(self, *, parsed, raw, result, metrics):
        # usage=None / usage_source="estimate": kiro-cli exposes no real token
        # usage — only the char/4 estimates recorded in the metrics log. Metered
        # backends return real counts with usage_source="real", so the run cost
        # budget never silently compares an estimate against a measurement.
        return None, "estimate"

    # --- MCP startup hardening: --require-mcp-startup + exit-3 retry ---------
    def _attempts_allowed(self, *, needs_tools, mcp_startup_retries: int = 2, **_opts) -> int:
        return 1 + (mcp_startup_retries if needs_tools else 0)

    def _initial_backoff(self, *, mcp_startup_backoff: float = 3.0, **_opts) -> float:
        return mcp_startup_backoff

    def _should_retry(
        self, result, *, needs_tools, attempt, attempts_allowed, metrics, backoff
    ) -> bool:
        # MCP startup can fail transiently in the scheduled (launchd) context.
        # With --require-mcp-startup, kiro exits 3 *before* any model call, so
        # retrying is cheap (no tokens spent) and usually succeeds once warm.
        if needs_tools and result.returncode == 3:
            metrics["mcp_startup_retries"] = attempt
            logger.warning(
                "MCP startup failed (exit 3), attempt %d/%d; retrying in %.0fs. "
                "stderr tail: %s",
                attempt, attempts_allowed, backoff, (result.stderr or "")[-400:],
            )
            return True
        return False
