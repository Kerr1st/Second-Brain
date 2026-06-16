"""Backend abstraction for pluggable agent execution paths.

This module defines the parts shared by every backend:

* :class:`Invoker` — the common call contract. It mirrors the call the
  orchestrator already makes against ``AgentInvoker`` today (plus a normalized
  ``usage`` field), so refactoring the current invoker behind it is
  behavior-preserving.
* :class:`InvocationResult` — the normalized return shape.
* :class:`BackendCapabilities` — flags that drive the guardrails.
* :data:`BACKEND_CAPABILITIES` — the capability table (facts from the
  "Backend capability matrix" in ``docs/MODEL-BACKENDS.md``).
* :func:`assert_backend_supports_role` — the Explorer-needs-tools guard.

Concrete adapters (``KiroInvoker`` and, later, ``ClaudeCodeInvoker`` etc.) and
the role->backend resolver live in sibling modules. Nothing here changes runtime
behavior; it is the contract the refactor and the new adapters build against.
See ``docs/MODEL-BACKENDS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, TypedDict, runtime_checkable


class InvocationResult(TypedDict):
    """Normalized return of every :meth:`Invoker.invoke`.

    ``output`` is the parsed payload (recovered via the backend-agnostic
    ``parse_json_output`` backstop); its shape is stage-specific. ``raw`` is the
    backend's final text. ``usage`` carries real token counts when the backend
    reports them (Claude Code / Codex / Bedrock) and is ``None`` for backends
    that do not (Kiro exposes char/4 estimates only, not real usage).

    ``usage_source`` records whether ``usage`` is real (``"real"``) or a char/4
    estimate (``"estimate"``; Kiro). It exists so the run cost-budget and the
    Fable A/B never silently compare an estimate against a measurement.
    """

    output: Any
    raw: str
    usage: Optional[dict]
    usage_source: Literal["real", "estimate"]


@runtime_checkable
class Invoker(Protocol):
    """Common contract for every backend.

    Concrete adapters may accept additional backend-specific keyword arguments
    (e.g. Kiro's MCP-startup retry knobs) as long as they remain optional, so
    existing call sites keep working unchanged.
    """

    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        *,
        mcp_config: Optional[str] = None,
        timeout: int = 300,
        effort: Optional[str] = None,
        stage: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> InvocationResult:
        """Run one agent turn and return a normalized result.

        Args:
            system_prompt: The role prompt establishing the agent's identity.
            user_message: The input payload for this turn.
            mcp_config: Tool-access signal. Truthy means attach the live
                second-brain MCP tools (agentic stages: Explorer/Thinker).
                Tool-less stages (evaluators, Express) pass ``None``. Direct-API
                backends require ``None`` and must fail loudly otherwise.
            timeout: Max seconds before the call is aborted (``TimeoutError``).
            effort: Reasoning-effort level. NOTE: level names/semantics differ
                across backends ("high" on Claude != Kiro != Codex), so it is
                recorded as provenance, not assumed equivalent across backends.
            stage: Pipeline stage label, for metrics/provenance.
            run_id: Dream-cycle run id, for metrics/provenance.

        Returns:
            :class:`InvocationResult`.

        Raises:
            TimeoutError: If the call exceeds ``timeout``.
            RuntimeError: If the backend fails (non-zero exit, transport error,
                or — for agentic backends — MCP tools failed to attach).
        """
        ...


@dataclass(frozen=True)
class BackendCapabilities:
    """Capability flags for a backend family, used to drive guardrails.

    Attributes:
        supports_mcp: The backend can attach live MCP tools. Agentic CLIs are
            ``True``; tool-less Direct-API backends are ``False``.
        metered: The backend incurs per-token cost (its usage must be tracked
            against the run budget).
        structured_output: The backend offers native schema-constrained output
            (e.g. ``--json-schema``). The ``parse_json_output`` backstop is kept
            regardless.
        reports_usage: The backend returns real token usage (not an estimate).
    """

    supports_mcp: bool
    metered: bool
    structured_output: bool
    reports_usage: bool

    def __post_init__(self) -> None:
        # Invariant: never run a metered backend you cannot meter. A metered
        # seat whose usage is invisible would silently escape the run budget.
        if self.metered and not self.reports_usage:
            raise ValueError(
                "Invalid BackendCapabilities: a metered backend must report usage "
                "(metered=True requires reports_usage=True)."
            )


# Capability table — facts from the "Backend capability matrix" in
# docs/MODEL-BACKENDS.md. Declaring a backend here does NOT imply its adapter is
# implemented yet; adapters are added in priority order (Kiro, then Claude Code).
BACKEND_CAPABILITIES: dict[str, BackendCapabilities] = {
    # Agentic CLI families (speak MCP).
    "kiro": BackendCapabilities(
        supports_mcp=True, metered=False, structured_output=False, reports_usage=False
    ),
    "claude_code": BackendCapabilities(
        supports_mcp=True, metered=True, structured_output=True, reports_usage=True
    ),
    "codex": BackendCapabilities(
        supports_mcp=True, metered=True, structured_output=True, reports_usage=True
    ),
    # Direct-API family (tool-less by design).
    "bedrock": BackendCapabilities(
        supports_mcp=False, metered=True, structured_output=True, reports_usage=True
    ),
}


def assert_backend_supports_role(backend: str, role: str) -> None:
    """Guard a role->backend assignment.

    The Explorer needs a live tool-loop (memory_search/read), so it requires an
    MCP-capable backend. Routing it to a tool-less Direct-API backend is the
    deferred "B1" decoupling (see ``docs/DREAM-CYCLE-MCP-DECOUPLING.md``); we
    reject it loudly rather than let the Explorer run blind.

    Args:
        backend: Backend name; must be a key of :data:`BACKEND_CAPABILITIES`.
        role: Agent role (``"explorer"``, ``"thinker"``, ``"skeptic"``, ...).

    Raises:
        KeyError: If ``backend`` is unknown.
        ValueError: If the assignment violates a capability requirement.
    """
    caps = BACKEND_CAPABILITIES[backend]
    if role == "explorer" and not caps.supports_mcp:
        raise ValueError(
            f"Explorer requires an MCP-capable backend, but {backend!r} is tool-less "
            f"(supports_mcp=False). Routing the Explorer to a Direct-API backend is the "
            f"deferred B1 decoupling — see docs/DREAM-CYCLE-MCP-DECOUPLING.md."
        )
