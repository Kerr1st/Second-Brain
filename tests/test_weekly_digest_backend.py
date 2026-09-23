"""The weekly digest resolves its model through the backend profile (2026-09-23).

The digest used to build a ``KiroInvoker`` directly with ``model="claude-sonnet-5"``,
which kiro-cli 2.22 ignores — Auto routing served ``claude-fable-5.1``, forbidden for
the corpus (docs/PLAN-panel-acceptance.md §3) — and it timed out at 600s anyway. These
tests pin the replacement: profile resolution, the agentic-backend refusal, the spend
cap, the timeout exit, and the unchanged digest structure.

No network and no DB: every invoker here is a fake, and ``run()`` is exercised with
``aggregate``/``create_memory``/``generate_embedding`` patched out. The aggregate
figures are synthetic.
"""

import pytest

import scripts.weekly_digest as wd
from src.backends.cost import SpendCapExceeded, SpendTracker, price_for
from src.backends.resolver import RoleBackend, Resolver


# --- fakes -------------------------------------------------------------------

class FakeInvoker:
    """Records the call and returns a well-formed digest envelope."""

    def __init__(self, model="fake-model", usage=None, raise_exc=None, **options):
        self.model = model
        self.options = options
        self.calls = []
        self._usage = usage if usage is not None else {"inputTokens": 3000, "outputTokens": 400}
        self._raise = raise_exc

    def invoke(self, system_prompt, user_message, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        if self._raise is not None:
            raise self._raise
        return {
            "output": {"title": "Week of backend work", "summary": "Moved the digest off Kiro."},
            "raw": '{"title": "Week of backend work"}',
            "usage": dict(self._usage),
            "usage_source": "real",
        }


def _resolver(backend, model, effort="low", options=None, adapters=None):
    """A real Resolver holding just the digest role. Only works for a compliant
    (Bedrock) backend: Resolver refuses to construct a skim seat on an agentic CLI
    — see ``StubResolver`` for exercising the script's own refusal."""
    spec = RoleBackend(role=wd.DIGEST_ROLE, backend=backend, model=model,
                       effort=effort, options=options)
    return Resolver({wd.DIGEST_ROLE: spec}, adapters=adapters or {backend: FakeInvoker})


class StubResolver:
    """Minimal resolver that will hand back an off-policy spec, so the digest's own
    guard can be tested. The real Resolver cannot be built this way (see
    ``test_real_resolver_also_refuses``), which is the belt to this braces."""

    def __init__(self, backend, model, adapter=None):
        self.spec = RoleBackend(role=wd.DIGEST_ROLE, backend=backend, model=model, effort="low")
        self._adapter = adapter or FakeInvoker

    def spec_for(self, role):
        assert role == wd.DIGEST_ROLE
        return self.spec

    def invoker_for(self, role):
        return self._adapter(model=self.spec.model)


@pytest.fixture
def digest_env(monkeypatch):
    """Patch away the DB/embedding/notification edges of run(); collect the writes."""
    written = []
    monkeypatch.setattr(wd, "get_processed_source_urls", lambda *_a, **_k: set())
    monkeypatch.setattr(wd, "aggregate", lambda days: (
        [("backends", 9), ("digest", 4)],
        [("second-brain", 12)],
        [("kiro_cli_chat", 30), ("article", 5)],
        ["Pin the corpus to Bedrock", "Skim stage is Bedrock-only"],
    ))
    monkeypatch.setattr(wd, "generate_embedding", lambda text: [0.0] * 1024)
    monkeypatch.setattr(wd, "create_memory", lambda **kw: written.append(kw))
    # the best-effort osascript ping must not fire during tests
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)
    return written


# --- profile resolution ------------------------------------------------------

class TestResolvesThroughProfile:

    def test_uses_the_roles_backend_model_and_options(self):
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0", effort="low",
                      options={"temperature": 0.0, "service_tier": "flex", "max_tokens": 4096})
        invoker, spec = wd.resolve_digest_backend(r)
        assert (spec.backend, spec.model, spec.effort) == (
            "bedrock", "us.amazon.nova-2-lite-v1:0", "low")
        assert isinstance(invoker, FakeInvoker)
        assert invoker.model == "us.amazon.nova-2-lite-v1:0"
        assert invoker.options == {"temperature": 0.0, "service_tier": "flex", "max_tokens": 4096}

    def test_digest_role_is_an_existing_role(self):
        from src.backends.resolver import VALID_ROLES
        assert wd.DIGEST_ROLE in VALID_ROLES

    def test_every_committed_profile_gives_the_digest_a_bedrock_seat(self):
        """The real config, all profiles: the digest can never land on Kiro/Fable."""
        from src.backends.resolver import load_profiles
        profiles, _default = load_profiles()
        assert profiles
        for name, roles in profiles.items():
            spec = roles[wd.DIGEST_ROLE]
            assert spec.backend == "bedrock", f"profile {name} would run the digest on {spec.backend}"

    def test_resolved_model_has_a_known_list_price(self):
        """An unpriced id is billed at the $10/$50 ceiling — the default must not be."""
        from src.backends.resolver import load_profiles
        profiles, _ = load_profiles()
        model = profiles["controlled"][wd.DIGEST_ROLE].model
        assert price_for(model).known
        assert price_for(model).input_per_m < 1.0    # a cheap seat, not an Opus/Fable one

    def test_honours_second_brain_profile(self, monkeypatch):
        from src.backends.resolver import default_resolver
        monkeypatch.setenv("SECOND_BRAIN_PROFILE", "controlled")
        assert default_resolver().spec_for(wd.DIGEST_ROLE).backend == "bedrock"
        monkeypatch.setenv("SECOND_BRAIN_PROFILE", "does-not-exist")
        with pytest.raises(KeyError):
            wd.resolve_digest_backend()

    def test_model_override_keeps_the_resolved_backend(self):
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0")
        invoker, spec = wd.resolve_digest_backend(r, model_override="global.anthropic.claude-sonnet-5")
        assert spec.backend == "bedrock"                      # backend is NOT overridable
        assert spec.model == "global.anthropic.claude-sonnet-5"
        assert invoker.model == "global.anthropic.claude-sonnet-5"


