"""Dedicated ``grounding_auditor`` role (routing lane, PLAN item 6).

It must resolve, in every committed profile, to exactly what the skeptic seat
resolves to (same backend/model/effort/options and therefore the same cached
invoker), sit under the corpus Kiro/Fable guard, be metered, and never leak its
findings into a panel seat's prompt. No model is invoked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.backends import cost
from src.backends.base import CORPUS_ROLES, CorpusRoutingError, GROUNDING_AUDITOR_ROLE, corpus_role_for_stage
from src.backends.resolver import DREAM_ROLES, VALID_ROLES, Resolver, RoleBackend, load_profiles
from src.dream_cycle.budget import GROUNDING_AUDIT_ROLE, _RESERVE_FLOOR_USD

PROFILES, _DEFAULT = load_profiles()


class _Fake:
    BACKEND_NAME = "bedrock"

    def __init__(self, model=None, **options):
        self.model, self.options = model, options


def test_grounding_stage_uses_the_dedicated_role():
    assert GROUNDING_AUDIT_ROLE == GROUNDING_AUDITOR_ROLE == "grounding_auditor"
    assert GROUNDING_AUDITOR_ROLE in CORPUS_ROLES and GROUNDING_AUDITOR_ROLE in VALID_ROLES
    assert GROUNDING_AUDITOR_ROLE in DREAM_ROLES  # recorded in run provenance
    assert corpus_role_for_stage("grounding_audit") == GROUNDING_AUDITOR_ROLE
    assert _RESERVE_FLOOR_USD[GROUNDING_AUDITOR_ROLE] == _RESERVE_FLOOR_USD["skeptic"]


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_auditor_resolves_exactly_like_the_skeptic_in_every_profile(name):
    profile = PROFILES[name]
    a, s = profile[GROUNDING_AUDITOR_ROLE], profile["skeptic"]
    assert (a.backend, a.model, a.effort, a.options) == (s.backend, s.model, s.effort, s.options)
    resolver = Resolver(profile, adapters={a.backend: _Fake, "claude_code": _Fake, "bedrock": _Fake})
    assert resolver.invoker_for(GROUNDING_AUDITOR_ROLE) is resolver.invoker_for("skeptic")


def test_committed_profiles_must_name_the_role(tmp_path):
    text = open("config/backends.toml", encoding="utf-8").read()
    stripped = "\n".join(l for l in text.splitlines() if not l.startswith("grounding_auditor "))
    path = tmp_path / "b.toml"
    path.write_text(stripped, encoding="utf-8")
    with pytest.raises(ValueError, match="grounding_auditor"):
        load_profiles(str(path))


def test_partial_programmatic_profile_inherits_the_skeptic_route():
    seat = RoleBackend("skeptic", "bedrock", "global.openai.gpt-6-astra", "high", {"max_tokens": 8192})
    resolver = Resolver({"skeptic": seat}, adapters={"bedrock": _Fake})
    spec = resolver.spec_for(GROUNDING_AUDITOR_ROLE)
    assert spec.role == GROUNDING_AUDITOR_ROLE and spec.model == seat.model and spec.options == seat.options


@pytest.mark.parametrize("backend,model", [("kiro", "claude-opus-5"), ("bedrock", "global.anthropic.claude-fable-5-1")])
def test_auditor_is_under_the_corpus_guard(backend, model):
    with pytest.raises(CorpusRoutingError):
        Resolver({GROUNDING_AUDITOR_ROLE: RoleBackend(GROUNDING_AUDITOR_ROLE, backend, model)})


def test_auditor_calls_are_metered():
    spec = RoleBackend(GROUNDING_AUDITOR_ROLE, "bedrock", "global.openai.gpt-6-astra", "high")
    invoker = MagicMock(model=spec.model, BACKEND_NAME="bedrock")
    invoker.invoke.return_value = {"output": {}, "raw": "{}", "usage_source": "real",
                                   "usage": {"input_tokens": 1000, "output_tokens": 100}}
    tracker = cost.SpendTracker(cap_usd=5.0)
    cost.invoke_metered(invoker, spec, tracker, "sys", "msg", stage="grounding_audit")
    assert tracker.calls == 1 and tracker.total_usd > 0


def test_audit_findings_never_reach_a_panel_seat():
    from src.dream_cycle.orchestrator import DreamCycleOrchestrator
    from src.models import CandidateInsight
    orch = DreamCycleOrchestrator()
    seat = RoleBackend("skeptic", "bedrock", "global.openai.gpt-6-astra", "high")
    orch.resolver = Resolver({"skeptic": seat, "advocate": RoleBackend("advocate", "bedrock", "global.anthropic.claude-sonnet-5", "high")},
                             adapters={"bedrock": _Fake})
    orch._slice_memory_cache = {}
    orch._current_run_id = None
    seen = []

    def fake_metered(role, **kw):
        seen.append((role, kw["stage"], kw["system_prompt"]))
        return {"output": {"verdict": "ACCEPT", "score": 80, "reasoning": "r"}}

    orch._invoke_metered = fake_metered
    cand = CandidateInsight(title="T", content="C", type="insight", operation="CREATE", source_memories=[],
                            confidence="high", confidence_reasoning="r",
                            revision={"blocking_issues": ["SECRET-AUDIT-FINDING"]})
    orch.invoke_evaluator(cand, GROUNDING_AUDIT_ROLE, stage="grounding_audit")
    orch.invoke_evaluator(cand, "advocate")
    (audit_role, audit_stage, _), (panel_role, panel_stage, panel_prompt) = seen
    assert (audit_role, audit_stage) == (GROUNDING_AUDITOR_ROLE, "grounding_audit")
    assert (panel_role, panel_stage) == ("advocate", "evaluator:advocate")
    assert "SECRET-AUDIT-FINDING" not in panel_prompt
