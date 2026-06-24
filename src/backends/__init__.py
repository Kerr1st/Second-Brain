"""Pluggable model-backend execution layer.

Each dream-cycle agent role is invoked through an :class:`Invoker`. A backend
family implements that one contract over a specific execution path — an agentic
CLI that speaks MCP (Kiro, Claude Code, Codex) or a tool-less Direct-API call
(Bedrock, and later Anthropic/OpenAI direct). See ``docs/MODEL-BACKENDS.md``.
"""

from src.backends.base import (
    BACKEND_CAPABILITIES,
    BackendCapabilities,
    InvocationResult,
    Invoker,
    assert_backend_supports_role,
)

__all__ = [
    "BACKEND_CAPABILITIES",
    "BackendCapabilities",
    "InvocationResult",
    "Invoker",
    "assert_backend_supports_role",
]
