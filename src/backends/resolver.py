"""Role -> backend resolution for the dream cycle.

Reads the committed per-machine profiles in ``config/backends.toml`` (selected by
the ``SECOND_BRAIN_PROFILE`` env var; unset => the default profile, which
selects the controlled corpus-safe profile) and hands the orchestrator a concrete
:class:`~src.backends.base.Invoker` per role, plus the resolved
``(backend, model, effort)`` for provenance.

Invokers are cached per ``(backend, model, options)``. Corpus roles reject Kiro
and Fable before adapter construction; data-processing roles use direct Bedrock.
A role-diverse map reuses one client per distinct backend+model. See
``docs/MODEL-BACKENDS.md``.

Adapter options (``options = { ... }`` on a role, skim stage 2026-09-20): a role
may carry an inline table of constructor keyword arguments for its adapter, e.g.
``options = { temperature = 0.0, service_tier = "flex", max_tokens = 4096 }``.
Only the ``bedrock`` adapter accepts options today (:data:`ADAPTER_OPTIONS`);
an option on another backend, or an unknown key, is rejected at config load.
"""

from __future__ import annotations

import os
import math
import tomllib
from dataclasses import dataclass, field, replace
from typing import Any

from src.backends.base import (
    AGENT_ROLES, CORPUS_ROLES, GROUNDING_AUDITOR_ROLE, DENIED_BACKENDS_FOR_CORPUS, DENIED_MODEL_SUBSTRINGS,
    BACKEND_CAPABILITIES, Invoker, assert_backend_supports_role, assert_corpus_route,
)
from src.backends.bedrock import BedrockInvoker
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.codex import CodexInvoker
from src.backends.kiro import KiroInvoker

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "backends.toml")

# backend name -> adapter class. The three agentic CLIs speak MCP and can run
# any agent role; `bedrock` is the tool-less Direct-API family (Thinker/evaluators
# and the skim — the Explorer guard below rejects it for the Explorer).
DEFAULT_ADAPTERS: dict[str, type] = {
    "kiro": KiroInvoker,
    "claude_code": ClaudeCodeInvoker,
    "codex": CodexInvoker,
    "bedrock": BedrockInvoker,
}

#: Constructor keyword arguments a role's ``options`` table may set, per backend.
#: Backends absent here accept no options.
ADAPTER_OPTIONS: dict[str, frozenset[str]] = {
    "bedrock": frozenset({"temperature", "service_tier", "max_tokens"}),
}

SKIM_ROLE = "skim"
DISTILL_ROLE = "distill"
CODEX_SEMANTIC_ROLE = "codex_semantic"
EDITOR_ROLE = "editor"
DREAM_ROLES = (*AGENT_ROLES, GROUNDING_AUDITOR_ROLE, SKIM_ROLE)
VALID_ROLES = (*DREAM_ROLES, DISTILL_ROLE, CODEX_SEMANTIC_ROLE, EDITOR_ROLE)

_REQUIRED_ROLES = frozenset(VALID_ROLES)


def _validate_profile(name: str, role_map: dict[str, "RoleBackend"]) -> None:
    """Fail fast if a configured profile doesn't define exactly the required roles.

    A profile missing a role would otherwise abort the dream cycle **mid-run**
    (after LLM spend) with a bare ``KeyError`` when that role is first resolved;
    an unknown (typo'd) role would silently never be used. Catch both at config
    load, before the orchestrator builds. Also rejects adapter options the
    backend does not accept, and a skim role off the Bedrock backend.
    """
    present = set(role_map)
    missing = _REQUIRED_ROLES - present
    if missing:
        raise ValueError(
            f"backend profile {name!r} is missing required role(s): {sorted(missing)}. "
            f"Every profile must define all of {list(VALID_ROLES)} (see config/backends.toml)."
        )
    unknown = present - _REQUIRED_ROLES
    if unknown:
        raise ValueError(
            f"backend profile {name!r} has unknown role(s): {sorted(unknown)} (typo?). "
            f"Valid roles: {list(VALID_ROLES)}."
        )
    for role, spec in role_map.items():
        if spec.backend not in BACKEND_CAPABILITIES:
            raise ValueError(
                f"backend profile {name!r}: role {role!r} uses unknown backend "
                f"{spec.backend!r}. Valid backends: {sorted(BACKEND_CAPABILITIES)}."
            )
        assert_corpus_route(spec.backend, spec.model, role)
        _validate_options(name, spec)
        # Every role/backend capability rule (Explorer needs tools, skim is
        # Bedrock-only) is checked here too, so a bad profile fails at load.
        assert_backend_supports_role(spec.backend, role)


def _validate_options(profile_name: str, spec: "RoleBackend") -> None:
    if not spec.options:
        return
    options = spec.options
    if "max_tokens" in options:
        tokens = options["max_tokens"]
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
    if "temperature" in options:
        temperature = options["temperature"]
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not 0 <= temperature <= 1:
            raise ValueError("temperature must be finite and between 0 and 1")
    allowed = ADAPTER_OPTIONS.get(spec.backend, frozenset())
    unknown = set(spec.options) - allowed
    if unknown:
        raise ValueError(
            f"backend profile {profile_name!r}: role {spec.role!r} sets option(s) {sorted(unknown)} "
            f"that the {spec.backend!r} adapter does not accept. "
            f"Allowed: {sorted(allowed) or 'none'}."
        )


@dataclass(frozen=True)
class RoleBackend:
    """Resolved execution spec for one role (also the provenance record).

    ``options`` are adapter constructor keyword arguments from the role's
    ``options = { ... }`` table (``None`` when absent).
    """

    role: str
    backend: str
    model: str
    effort: str | None = None
    options: dict[str, Any] | None = field(default=None, hash=False)

    def options_key(self) -> tuple:
        """Hashable form of ``options`` for the invoker cache."""
        return tuple(sorted((self.options or {}).items()))


