"""Tests for the role->backend resolver (src/backends/resolver.py).

Validates profile loading, the all-Kiro default (default-preserving), invoker
caching, the Explorer-needs-tools guard, and unimplemented-backend errors.
See docs/MODEL-BACKENDS.md.
"""

from __future__ import annotations

import pytest

from src.backends.base import BACKEND_CAPABILITIES
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.codex import CodexInvoker
from src.backends.kiro import KiroInvoker
from src.backends.resolver import (
    DEFAULT_ADAPTERS,
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
        assert {"laptop", "mini", "bedrock"} <= set(profiles)
        assert set(profiles["laptop"]) >= set(VALID_ROLES)

    def test_bedrock_profile_is_direct_api_with_mixed_panel(self):
        """The committed `bedrock` profile: Thinker + evaluators on the direct
        Bedrock API, Explorer on a tool-capable backend (the resolver guard), and
        both model families on the panel so 3-of-4 needs cross-family agreement."""
        profiles, _ = load_profiles()
        p = profiles["bedrock"]
        assert p["explorer"].backend == "claude_code"
        for role in ("thinker", "skeptic", "advocate", "epistemologist", "methodologist"):
            assert p[role].backend == "bedrock"
        assert p["thinker"].model == "global.anthropic.claude-fable-5-1"
        panel_models = {p[r].model for r in ("skeptic", "advocate", "epistemologist", "methodologist")}
        assert any("openai." in m for m in panel_models) and any("anthropic." in m for m in panel_models)
        Resolver(p)  # eager Explorer guard passes

    def test_hybrid_profile_meters_only_the_astra_seats(self):
        """`hybrid`: Claude roles on Kiro (credits), only the two Astra evaluator
        seats on Bedrock. Never Kiro's internal Fable preview for this workload."""
        profiles, _ = load_profiles()
        p = profiles["hybrid"]
        metered = {r for r in VALID_ROLES if BACKEND_CAPABILITIES[p[r].backend].metered}
        assert metered == {"skeptic", "epistemologist"}
        assert all(p[r].model == "global.openai.gpt-6-astra" for r in metered)
        assert all(p[r].backend == "kiro" for r in VALID_ROLES if r not in metered)
        assert not any("fable" in p[r].model for r in VALID_ROLES)
        Resolver(p)

    def test_controlled_profile_avoids_kiro_and_fable(self):
        """`controlled` (nightly default since 2026-09-17): no role on Kiro — its
        server-side "Auto" routing served claude-fable-5.1 regardless of --model —
        and no Fable anywhere. Explorer keeps tools (Claude Code); Thinker is
        tool-less on direct Bedrock (it gets the slice packet inline); the panel
        keeps two model families so 3-of-4 needs both Claude and Astra."""
        profiles, _ = load_profiles()
        p = profiles["controlled"]
        assert all(p[r].backend != "kiro" for r in VALID_ROLES)
        assert not any("fable" in p[r].model for r in VALID_ROLES)
        assert BACKEND_CAPABILITIES[p["explorer"].backend].supports_mcp
        assert p["thinker"].backend == "bedrock"
        assert {p[r].model for r in ("skeptic", "epistemologist")} == {"global.openai.gpt-6-astra"}
        assert all("claude" in p[r].model for r in ("advocate", "methodologist"))
        Resolver(p)

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

    def test_bedrock_role_resolves_to_adapter(self):
        """A profile selecting bedrock resolves to BedrockInvoker with the model id
        preserved (the metered adapter rejects a blank id, so a real id is used)."""
        from src.backends.bedrock import BedrockInvoker

        profile = {"thinker": RoleBackend("thinker", "bedrock", "global.anthropic.claude-fable-5-1", "high")}
        r = Resolver(profile)
        inv = r.invoker_for("thinker")
        assert isinstance(inv, BedrockInvoker)
        assert inv.model == "global.anthropic.claude-fable-5-1"

    def test_backend_without_adapter_raises(self):
        """A backend name with no registered adapter fails loudly at resolution."""
        profile = {"thinker": RoleBackend("thinker", "bedrock", "global.anthropic.claude-fable-5-1", "max")}
        r = Resolver(profile, adapters={})
        with pytest.raises(NotImplementedError):
            r.invoker_for("thinker")


class TestDefaultResolver:
    def test_default_resolver_uses_laptop(self, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        r = default_resolver()
        assert isinstance(r.invoker_for("skeptic"), KiroInvoker)

    def test_profile_selected_by_env(self, monkeypatch):
        """mini routes to claude_code, whose adapter is now registered."""
        monkeypatch.setenv("SECOND_BRAIN_PROFILE", "mini")
        r = default_resolver()
        assert isinstance(r.invoker_for("thinker"), ClaudeCodeInvoker)

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


class TestNewAdaptersRegistered:
    """Task 6 / Requirement 19: claude_code + codex are registered adapters."""

    def test_default_adapters_map_claude_code(self):
        # Req 19.1
        assert DEFAULT_ADAPTERS["claude_code"] is ClaudeCodeInvoker

    def test_default_adapters_map_codex(self):
        # Req 19.2
        assert DEFAULT_ADAPTERS["codex"] is CodexInvoker

    def test_default_adapters_map_bedrock(self):
        from src.backends.bedrock import BedrockInvoker

        assert DEFAULT_ADAPTERS["bedrock"] is BedrockInvoker

    def test_claude_code_role_resolves_to_adapter(self):
        """A profile selecting claude_code resolves to ClaudeCodeInvoker, not
        NotImplementedError (Req 19.1). A real model id is required because the
        metered adapter rejects a blank id at construction."""
        profile = {"thinker": RoleBackend("thinker", "claude_code", "claude-opus-4.8", "max")}
        r = Resolver(profile)
        inv = r.invoker_for("thinker")
        assert isinstance(inv, ClaudeCodeInvoker)
        assert inv.model == "claude-opus-4.8"

    def test_codex_role_resolves_to_adapter(self):
        """A profile selecting codex resolves to CodexInvoker, not
        NotImplementedError (Req 19.2)."""
        profile = {"thinker": RoleBackend("thinker", "codex", "gpt-5-codex", "high")}
        r = Resolver(profile)
        inv = r.invoker_for("thinker")
        assert isinstance(inv, CodexInvoker)
        assert inv.model == "gpt-5-codex"

    def test_claude_code_invoker_cached_per_backend_model(self):
        """Repeated resolution of the same (backend, model) reuses one invoker
        (Req 19.3)."""
        profile = {
            "thinker": RoleBackend("thinker", "claude_code", "claude-opus-4.8", "max"),
            "skeptic": RoleBackend("skeptic", "claude_code", "claude-opus-4.8", "high"),
        }
        r = Resolver(profile)
        assert r.invoker_for("thinker") is r.invoker_for("thinker")  # cached per role
        # same (backend, model) across roles shares one cached invoker
        assert r.invoker_for("thinker") is r.invoker_for("skeptic")

    def test_codex_invoker_cached_per_backend_model(self):
        # Req 19.3
        profile = {"thinker": RoleBackend("thinker", "codex", "gpt-5-codex", "high")}
        r = Resolver(profile)
        assert r.invoker_for("thinker") is r.invoker_for("thinker")

    def test_distinct_models_on_claude_code_get_distinct_invokers(self):
        """Cache key is (backend, model): distinct models => distinct invokers
        (Req 19.3)."""
        profile = {
            "thinker": RoleBackend("thinker", "claude_code", "claude-opus-4.8", "max"),
            "skeptic": RoleBackend("skeptic", "claude_code", "claude-sonnet-4", "high"),
        }
        r = Resolver(profile)
        assert r.invoker_for("thinker") is not r.invoker_for("skeptic")

    def test_explorer_on_claude_code_passes_guard_at_construction(self):
        """Explorer on claude_code passes assert_backend_supports_role because
        claude_code declares supports_mcp=True (Req 19.4)."""
        profile = {"explorer": RoleBackend("explorer", "claude_code", "claude-opus-4.8", "high")}
        r = Resolver(profile)  # must not raise
        assert isinstance(r.invoker_for("explorer"), ClaudeCodeInvoker)

    def test_explorer_on_codex_passes_guard_at_construction(self):
        """Explorer on codex passes the guard because codex declares
        supports_mcp=True (Req 19.4)."""
        profile = {"explorer": RoleBackend("explorer", "codex", "gpt-5-codex", "high")}
        r = Resolver(profile)  # must not raise
        assert isinstance(r.invoker_for("explorer"), CodexInvoker)
