"""Tests for the role->backend resolver (src/backends/resolver.py).

Validates profile loading, the controlled default and corpus policy, invoker
caching, the Explorer-needs-tools guard, and unimplemented-backend errors.
See docs/MODEL-BACKENDS.md.
"""

from __future__ import annotations

import pytest

from src.backends.base import BACKEND_CAPABILITIES
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.codex import CodexInvoker
from src.backends.bedrock import BedrockInvoker
from src.backends.resolver import (
    ADAPTER_OPTIONS,
    AGENT_ROLES,
    DEFAULT_ADAPTERS,
    SKIM_ROLE, DISTILL_ROLE, CODEX_SEMANTIC_ROLE, EDITOR_ROLE, DREAM_ROLES, resolve_stage,
    Resolver,
    RoleBackend,
    VALID_ROLES,
    default_resolver,
    load_profiles,
)

EVALUATORS = ("skeptic", "advocate", "epistemologist", "methodologist")
SKIM_MODEL = "us.amazon.nova-2-lite-v1:0"
SKIM_OPTIONS = {"temperature": 0.0, "service_tier": "flex", "max_tokens": 4096}


def _skim():
    return RoleBackend(role="skim", backend="bedrock", model=SKIM_MODEL, effort="low", options=dict(SKIM_OPTIONS))


def _profile_rows(role_backend="claude_code", skim_backend="bedrock", skim_model=SKIM_MODEL, skim_options=""):
    """TOML lines for a complete profile: agent roles on ``role_backend``, skim as given."""
    rows = [f'{r} = {{ backend = "{role_backend}", model = "m", effort = "high" }}' for r in AGENT_ROLES]
    rows.append(f'skim = {{ backend = "{skim_backend}", model = "{skim_model}", effort = "low"{skim_options} }}')
    rows.extend(f'{r} = {{ backend = "bedrock", model = "safe-model" }}' for r in (DISTILL_ROLE, CODEX_SEMANTIC_ROLE, EDITOR_ROLE))
    return "\n".join(rows) + "\n"


def _claude(role, effort="high"):
    return RoleBackend(role=role, backend="claude_code", model="global.anthropic.claude-opus-4-8", effort=effort)


class TestLoadProfiles:
    def test_committed_config_parses_with_controlled_default(self):
        profiles, default_profile = load_profiles()  # real config/backends.toml
        assert default_profile == "controlled"
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
        assert p["thinker"].model == "global.anthropic.claude-opus-5"
        panel_models = {p[r].model for r in ("skeptic", "advocate", "epistemologist", "methodologist")}
        assert any("openai." in m for m in panel_models) and any("anthropic." in m for m in panel_models)
        Resolver(p)  # eager Explorer guard passes

    def test_unsafe_hybrid_profile_is_retired(self):
        profiles, _ = load_profiles()
        assert "hybrid" not in profiles

    def test_controlled_profile_avoids_claude_and_fable(self):
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

    def test_laptop_profile_uses_safe_compatibility_route(self):
        """Compatibility: laptop keeps Opus/effort choices on Claude Code."""
        profiles, _ = load_profiles()
        laptop = profiles["laptop"]
        for role in AGENT_ROLES:
            assert laptop[role].backend == "claude_code"
            assert laptop[role].model == "global.anthropic.claude-opus-4-8"
        assert laptop["explorer"].effort == "high"
        assert laptop["thinker"].effort == "max"
        for ev in EVALUATORS:
            assert laptop[ev].effort == "high"


class TestResolver:
    def _laptop(self):
        return {role: _claude(role) for role in AGENT_ROLES} | {"skim": _skim()}

    def test_invoker_for_returns_claude(self):
        r = Resolver(self._laptop())
        inv = r.invoker_for("skeptic")
        assert isinstance(inv, ClaudeCodeInvoker)
        assert inv.model == "global.anthropic.claude-opus-4-8"

    def test_all_claude_profile_shares_one_invoker(self):
        """All agent roles on the same (backend,model) reuse one cached invoker — like today."""
        r = Resolver(self._laptop())
        ids = {id(r.invoker_for(role)) for role in AGENT_ROLES}
        assert len(ids) == 1

    def test_distinct_models_get_distinct_invokers(self):
        profile = {
            "skeptic": RoleBackend("skeptic", "claude_code", "model-a", "high"),
            "advocate": RoleBackend("advocate", "claude_code", "model-b", "high"),
        }
        r = Resolver(profile)
        assert r.invoker_for("skeptic") is not r.invoker_for("advocate")
        assert r.invoker_for("skeptic") is r.invoker_for("skeptic")  # cached

    def test_spec_for_returns_provenance(self):
        r = Resolver(self._laptop())
        spec = r.spec_for("thinker")
        assert (spec.backend, spec.model) == ("claude_code", "global.anthropic.claude-opus-4-8")

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

        profile = {"thinker": RoleBackend("thinker", "bedrock", "global.anthropic.claude-opus-5", "high")}
        r = Resolver(profile)
        inv = r.invoker_for("thinker")
        assert isinstance(inv, BedrockInvoker)
        assert inv.model == "global.anthropic.claude-opus-5"

    def test_backend_without_adapter_raises(self):
        """A backend name with no registered adapter fails loudly at resolution."""
        profile = {"thinker": RoleBackend("thinker", "bedrock", "global.anthropic.claude-opus-5", "max")}
        r = Resolver(profile, adapters={})
        with pytest.raises(NotImplementedError):
            r.invoker_for("thinker")


