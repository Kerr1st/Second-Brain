"""Tests for the role->backend resolver (src/backends/resolver.py).

Validates profile loading, the all-Kiro default (default-preserving), invoker
caching, the Explorer-needs-tools guard, and unimplemented-backend errors.
See docs/MODEL-BACKENDS.md.
"""

from __future__ import annotations

import pytest

from src.backends.kiro import KiroInvoker
from src.backends.resolver import (
    Resolver,
    RoleBackend,
    VALID_ROLES,
    default_resolver,
    load_profiles,
)

EVALUATORS = ("skeptic", "advocate", "epistemologist", "methodologist")


def _kiro(role, effort="high"):
    return RoleBackend(role=role, backend="kiro", model="claude-opus-4.8", effort=effort)


class TestLoadProfiles:
    def test_committed_config_parses_with_laptop_default(self):
        profiles, default_profile = load_profiles()  # real config/backends.toml
        assert default_profile == "laptop"
        assert {"laptop", "mini"} <= set(profiles)
        assert set(profiles["laptop"]) >= set(VALID_ROLES)

    def test_laptop_profile_reproduces_today(self):
        """Default-preserving: laptop = all-Kiro/Opus; explorer+evaluators high, thinker max."""
        profiles, _ = load_profiles()
        laptop = profiles["laptop"]
        for role in VALID_ROLES:
            assert laptop[role].backend == "kiro"
            assert laptop[role].model == "claude-opus-4.8"
        assert laptop["explorer"].effort == "high"
        assert laptop["thinker"].effort == "max"
        for ev in EVALUATORS:
            assert laptop[ev].effort == "high"


class TestResolver:
    def _laptop(self):
        return {role: _kiro(role) for role in VALID_ROLES}

    def test_invoker_for_returns_kiro(self):
        r = Resolver(self._laptop())
        inv = r.invoker_for("skeptic")
        assert isinstance(inv, KiroInvoker)
        assert inv.model == "claude-opus-4.8"

    def test_all_kiro_profile_shares_one_invoker(self):
        """All roles on the same (backend,model) reuse one cached invoker — like today."""
        r = Resolver(self._laptop())
        ids = {id(r.invoker_for(role)) for role in VALID_ROLES}
        assert len(ids) == 1

    def test_distinct_models_get_distinct_invokers(self):
        profile = {
            "skeptic": RoleBackend("skeptic", "kiro", "model-a", "high"),
            "advocate": RoleBackend("advocate", "kiro", "model-b", "high"),
        }
        r = Resolver(profile)
        assert r.invoker_for("skeptic") is not r.invoker_for("advocate")
        assert r.invoker_for("skeptic") is r.invoker_for("skeptic")  # cached

    def test_spec_for_returns_provenance(self):
        r = Resolver(self._laptop())
        spec = r.spec_for("thinker")
        assert (spec.backend, spec.model) == ("kiro", "claude-opus-4.8")

    def test_unknown_role_raises(self):
        r = Resolver(self._laptop())
        with pytest.raises(KeyError):
            r.invoker_for("nonexistent_role")

    def test_explorer_on_toolless_backend_rejected_at_construction(self):
        """Eager guard: a profile routing the Explorer to a tool-less Direct-API
        backend fails fast at Resolver construction, not mid-run."""
        profile = {"explorer": RoleBackend("explorer", "bedrock", "claude-opus-4.8", "high")}
        with pytest.raises(ValueError):
            Resolver(profile)

    def test_unimplemented_backend_raises(self):
        profile = {"thinker": RoleBackend("thinker", "claude_code", "claude-opus-4.8", "max")}
        r = Resolver(profile)  # default adapters = {kiro} only
        with pytest.raises(NotImplementedError):
            r.invoker_for("thinker")


class TestDefaultResolver:
    def test_default_resolver_uses_laptop(self, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        r = default_resolver()
        assert isinstance(r.invoker_for("skeptic"), KiroInvoker)

    def test_profile_selected_by_env(self, monkeypatch):
        """mini routes to claude_code, whose adapter isn't implemented yet."""
        monkeypatch.setenv("SECOND_BRAIN_PROFILE", "mini")
        r = default_resolver()
        with pytest.raises(NotImplementedError):
            r.invoker_for("thinker")

    def test_unknown_profile_raises(self, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_PROFILE", "nonexistent")
        with pytest.raises(KeyError):
            default_resolver()

    def test_incomplete_active_profile_rejected(self, tmp_path, monkeypatch):
        """Fail fast: an active profile missing a role is rejected at load, before
        any run / LLM spend — not mid-run with a bare KeyError."""
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        cfg = tmp_path / "backends.toml"
        cfg.write_text(
            'default_profile = "p"\n'
            "[profiles.p]\n"
            'explorer = { backend = "kiro", model = "m", effort = "high" }\n'
        )
        with pytest.raises(ValueError, match="missing required role"):
            default_resolver(config_path=str(cfg))

    def test_typoed_role_in_active_profile_rejected(self, tmp_path, monkeypatch):
        """A typo'd role (all six valid present + an extra) is rejected as unknown."""
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        rows = "\n".join(
            f'{r} = {{ backend = "kiro", model = "m", effort = "high" }}'
            for r in VALID_ROLES
        )
        cfg = tmp_path / "backends.toml"
        cfg.write_text(
            'default_profile = "p"\n[profiles.p]\n'
            + rows
            + '\nexplrer = { backend = "kiro", model = "m" }\n'
        )
        with pytest.raises(ValueError, match="unknown role"):
            default_resolver(config_path=str(cfg))

    def test_unknown_backend_in_active_profile_rejected(self, tmp_path, monkeypatch):
        """A profile naming a backend that isn't a known capability key (a typo
        like 'kiroo') is rejected with a clear error before the resolver builds."""
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        rows = []
        for r in VALID_ROLES:
            backend = "kiroo" if r == "skeptic" else "kiro"  # one typo'd backend
            rows.append(f'{r} = {{ backend = "{backend}", model = "m", effort = "high" }}')
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n' + "\n".join(rows) + "\n")
        with pytest.raises(ValueError, match="unknown backend"):
            default_resolver(config_path=str(cfg))
