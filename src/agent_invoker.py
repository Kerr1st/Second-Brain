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
import uuid

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


class AgentInvoker:
    """Invoke kiro-cli chat --no-interactive as isolated subprocesses."""

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_MODEL

    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        mcp_config: str | None = None,
        timeout: int = 300,
        effort: str | None = None,
    ) -> dict:
        """Invoke kiro-cli chat --no-interactive as a subprocess.

        Creates a temporary agent config with the system prompt, invokes
        kiro-cli chat, and parses the JSON output.

        Args:
            system_prompt: The agent's role prompt (written to agent config).
            user_message: Input data passed as the chat input argument.
            mcp_config: When truthy, the Second Brain MCP server is included
                in the agent config so the agent can use memory tools.
                Evaluators should pass None (no tool access).
            timeout: Max seconds before killing the process (default 300).

        Returns:
            dict with 'output' (parsed JSON) and 'raw' (raw stdout text).

        Raises:
            TimeoutError: If the subprocess exceeds the timeout.
            RuntimeError: If the subprocess exits with a non-zero code.
        """
        # Build agent config
        agent_name = f"dream_cycle_{uuid.uuid4().hex[:8]}"
        needs_tools = mcp_config is not None
        agent_config = {
            "name": agent_name,
            "prompt": system_prompt,
            "mcpServers": _SECOND_BRAIN_MCP if needs_tools else {},
            "tools": ["*"] if needs_tools else [],
        }

        agent_path = os.path.join(AGENTS_DIR, f"{agent_name}.json")
        os.makedirs(AGENTS_DIR, exist_ok=True)

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
            if effort:
                cmd += ["--effort", effort]

            # User message is the positional INPUT argument
            cmd.append(user_message)

            logger.debug(
                "Invoking agent: name=%s, timeout=%ds, tools=%s",
                agent_name, timeout, needs_tools,
            )

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                stderr = exc.stderr or ""
                logger.error("Agent timed out after %ds: %s", timeout, stderr)
                raise TimeoutError(
                    f"Agent subprocess timed out after {timeout}s: {stderr}"
                ) from exc

            if result.returncode != 0:
                logger.error(
                    "Agent exited with code %d: %s",
                    result.returncode, result.stderr,
                )
                raise RuntimeError(
                    f"Agent subprocess failed (exit code {result.returncode}): "
                    f"{result.stderr}"
                )

            raw = result.stdout
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
                logger.error("Failed to parse JSON from agent output (%d chars). Raw saved to %s", len(raw), raw_debug_path)
                raise

            return {"output": parsed, "raw": raw}

        finally:
            # Clean up the temporary agent config
            try:
                os.remove(agent_path)
            except OSError:
                pass

    # Regex to strip ANSI escape sequences from kiro-cli terminal output
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    @classmethod
    def parse_json_output(cls, raw: str) -> dict | list:
        """Extract JSON from agent output.

        Handles:
        - ANSI escape codes (stripped first)
        - Bare JSON objects/arrays
        - JSON wrapped in markdown code fences (```json ... ```)
        - JSON with surrounding non-JSON text (kiro-cli adds credit/timing lines)

        Returns:
            The parsed JSON object or array.

        Raises:
            ValueError: If no valid JSON found in the output.
        """
        # 0. Strip ANSI escape codes — kiro-cli outputs colored terminal text
        raw = cls._ANSI_RE.sub("", raw)

        # 1. Try direct parse
        stripped = raw.strip()
        try:
            return json.loads(stripped, strict=False)
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. Try extracting from markdown code fences
        fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
        match = fence_pattern.search(raw)
        if match:
            try:
                return json.loads(match.group(1).strip(), strict=False)
            except (json.JSONDecodeError, ValueError):
                pass

        # 3. Try finding first { or [ and parse from there.
        #    Pick whichever delimiter appears earliest so that an outer array
        #    is preferred over an inner object (e.g. "[{}]" should parse as a list).
        candidates = []
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start_idx = stripped.find(start_char)
            if start_idx == -1:
                continue
            end_idx = stripped.rfind(end_char)
            if end_idx == -1 or end_idx <= start_idx:
                continue
            candidates.append((start_idx, start_char, end_char, end_idx))

        # Sort by earliest start position so the outermost structure wins
        candidates.sort(key=lambda c: c[0])
        for start_idx, _sc, _ec, end_idx in candidates:
            fragment = stripped[start_idx : end_idx + 1]
            try:
                return json.loads(fragment, strict=False)
            except (json.JSONDecodeError, ValueError):
                continue

        raise ValueError(f"No valid JSON found in agent output: {raw[:200]}")
