"""Every spelling of a Fable model id is denied for every corpus role (review S3-1).

The rule in ``src/backends/base.py`` is a case-insensitive ``"fable"`` substring
match. These tests pin it against the ids that actually occur in the wild — the
bare and dotted names Kiro/Claude Code print, the Bedrock inference-profile forms
(``global.``/``us.`` prefixes, ``-5-1`` suffix, ``:0`` version) and upper-case —
so a narrower needle (e.g. ``"fable-5-1"``) cannot slip through. Each variant is
checked at the three enforcement points that precede any content leaving the
process: ``assert_corpus_route``, programmatic ``Resolver`` construction, and a
``model_override`` on ``resolve_stage``.
"""

from __future__ import annotations

import pytest

from src.backends.base import CORPUS_ROLES, CorpusRoutingError, assert_corpus_route
from src.backends.resolver import DEFAULT_ADAPTERS, Resolver, RoleBackend, resolve_stage

FABLE_VARIANTS = (
    "claude-fable-5",
    "claude-fable-5.1",
    "global.anthropic.claude-fable-5-1",
    "CLAUDE-FABLE-5",
    "us.anthropic.Claude-Fable-5:0",
)


def _backend_for(role: str) -> str:
    # The Explorer needs tools, so its realistic backend is Claude Code; every
    # other corpus role is exercised on direct Bedrock (the only backend the
    # Bedrock-only roles accept), so the fable check is what must fire.
    return "claude_code" if role == "explorer" else "bedrock"


@pytest.mark.parametrize("model", FABLE_VARIANTS)
@pytest.mark.parametrize("role", sorted(CORPUS_ROLES))
def test_assert_corpus_route_denies_every_fable_spelling(role, model):
    with pytest.raises(CorpusRoutingError, match="cannot run on model"):
        assert_corpus_route(_backend_for(role), model, role)


@pytest.mark.parametrize("model", FABLE_VARIANTS)
@pytest.mark.parametrize("backend", ("bedrock", "claude_code", "codex"))
def test_fable_is_denied_on_every_backend_for_the_explorer(backend, model):
    with pytest.raises(CorpusRoutingError):
        assert_corpus_route(backend, model, "explorer")


@pytest.mark.parametrize("model", FABLE_VARIANTS)
@pytest.mark.parametrize("role", sorted(CORPUS_ROLES))
def test_programmatic_profile_with_fable_is_rejected_at_construction(role, model):
    with pytest.raises(CorpusRoutingError):
        Resolver({role: RoleBackend(role, _backend_for(role), model)})


@pytest.mark.parametrize("model", FABLE_VARIANTS)
@pytest.mark.parametrize("role", sorted(CORPUS_ROLES))
def test_fable_override_is_rejected_before_any_adapter_is_built(role, model, monkeypatch):
    safe_model = "global.anthropic.claude-sonnet-5"
    resolver = Resolver({role: RoleBackend(role, _backend_for(role), safe_model)})
    built = []
    for backend in DEFAULT_ADAPTERS:
        monkeypatch.setitem(DEFAULT_ADAPTERS, backend, lambda **kwargs: built.append(kwargs))
    with pytest.raises(CorpusRoutingError):
        resolve_stage(role, resolver=resolver, model_override=model)
    assert built == []


def test_safe_ids_that_merely_resemble_fable_are_not_denied():
    # Guards against an over-broad needle: "fab"/"able" alone must not deny.
    for model in ("global.anthropic.claude-sonnet-5", "us.amazon.nova-2-lite-v1:0", "stable-diffusion-xl"):
        assert_corpus_route("bedrock", model, "thinker")
