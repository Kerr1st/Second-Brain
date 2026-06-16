"""Agent Invoker — abstraction layer for kiro-cli chat subprocesses.

Handles agent config creation, process spawning, timeout enforcement,
output parsing, and error handling. Used by the DreamCycleOrchestrator
for all agent invocations.

Each invocation writes a temporary agent config to ~/.kiro/agents/ with
the system prompt in the "prompt" field and MCP servers (if needed) in
the "mcpServers" field, then invokes kiro-cli chat --no-interactive.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from src.backends.base import parse_json_output as _parse_json_output

logger = logging.getLogger(__name__)

KIRO_CLI = os.environ.get("KIRO_CLI", os.path.expanduser("~/.local/bin/kiro-cli"))
AGENTS_DIR = os.path.expanduser("~/.kiro/agents")
DEFAULT_MODEL = "claude-opus-4.8"

# MCP server config for the Second Brain — Explorer and Thinker need this
# to query memories via memory_search, memory_read, etc.
_SECOND_BRAIN_MCP = {
    "second-brain": {
        "command": sys.executable,
        "args": ["-m", "src.mcp_server"],
        "cwd": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    }
}

# --- Phase 0 LLM-call instrumentation (see docs/FABLE5-THINKER-PLAN.md) ---
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS_DIR = os.environ.get(
    "KIRO_LLM_METRICS_DIR", os.path.join(_REPO_ROOT, "logs", "llm_metrics")
)


def _record_call_metrics(metrics: dict, run_id: str | None) -> None:
    """Append one JSONL record of an LLM call's metrics. Never raises.

    Captures per-call stage, model, effort, latency, and char-based token
    ESTIMATES. The kiro-cli path returns no structured usage, so token counts
    are estimates (chars / 4); real usage arrives when a stage moves to a
    direct Bedrock InvokeModel call (Phase 1).
    """
    try:
        os.makedirs(METRICS_DIR, exist_ok=True)
        fname = f"{run_id}.jsonl" if run_id else "adhoc.jsonl"
        with open(os.path.join(METRICS_DIR, fname), "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics) + "\n")
    except Exception:
        logger.debug("Failed to record LLM call metrics", exc_info=True)


class KiroInvoker:
    """Invoke ``kiro-cli chat --no-interactive`` as isolated subprocesses.

    The Kiro backend adapter — routes to Amazon Q ($0 metered). Implements the
    :class:`src.backends.base.Invoker` contract; returns ``usage=None`` because
    kiro-cli exposes no real token usage (only char/4 estimates in the metrics
    log). See ``docs/MODEL-BACKENDS.md``.
    """

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_MODEL

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

        Returns:
            dict with 'output' (parsed JSON) and 'raw' (raw stdout text).

        Raises:
            TimeoutError: If the subprocess exceeds the timeout.
            RuntimeError: If the subprocess exits with a non-zero code.
        """
        # Build agent config
        agent_name = f"dream_cycle_{uuid.uuid4().hex[:8]}"
        needs_tools = tools
        agent_config = {
            "name": agent_name,
            "prompt": system_prompt,
            "mcpServers": _SECOND_BRAIN_MCP if needs_tools else {},
            "tools": ["*"] if needs_tools else [],
        }

        agent_path = os.path.join(AGENTS_DIR, f"{agent_name}.json")
        os.makedirs(AGENTS_DIR, exist_ok=True)

        # Phase 0 instrumentation: per-call metrics (docs/FABLE5-THINKER-PLAN.md).
        # Token counts are ESTIMATES (chars / 4); kiro-cli returns no usage.
        metrics = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "stage": stage,
            "agent_name": agent_name,
            "model": self.model,
            "effort": effort,
            "tools": needs_tools,
            "timeout_s": timeout,
            "system_prompt_chars": len(system_prompt),
            "user_message_chars": len(user_message),
            "input_chars": len(system_prompt) + len(user_message),
            "output_chars": 0,
            "success": False,
            "error": None,
            "mcp_startup_retries": 0,
        }
        t0 = time.monotonic()

        try:
            with open(agent_path, "w", encoding="utf-8") as f:
                json.dump(agent_config, f)

            cmd = [
                KIRO_CLI, "chat",
                "--no-interactive",
                "--agent", agent_name,
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

            logger.debug(
                "Invoking agent: name=%s, timeout=%ds, tools=%s",
                agent_name, timeout, needs_tools,
            )

            # MCP startup can fail transiently in the scheduled (launchd) context.
            # With --require-mcp-startup, kiro exits 3 *before* any model call, so
            # retrying is cheap (no tokens spent) and usually succeeds once warm.
            attempts_allowed = 1 + (mcp_startup_retries if needs_tools else 0)
            backoff = mcp_startup_backoff
            for attempt in range(1, attempts_allowed + 1):
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    stderr = exc.stderr or ""
                    metrics["error"] = "timeout"
                    logger.error("Agent timed out after %ds: %s", timeout, stderr)
                    raise TimeoutError(
                        f"Agent subprocess timed out after {timeout}s: {stderr}"
                    ) from exc

                if needs_tools and result.returncode == 3 and attempt < attempts_allowed:
                    metrics["mcp_startup_retries"] = attempt
                    logger.warning(
                        "MCP startup failed (exit 3), attempt %d/%d; retrying in %.0fs. stderr tail: %s",
                        attempt, attempts_allowed, backoff, (result.stderr or "")[-400:],
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                break

            if result.returncode != 0:
                metrics["error"] = f"exit_code={result.returncode}"
                logger.error(
                    "Agent exited with code %d: %s",
                    result.returncode, result.stderr,
                )
                raise RuntimeError(
                    f"Agent subprocess failed (exit code {result.returncode}): "
                    f"{result.stderr}"
                )

            raw = result.stdout
            metrics["output_chars"] = len(raw)
            logger.debug("Agent raw output length: %d chars", len(raw))

            # Save raw output for debugging (especially useful for first runs)
            raw_debug_path = os.path.join("/tmp", f"kiro_agent_{agent_name}_raw.txt")
            try:
                with open(raw_debug_path, "w", encoding="utf-8") as f:
                    f.write(raw)
                logger.debug("Raw output saved to %s", raw_debug_path)
            except OSError:
                pass

            try:
                parsed = self.parse_json_output(raw)
            except ValueError:
                metrics["error"] = "json_parse_failed"
                logger.error("Failed to parse JSON from agent output (%d chars). Raw saved to %s", len(raw), raw_debug_path)
                raise

            metrics["success"] = True
            # usage=None / usage_source="estimate": kiro-cli exposes no real token
            # usage — only the char/4 estimates recorded in the metrics log. Metered
            # backends return real counts with usage_source="real", so the run cost
            # budget never silently compares an estimate against a measurement.
            return {"output": parsed, "raw": raw, "usage": None, "usage_source": "estimate"}

        finally:
            metrics["latency_s"] = round(time.monotonic() - t0, 3)
            metrics["est_input_tokens"] = metrics["input_chars"] // 4
            metrics["est_output_tokens"] = metrics["output_chars"] // 4
            _record_call_metrics(metrics, run_id)
            # Clean up the temporary agent config
            try:
                os.remove(agent_path)
            except OSError:
                pass

    # JSON recovery backstop lives in base.py now (shared across backends, so a
    # future ClaudeCodeInvoker need not import from kiro). The KiroInvoker
    # entry point is kept for back-compat — the agent_invoker shim re-exports it
    # as AgentInvoker.parse_json_output, which the test-suite and older callers use.
    parse_json_output = staticmethod(_parse_json_output)