# --- the Kiro / Fable guard --------------------------------------------------

class TestRefusesAgenticBackends:

    @pytest.mark.parametrize("backend,model", [
        ("kiro", "claude-sonnet-5"),
        ("claude_code", "global.anthropic.claude-sonnet-5"),
        ("codex", "gpt-6-astra"),
    ])
    def test_mcp_capable_backends_are_refused(self, backend, model):
        with pytest.raises(wd.DigestBackendError) as exc:
            wd.resolve_digest_backend(StubResolver(backend, model))
        msg = str(exc.value)
        assert backend in msg
        assert "fable" in msg.lower()
        assert "controlled" in msg

    @pytest.mark.parametrize("backend", ["kiro", "claude_code", "codex"])
    def test_real_resolver_also_refuses(self, backend):
        """Config-load guard: such a profile cannot even be built."""
        spec = RoleBackend(role=wd.DIGEST_ROLE, backend=backend, model="m", effort="low")
        with pytest.raises(ValueError):
            Resolver({wd.DIGEST_ROLE: spec}, adapters={backend: FakeInvoker})

    def test_refusal_happens_before_any_model_call(self, digest_env):
        called = []
        r = StubResolver("kiro", "claude-sonnet-5",
                         adapter=lambda **kw: called.append(kw))
        with pytest.raises(wd.DigestBackendError):
            wd.run(days=7, dry_run=True, resolver=r)
        assert called == []
        assert digest_env == []

    def test_main_exits_non_zero_on_refusal(self, digest_env, monkeypatch):
        r = StubResolver("kiro", "claude-sonnet-5")
        monkeypatch.setattr(wd, "default_resolver", lambda *a, **k: r)
        monkeypatch.setattr("sys.argv", ["weekly_digest.py"])
        assert wd.main() == 2
        assert digest_env == []


# --- spend cap ---------------------------------------------------------------

class TestSpendCap:

    def test_default_cap_and_env_override(self, monkeypatch):
        monkeypatch.delenv(wd.DIGEST_CAP_ENV, raising=False)
        assert wd.digest_cap_usd() == wd.DEFAULT_DIGEST_CAP_USD == 0.25
        monkeypatch.setenv(wd.DIGEST_CAP_ENV, "0.05")
        assert wd.digest_cap_usd() == 0.05

    @pytest.mark.parametrize("bad", ["nope", "0", "-3"])
    def test_bad_cap_falls_back_to_the_default(self, monkeypatch, bad):
        monkeypatch.setenv(wd.DIGEST_CAP_ENV, bad)
        assert wd.digest_cap_usd() == wd.DEFAULT_DIGEST_CAP_USD

    def test_call_is_metered_against_the_cap(self, digest_env):
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0")
        tracker = SpendTracker(cap_usd=0.25)
        wd.run(days=7, dry_run=True, resolver=r, tracker=tracker)
        assert tracker.calls == 1
        assert 0 < tracker.total_usd < 0.25

    def test_exceeding_the_cap_aborts_before_the_memory_is_written(self, digest_env):
        expensive = {"inputTokens": 2_000_000, "outputTokens": 500_000}
        r = _resolver("bedrock", "global.anthropic.claude-fable-5-1",
                      adapters={"bedrock": lambda **kw: FakeInvoker(usage=expensive, **kw)})
        with pytest.raises(SpendCapExceeded):
            wd.run(days=7, resolver=r, tracker=SpendTracker(cap_usd=0.25))
        assert digest_env == []   # nothing stored

    def test_main_exits_non_zero_on_cap(self, digest_env, monkeypatch):
        expensive = {"inputTokens": 2_000_000, "outputTokens": 500_000}
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0",
                      adapters={"bedrock": lambda **kw: FakeInvoker(usage=expensive, **kw)})
        monkeypatch.setattr(wd, "default_resolver", lambda *a, **k: r)
        monkeypatch.setenv(wd.DIGEST_CAP_ENV, "0.01")
        monkeypatch.setattr("sys.argv", ["weekly_digest.py"])
        assert wd.main() == 3
        assert digest_env == []


