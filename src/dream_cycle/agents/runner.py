"""Agents Runner responsibilities."""
from __future__ import annotations
import json
import logging
import os
from src.backends.base import BACKEND_CAPABILITIES
from src.backends.cost import UsageUnavailable, invoke_metered
from src.backends.resolver import DREAM_ROLES
from src.dream_cycle.budget import *
from src.dream_cycle.budget import _RESERVE_FLOOR_USD
logger = logging.getLogger(__name__)

class AgentRunner:
    """Shared strict invocation, provenance and fixture capture for every agent."""

    def _invoker_for(self, role: str):
        """Resolve the Invoker for a role from the active backend profile."""
        return self.resolver.invoker_for(role)


    def _role_has_tools(self, role: str) -> bool:
        """Whether the role's backend can attach MCP tools (agentic CLIs can;
        the Direct-API family cannot and must be fed a context packet)."""
        backend = self.resolver.spec_for(role).backend
        caps = BACKEND_CAPABILITIES.get(backend)
        return caps.supports_mcp if caps is not None else True


    def _invoke_metered(self, role: str, **kwargs):
        """All Dream Cycle requests share strict accounting, including failures.

        A transport/throttle/timeout failure is charged a conservative reservation
        by ``invoke_metered`` and re-raised as-is, so the stage keeps its own
        resilience rule: Explorer crash -> arrivals-only slice, Thinker failure ->
        skip the slice, evaluator failure -> bounded retry, skim failure ->
        non-fatal. Only a blocked tracker (routing guarantee failed, unmeterable
        response) is escalated to ``UsageUnavailable`` so nothing retries it.
        (R1 transport-error policy, preserved in R3.)
        """
        kwargs.setdefault("reserve_floor_usd", _RESERVE_FLOOR_USD.get(role, 0.0))
        try:
            return invoke_metered(
                self._invoker_for(role), self.resolver.spec_for(role), self._spend, **kwargs
            )
        except (TimeoutError, RuntimeError, ValueError) as exc:
            if self._spend.blocked_reason:
                raise UsageUnavailable(self._spend.blocked_reason) from exc
            raise


    def _budget_remaining(self) -> float | None:
        """Dollars remaining under the stop threshold, from metered usage."""
        cap = getattr(self._spend, "cap_usd", None)
        if not isinstance(cap, (int, float)) or cap <= 0:
            return None
        return float(cap) - float(self._spend.total_usd)


    def _backend_provenance(self) -> dict:
        """Snapshot the active profile's (backend, model, effort) per role.

        Persisted on the run record so a run's candidates/verdicts stay auditable
        across a backend swap or the Mac Mini cutover (Kiro-Opus era vs
        Claude-Code-on-Bedrock era). Built here from the resolver (read-only)
        rather than on the Resolver itself, to keep this change out of the
        backends lane. See docs/MODEL-BACKENDS.md.
        """
        provenance: dict[str, dict] = {}
        for role in DREAM_ROLES:
            try:
                spec = self.resolver.spec_for(role)
            except KeyError:
                # Optional stage not configured (e.g. no skim role in a hand-built
                # profile): the run proceeds without it, so provenance omits it.
                continue
            provenance[role] = {
                "backend": spec.backend,
                "model": spec.model,
                "effort": spec.effort,
            }
        return provenance


    def _capture(self, name: str, *, inputs: dict | None = None, output: object = None) -> None:
        """Write agent call inputs/outputs to capture_dir as JSON fixtures.

        No-op when capture_dir is not set. Failures are logged but never
        propagate — fixture capture must not break the pipeline.
        """
        if not self._capture_dir:
            return
        try:
            os.makedirs(self._capture_dir, mode=0o700, exist_ok=True)
            if os.path.islink(self._capture_dir) or os.stat(self._capture_dir).st_mode & 0o077:
                raise OSError("Capture directory must be private (0700) and not a symlink")
            data: dict = {}
            if inputs is not None:
                data["inputs"] = inputs
            if output is not None:
                data["output"] = output
            path = os.path.join(self._capture_dir, f"{name}.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            logger.debug("Captured fixture: %s", path)
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to capture fixture %s", name, exc_info=True)
