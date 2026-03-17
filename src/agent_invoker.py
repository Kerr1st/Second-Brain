"""Agent Invoker — abstraction layer for kiro --no-interactive subprocesses.

Handles process spawning, timeout enforcement, output parsing, and error handling.
Used by the DreamCycleOrchestrator for all agent invocations.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)


class AgentInvoker:
    """Invoke kiro --no-interactive as isolated subprocesses with system prompts."""

    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        mcp_config: str | None = None,
        timeout: int = 300,
    ) -> dict:
        """Invoke kiro --no-interactive as a subprocess.

        Args:
            system_prompt: The agent's role prompt.
            user_message: Input data (memory slices, candidates, etc.).
            mcp_config: File path to MCP server config for tool access
                        (Explorer/Thinker need it, evaluators don't).
            timeout: Max seconds before killing the process (default 300).

        Returns:
            dict with 'output' (parsed JSON) and 'raw' (raw stdout text).

        Raises:
            TimeoutError: If the subprocess exceeds the timeout.
            RuntimeError: If the subprocess exits with a non-zero code.
        """
        cmd = ["kiro", "--no-interactive", "--system-prompt", system_prompt]

        if mcp_config is not None:
            cmd.extend(["--mcp-config", mcp_config])

        logger.debug("Invoking agent: timeout=%ds, mcp_config=%s", timeout, mcp_config)

        try:
            result = subprocess.run(
                cmd,
                input=user_message,
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
                "Agent exited with code %d: %s", result.returncode, result.stderr
            )
            raise RuntimeError(
                f"Agent subprocess failed (exit code {result.returncode}): {result.stderr}"
            )

        raw = result.stdout
        parsed = self.parse_json_output(raw)

        return {"output": parsed, "raw": raw}

    @staticmethod
    def parse_json_output(raw: str) -> dict | list:
        """Extract JSON from agent output.

        Handles:
        - Bare JSON objects/arrays
        - JSON wrapped in markdown code fences (```json ... ```)
        - JSON with surrounding non-JSON text

        Returns:
            The parsed JSON object or array.

        Raises:
            ValueError: If no valid JSON found in the output.
        """
        # 1. Try direct parse
        stripped = raw.strip()
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. Try extracting from markdown code fences
        fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
        match = fence_pattern.search(raw)
        if match:
            try:
                return json.loads(match.group(1).strip())
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
                return json.loads(fragment)
            except (json.JSONDecodeError, ValueError):
                continue

        raise ValueError(f"No valid JSON found in agent output: {raw[:200]}")