def load_profiles(
    config_path: str | None = None,
) -> tuple[dict[str, dict[str, RoleBackend]], str]:
    """Parse ``backends.toml``.

    Returns ``(profiles, default_profile_name)`` where ``profiles`` maps
    ``profile_name -> {role -> RoleBackend}``.
    """
    path = config_path or os.environ.get(
        "SECOND_BRAIN_BACKENDS_CONFIG", DEFAULT_CONFIG_PATH
    )
    with open(path, "rb") as f:
        data = tomllib.load(f)
    default_profile = data.get("default_profile", "controlled")
    profiles: dict[str, dict[str, RoleBackend]] = {}
    for pname, roles in data.get("profiles", {}).items():
        role_map: dict[str, RoleBackend] = {}
        for role, spec in roles.items():
            options = spec.get("options")
            if options is not None and not isinstance(options, dict):
                raise ValueError(
                    f"backend profile {pname!r}: role {role!r} 'options' must be a table, "
                    f"got {type(options).__name__}"
                )
            role_map[role] = RoleBackend(
                role=role,
                backend=spec["backend"],
                model=spec["model"],
                effort=spec.get("effort"),
                options=dict(options) if options else None,
            )
        _validate_profile(pname, role_map)
        profiles[pname] = role_map
    return profiles, default_profile


def active_profile_name(default_profile: str) -> str:
    """The selected profile: ``SECOND_BRAIN_PROFILE`` or the config default."""
    return os.environ.get("SECOND_BRAIN_PROFILE", default_profile)


class Resolver:
    """Resolves a role to a cached Invoker + its execution spec for one profile."""

    def __init__(
        self,
        profile: dict[str, RoleBackend],
        adapters: dict[str, type] | None = None,
    ):
        # A hand-built (partial) profile that seats a skeptic but no grounding
        # auditor inherits the skeptic route for the audit, which is what every
        # committed profile configures explicitly. Committed profiles must name
        # the role (``_validate_profile``).
        if GROUNDING_AUDITOR_ROLE not in profile and "skeptic" in profile:
            profile = {**profile, GROUNDING_AUDITOR_ROLE: replace(profile["skeptic"], role=GROUNDING_AUDITOR_ROLE)}
        self._profile = profile
        self._adapters = adapters if adapters is not None else DEFAULT_ADAPTERS
        self._cache: dict[tuple, Invoker] = {}
        # Partial programmatic profiles are useful to callers and tests, but
        # every supplied route still obeys the exact same corpus boundary.
        for role, spec in self._profile.items():
            if role not in VALID_ROLES or spec.role != role:
                raise ValueError(f"Invalid role mapping: {role!r} -> {spec.role!r}")
            assert_corpus_route(spec.backend, spec.model, role)
            assert_backend_supports_role(spec.backend, role)
            _validate_options("programmatic", spec)

    def spec_for(self, role: str) -> RoleBackend:
        try:
            return self._profile[role]
        except KeyError:
            raise KeyError(
                f"role {role!r} not configured in the active backend profile"
            ) from None

    def invoker_for(self, role: str) -> Invoker:
        spec = self.spec_for(role)
        assert_corpus_route(spec.backend, spec.model, role)
        # Capability guards (Explorer needs tools; skim is Bedrock-only).
        assert_backend_supports_role(spec.backend, role)
        key = (spec.backend, spec.model, spec.options_key())
        if key not in self._cache:
            adapter_cls = self._adapters.get(spec.backend)
            if adapter_cls is None:
                raise NotImplementedError(
                    f"Backend {spec.backend!r} has no adapter yet (role {role!r}). "
                    f"Implemented: {sorted(self._adapters)}. See docs/MODEL-BACKENDS.md."
                )
            self._cache[key] = adapter_cls(model=spec.model, **(spec.options or {}))
        return self._cache[key]


def default_resolver(
    config_path: str | None = None,
    adapters: dict[str, type] | None = None,
) -> Resolver:
    """Build the Resolver for the active profile (``SECOND_BRAIN_PROFILE`` or default)."""
    profiles, default_profile = load_profiles(config_path)
    pname = active_profile_name(default_profile)
    if pname not in profiles:
        raise KeyError(
            f"SECOND_BRAIN_PROFILE={pname!r} not found in {sorted(profiles)}. "
            f"See config/backends.toml."
        )
    # Fail fast: the active profile must define every role before the orchestrator
    # builds — never abort mid-run (after LLM spend) on a missing/typo'd role.
    _validate_profile(pname, profiles[pname])
    return Resolver(profiles[pname], adapters=adapters)


def resolve_stage(role: str, *, resolver=None, model_override: str | None = None):
    """Resolve a corpus stage and validate optional overrides before construction.

    Consumers use this together with cost.invoke_metered; neither function
    invokes a model while resolving configuration.
    """
    resolver = resolver or default_resolver()
    spec = resolver.spec_for(role)
    if spec.role != role or role not in CORPUS_ROLES:
        raise ValueError(f"Invalid corpus stage {role!r}")
    if model_override is not None:
        spec = replace(spec, model=model_override)
    assert_corpus_route(spec.backend, spec.model, role)
    assert_backend_supports_role(spec.backend, role)
    _validate_options("stage", spec)
    if model_override is not None:
        adapter = DEFAULT_ADAPTERS[spec.backend]
        return adapter(model=spec.model, **(spec.options or {})), spec
    return resolver.invoker_for(role), spec
