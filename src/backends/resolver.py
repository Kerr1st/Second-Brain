"""Role -> backend resolution for the dream cycle.

Reads the committed per-machine profiles in ``config/backends.toml`` (selected by
the ``SECOND_BRAIN_PROFILE`` env var; unset => the default profile, which
reproduces today's all-Kiro behavior) and hands the orchestrator a concrete
:class:`~src.backends.base.Invoker` per role, plus the resolved
``(backend, model, effort)`` for provenance.

Invokers are cached per ``(backend, model)``, so an all-Kiro profile yields a
single shared KiroInvoker across all roles — identical to today — while a
role-diverse map reuses one client per distinct backend+model. See
``docs/MODEL-BACKENDS.md``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from src.backends.base import BACKEND_CAPABILITIES, Invoker, assert_backend_supports_role
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.codex import CodexInvoker
from src.backends.kiro import KiroInvoker

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "backends.toml")

# backend name -> adapter class. Adapters are registered as they are implemented
# (priority order: kiro today; claude_code / codex landed here; bedrock in a
# later step).
DEFAULT_ADAPTERS: dict[str, type] = {
    "kiro": KiroInvoker,
    "claude_code": ClaudeCodeInvoker,
    "codex": CodexInvoker,
}

VALID_ROLES = (
    "explorer",
    "thinker",
    "skeptic",
    "advocate",
    "epistemologist",
    "methodologist",
)

_REQUIRED_ROLES = frozenset(VALID_ROLES)


def _validate_profile(name: str, role_map: dict[str, "RoleBackend"]) -> None:
    """Fail fast if a configured profile doesn't define exactly the required roles.

    A profile missing a role would otherwise abort the dream cycle **mid-run**
    (after LLM spend) with a bare ``KeyError`` when that role is first resolved;
    an unknown (typo'd) role would silently never be used. Catch both at config
    load, before the orchestrator builds.
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


@dataclass(frozen=True)
class RoleBackend:
    """Resolved execution spec for one role (also the provenance record)."""

    role: str
    backend: str
    model: str
    effort: str | None = None


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
    default_profile = data.get("default_profile", "laptop")
    profiles: dict[str, dict[str, RoleBackend]] = {}
    for pname, roles in data.get("profiles", {}).items():
        role_map: dict[str, RoleBackend] = {}
        for role, spec in roles.items():
            role_map[role] = RoleBackend(
                role=role,
                backend=spec["backend"],
                model=spec["model"],
                effort=spec.get("effort"),
            )
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
        self._profile = profile
        self._adapters = adapters if adapters is not None else DEFAULT_ADAPTERS
        self._cache: dict[tuple[str, str], Invoker] = {}
        # Eager fail-fast: a tool-less Direct-API backend cannot run the Explorer
        # (it needs the live MCP tool-loop). Reject such a profile at construction
        # rather than only when the Explorer is first resolved mid-run. The lazy
        # guard in invoker_for() stays as defense-in-depth for every role.
        if "explorer" in self._profile:
            assert_backend_supports_role(self._profile["explorer"].backend, "explorer")

    def spec_for(self, role: str) -> RoleBackend:
        try:
            return self._profile[role]
        except KeyError:
            raise KeyError(
                f"role {role!r} not configured in the active backend profile"
            ) from None

    def invoker_for(self, role: str) -> Invoker:
        spec = self.spec_for(role)
        # Explorer-needs-tools guard (rejects routing it to a Direct-API backend).
        assert_backend_supports_role(spec.backend, role)
        key = (spec.backend, spec.model)
        if key not in self._cache:
            adapter_cls = self._adapters.get(spec.backend)
            if adapter_cls is None:
                raise NotImplementedError(
                    f"Backend {spec.backend!r} has no adapter yet (role {role!r}). "
                    f"Implemented: {sorted(self._adapters)}. See docs/MODEL-BACKENDS.md."
                )
            self._cache[key] = adapter_cls(model=spec.model)
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
