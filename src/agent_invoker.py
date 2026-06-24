"""Backward-compatibility shim for the Kiro backend.

The Kiro execution backend now lives in :mod:`src.backends.kiro` as
``KiroInvoker`` — part of the pluggable Model Backends abstraction
(see ``docs/MODEL-BACKENDS.md``). ``AgentInvoker`` remains here as an alias for
the duration of the migration, so existing imports
(``from src.agent_invoker import AgentInvoker``) and the module-level names keep
working unchanged.

New code should import :class:`src.backends.kiro.KiroInvoker` (or, better,
resolve a backend via the role->backend map) rather than this shim.
"""

from src.backends.kiro import (  # noqa: F401
    AGENTS_DIR,
    DEFAULT_MODEL,
    KIRO_CLI,
    METRICS_DIR,
    KiroInvoker,
    KiroInvoker as AgentInvoker,
    _SECOND_BRAIN_MCP,
    _record_call_metrics,
)

__all__ = [
    "AgentInvoker",
    "KiroInvoker",
    "DEFAULT_MODEL",
    "KIRO_CLI",
    "AGENTS_DIR",
    "METRICS_DIR",
    "_SECOND_BRAIN_MCP",
    "_record_call_metrics",
]