# --- timeout -----------------------------------------------------------------

class TestTimeout:

    def test_default_timeout_is_sized_for_a_direct_api_call(self):
        assert 60 <= wd.DIGEST_TIMEOUT <= 300          # not the old 600s kiro-cli ceiling
        assert wd.DIGEST_TIMEOUT != 600

    def test_timeout_is_passed_to_the_invoker(self, digest_env):
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0")
        invoker, _ = wd.resolve_digest_backend(r)
        wd.run(days=7, dry_run=True, resolver=r, tracker=SpendTracker(cap_usd=1.0))
        call = invoker.calls[-1]
        assert call["timeout"] == wd.DIGEST_TIMEOUT
        assert call["tools"] is False               # never a tool loop
        assert call["stage"] == "weekly_digest"

    def test_timeout_is_a_loud_non_zero_exit(self, digest_env, monkeypatch):
        boom = TimeoutError("Agent subprocess timed out")
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0",
                      adapters={"bedrock": lambda **kw: FakeInvoker(raise_exc=boom, **kw)})
        monkeypatch.setattr(wd, "default_resolver", lambda *a, **k: r)
        monkeypatch.setattr("sys.argv", ["weekly_digest.py"])
        assert wd.main() == 4
        assert digest_env == []


# --- unchanged digest structure ---------------------------------------------

class TestDigestStructureUnchanged:

    def test_dry_run_body_anchors_the_meta_question(self, digest_env):
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0")
        body = wd.run(days=7, dry_run=True, resolver=r, tracker=SpendTracker(cap_usd=1.0))
        assert body.startswith("What I've been spending my time working on and learning")
        assert "(last 7-day digest)" in body
        assert body.endswith("Moved the digest off Kiro.")
        assert digest_env == []

    def test_stored_memory_fields(self, digest_env):
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0")
        wd.run(days=7, resolver=r, tracker=SpendTracker(cap_usd=1.0))
        assert len(digest_env) == 1
        kw = digest_env[0]
        assert kw["type"] == "synthesis"
        assert kw["source_type"] == wd.SOURCE_TYPE == "weekly_digest"
        assert kw["source_url"].startswith("digest://")
        assert kw["tags"] == ["digest", "weekly"]
        assert kw["confidence"] == 0.85
        assert kw["mem_class"] == "semantic"
        assert kw["metadata"]["days"] == 7
        assert kw["title"] == "Week of backend work"
        assert len(kw["embedding"]) == 1024

    def test_prompt_and_user_message_are_unchanged_in_shape(self):
        msg = wd.build_user_message(
            7, [("backends", 9)], [("second-brain", 12)], [("article", 5)], ["A decision"])
        assert "Window: last 7 days." in msg
        assert "Top topic tags: backends (9)" in msg
        assert "Active projects: second-brain (12)" in msg
        assert "Channel activity: article (5)" in msg
        assert "- A decision" in msg
        assert "JSON object" in wd.SYSTEM_PROMPT

    def test_empty_window_is_a_no_op_with_no_model_call(self, monkeypatch, digest_env):
        monkeypatch.setattr(wd, "aggregate", lambda days: ([], [], [], []))
        called = []
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0",
                      adapters={"bedrock": lambda **kw: called.append(kw)})
        assert wd.run(days=7, resolver=r) is None
        assert called == [] and digest_env == []

    def test_already_present_digest_is_skipped(self, monkeypatch, digest_env):
        import datetime
        y, w, _ = datetime.date.today().isocalendar()
        monkeypatch.setattr(wd, "get_processed_source_urls",
                            lambda *_a, **_k: {f"digest://{y}-W{w:02d}"})
        called = []
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0",
                      adapters={"bedrock": lambda **kw: called.append(kw)})
        assert wd.run(days=7, resolver=r) is None
        assert called == []

    def test_missing_summary_writes_nothing(self, digest_env):
        class NoSummary(FakeInvoker):
            def invoke(self, *a, **k):
                return {"output": {"title": "t"}, "raw": "junk", "usage": None,
                        "usage_source": "real"}
        r = _resolver("bedrock", "us.amazon.nova-2-lite-v1:0",
                      adapters={"bedrock": lambda **kw: NoSummary(**kw)})
        assert wd.run(days=7, resolver=r, tracker=SpendTracker(cap_usd=1.0)) is None
        assert digest_env == []


def test_no_direct_kiro_invoker_remains():
    """The regression itself: the module must not import or construct a Kiro invoker.

    (The module docstring names ``KiroInvoker`` when recording why it was removed, so
    this checks for the import and the construction, not the bare word.)
    """
    import inspect
    src = inspect.getsource(wd)
    assert "from src.backends.kiro import" not in src
    assert "KiroInvoker(" not in src