class TestDefaultResolver:
    def test_default_resolver_uses_controlled(self, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        r = default_resolver()
        assert isinstance(r.invoker_for("skeptic"), BedrockInvoker)

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
        """A typo'd role (all valid roles present + an extra) is rejected as unknown."""
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        cfg = tmp_path / "backends.toml"
        cfg.write_text(
            'default_profile = "p"\n[profiles.p]\n'
            + _profile_rows()
            + 'explrer = { backend = "kiro", model = "m" }\n'
        )
        with pytest.raises(ValueError, match="unknown role"):
            default_resolver(config_path=str(cfg))

    def test_unknown_backend_in_active_profile_rejected(self, tmp_path, monkeypatch):
        """A profile naming a backend that isn't a known capability key (a typo
        like 'kiroo') is rejected with a clear error before the resolver builds."""
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        rows = _profile_rows().replace('skeptic = { backend = "claude_code"', 'skeptic = { backend = "kiroo"')  # one typo
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n' + rows)
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



class TestSkimRole:
    """docs/PLAN-skim-stage.md constraint 1: the skim role is Bedrock-only in every
    profile and can never resolve to Kiro or another agentic CLI; its adapter
    options (temperature 0, Flex tier, 4096 tokens) are expressed in the toml."""

    def test_valid_roles_are_the_agent_roles_plus_skim(self):
        assert DREAM_ROLES == (*AGENT_ROLES, SKIM_ROLE)
        assert VALID_ROLES == (*DREAM_ROLES, DISTILL_ROLE, CODEX_SEMANTIC_ROLE, EDITOR_ROLE)
        assert "skim" not in AGENT_ROLES

    def test_every_committed_profile_runs_the_skim_on_bedrock_nova(self):
        profiles, _ = load_profiles()
        assert set(profiles) == {"laptop", "mini", "bedrock", "controlled"}
        for name, p in profiles.items():
            spec = p["skim"]
            assert spec.backend == "bedrock", name
            assert spec.model == SKIM_MODEL, name
            assert spec.effort == "low", name
            assert spec.options == SKIM_OPTIONS, name
            assert not BACKEND_CAPABILITIES[spec.backend].supports_mcp
            Resolver(p)  # eager guards pass for every committed profile

    def test_controlled_profile_still_avoids_claude_with_the_skim(self):
        profiles, _ = load_profiles()
        p = profiles["controlled"]
        assert all(p[r].backend != "kiro" for r in VALID_ROLES) and p["skim"].backend == "bedrock"

    def test_skim_resolves_to_bedrock_invoker_with_the_options_applied(self):
        from src.backends.bedrock import BedrockInvoker

        r = Resolver({"skim": _skim()})
        inv = r.invoker_for("skim")
        assert isinstance(inv, BedrockInvoker) and inv.model == SKIM_MODEL
        assert inv._temperature == 0.0 and inv._service_tier == "flex" and inv._max_tokens == 4096
        assert r.invoker_for("skim") is inv  # cached

    def test_options_are_part_of_the_cache_key(self):
        """Two roles on the same Bedrock model but different options get distinct invokers."""
        profile = {
            "skim": _skim(),
            "thinker": RoleBackend("thinker", "bedrock", SKIM_MODEL, "high"),
        }
        r = Resolver(profile)
        assert r.invoker_for("skim") is not r.invoker_for("thinker")
        assert r.invoker_for("thinker")._temperature is None

    @pytest.mark.parametrize("backend", ["kiro", "claude_code", "codex"])
    def test_skim_on_an_agentic_cli_is_rejected_at_construction(self, backend):
        profile = {"skim": RoleBackend("skim", backend, "any-model", "low")}
        with pytest.raises(ValueError, match="skim"):
            Resolver(profile)

    def test_skim_on_claude_in_the_active_profile_is_rejected_at_load(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n' + _profile_rows(skim_backend="kiro", skim_model="m"))
        with pytest.raises(ValueError, match="skim"):
            default_resolver(config_path=str(cfg))

    def test_profile_without_skim_is_rejected_at_load(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        rows = "\n".join(f'{r} = {{ backend = "kiro", model = "m", effort = "high" }}' for r in AGENT_ROLES)
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n' + rows + "\n")
        with pytest.raises(ValueError, match="missing required role"):
            default_resolver(config_path=str(cfg))

    def test_lazy_guard_also_rejects_skim_on_agentic_cli(self):
        """Defense in depth: a profile mutated after construction still cannot resolve
        the skim to an agentic CLI."""
        r = Resolver({"skim": _skim()})
        r._profile["skim"] = RoleBackend("skim", "kiro", "claude-opus-4.8", "low")
        with pytest.raises(ValueError):
            r.invoker_for("skim")


class TestAdapterOptions:
    def test_options_parsed_from_toml_and_default_none(self, tmp_path):
        cfg = tmp_path / "backends.toml"
        cfg.write_text(
            'default_profile = "p"\n[profiles.p]\n'
            + _profile_rows(skim_options=', options = { temperature = 0.5, service_tier = "default" }')
        )
        profiles, _ = load_profiles(str(cfg))
        assert profiles["p"]["skim"].options == {"temperature": 0.5, "service_tier": "default"}
        assert profiles["p"]["thinker"].options is None
        assert profiles["p"]["skim"].options_key() == (("service_tier", "default"), ("temperature", 0.5))

    def test_unknown_option_key_rejected_at_load(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n'
                       + _profile_rows(skim_options=", options = { temprature = 0.0 }"))
        with pytest.raises(ValueError, match=r"option\(s\) \['temprature'\]"):
            default_resolver(config_path=str(cfg))

    def test_options_on_a_backend_without_options_rejected_at_load(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_PROFILE", raising=False)
        rows = _profile_rows().replace('thinker = { backend = "claude_code", model = "m", effort = "high" }',
                                       'thinker = { backend = "claude_code", model = "m", effort = "high", options = { temperature = 0.0 } }')
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n' + rows)
        with pytest.raises(ValueError, match="does not accept"):
            default_resolver(config_path=str(cfg))

    def test_options_must_be_a_table(self, tmp_path):
        cfg = tmp_path / "backends.toml"
        cfg.write_text('default_profile = "p"\n[profiles.p]\n' + _profile_rows(skim_options=', options = "flex"'))
        with pytest.raises(ValueError, match="'options' must be a table"):
            load_profiles(str(cfg))

    def test_only_bedrock_accepts_options_today(self):
        assert set(ADAPTER_OPTIONS) == {"bedrock"}
        assert ADAPTER_OPTIONS["bedrock"] == {"temperature", "service_tier", "max_tokens"}

    def test_bad_option_value_fails_at_resolution_not_silently(self):
        """The adapter validates values (e.g. an unknown tier) when the invoker is built."""
        r = Resolver({"skim": RoleBackend("skim", "bedrock", SKIM_MODEL, "low", {"service_tier": "turbo"})})
        with pytest.raises(ValueError, match="service_tier"):
            r.invoker_for("skim")


@pytest.mark.parametrize("role", VALID_ROLES)
def test_programmatic_corpus_routes_reject_kiro_and_fable(role):
    for backend, model in (("kiro", "safe-model"), ("claude_code" if role == "explorer" else "bedrock", "GLOBAL.CLAUDE-FABLE-5-1")):
        with pytest.raises(ValueError):
            Resolver({role: RoleBackend(role, backend, model)})


def test_all_committed_profiles_obey_corpus_boundary():
    profiles, _ = load_profiles()
    for profile in profiles.values():
        for role, spec in profile.items():
            assert spec.backend != "kiro", role
            assert "fable" not in spec.model.lower(), role


def test_denied_override_rejected_before_adapter_constructor(monkeypatch):
    resolver = Resolver({EDITOR_ROLE: RoleBackend(EDITOR_ROLE, "bedrock", SKIM_MODEL)})
    called = []
    monkeypatch.setitem(DEFAULT_ADAPTERS, "bedrock", lambda **kwargs: called.append(kwargs))
    with pytest.raises(ValueError, match="cannot run on model"):
        resolve_stage(EDITOR_ROLE, resolver=resolver, model_override="claude-fable-5-1")
    assert called == []


def test_inactive_unsafe_profile_rejected_at_load(tmp_path):
    config = tmp_path / "backends.toml"
    config.write_text('default_profile="good"\n[profiles.good]\n' + _profile_rows()
                      + '[profiles.bad]\n' + _profile_rows(role_backend="kiro"))
    with pytest.raises(ValueError, match="cannot run on backend"):
        load_profiles(str(config))
