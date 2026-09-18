"""Hermetic tests for scripts/panel_replay.py (fake invoker, no DB, no model calls)."""
import json
import sys
import types
import uuid
from unittest.mock import MagicMock

import pytest

from scripts import panel_replay as pr
from src.backends.cost import SpendCapExceeded, SpendTracker
from src.backends.resolver import RoleBackend


def _row(title="T", verdicts=("REJECT", "ACCEPT", "REJECT", "ACCEPT"), sources=None):
    return {"id": str(uuid.uuid4()), "run_id": "run-1",
            "candidate_json": {"title": title, "type": "insight", "operation": "CREATE", "content": "WHAT: x",
                               "source_memories": sources or [str(uuid.uuid4())], "confidence": "high",
                               "strategy_that_found_it": "arrivals_synthesis", "unknown_field": "ignored"},
            "evaluator_a_verdict": verdicts[0], "evaluator_b_verdict": verdicts[1],
            "evaluator_c_verdict": verdicts[2], "evaluator_d_verdict": verdicts[3], "final_verdict": "REJECTED"}


class _FakeResolver:
    def __init__(self, verdict_by_seat):
        self.specs = {s: RoleBackend(role=s, backend="bedrock", model=f"model-{s}", effort="high") for s in pr.SEATS}
        self.calls = []
        self._verdicts = verdict_by_seat

    def spec_for(self, role):
        return self.specs[role]

    def invoker_for(self, role):
        inv = MagicMock()
        def invoke(system_prompt, user_message, effort=None, stage=None, schema=None, **kw):
            self.calls.append({"role": role, "prompt": system_prompt, "effort": effort, "schema": schema})
            return {"output": {"verdict": self._verdicts[role], "reasoning": f"{role} says so"},
                    "usage": {"input_tokens": 1000, "output_tokens": 100}, "raw": ""}
        inv.invoke = invoke
        return inv


class TestCandidateReconstruction:
    def test_unknown_fields_dropped_and_overrides_applied(self):
        row = _row(title="Every message says no action")
        cand = pr.candidate_from_row(row, {"title": "In the three captured notifications, none flagged the deadline",
                                           "content": "WHAT: scoped", "strategy_that_found_it": "hacked"})
        assert cand.title.startswith("In the three")
        assert cand.content == "WHAT: scoped"
        assert cand.strategy_that_found_it == "arrivals_synthesis"          # not an overridable field

    def test_sources_text_reproduces_live_rendering(self):
        mid = str(uuid.uuid4())
        cand = pr.candidate_from_row(_row(sources=[mid]))
        loader = lambda ids: [{"id": mid, "title": "Slack: DM — 2026-09-15", "content": "hello", "type": "source",
                               "source_type": "slack", "created_at": "2026-09-15"}]
        text = pr.sources_text_for(cand, loader=loader)
        assert text.startswith(f"Source memories: ['{mid}']")
        assert "Slack: DM — 2026-09-15" in text and "hello" in text


class TestReplay:
    def test_matrix_and_records(self):
        res = _FakeResolver({"skeptic": "REJECT", "advocate": "ACCEPT", "epistemologist": "ACCEPT", "methodologist": "ACCEPT"})
        rows = [_row("c1"), _row("c2")]
        recs = pr.replay(rows, list(pr.SEATS), res, SpendTracker(cap_usd=5), sources_fn=lambda c: "Source memories: []")
        assert len(recs) == 8
        assert all(c["schema"] is pr.EVALUATOR_VERDICT_SCHEMA for c in res.calls)  # schema-constrained like the live panel
        m = pr.matrix(recs, list(pr.SEATS))
        assert m["candidates"] == 2
        assert m["per_seat"]["skeptic"] == {"REJECT": 2} and m["per_seat"]["advocate"] == {"ACCEPT": 2}
        assert m["consensus"] == {"ACCEPTED": 2}                              # 3 of 4 accept
        assert recs[0]["live_verdict"] == "REJECT" and recs[1]["live_verdict"] == "ACCEPT"
        assert m["total_cost_usd"] > 0

    def test_model_override_bypasses_profile_and_effort_override_applies(self, monkeypatch):
        res = _FakeResolver({s: "REJECT" for s in pr.SEATS})
        made = []
        class FakeAdapter:
            def __init__(self, model): made.append(model)
            def invoke(self, system_prompt, user_message, effort=None, stage=None, schema=None, **kw):
                return {"output": {"verdict": "ACCEPT", "reasoning": "swapped"}, "usage": {"input_tokens": 10, "output_tokens": 1}}
        monkeypatch.setitem(pr.DEFAULT_ADAPTERS, "fakebe", FakeAdapter)
        overrides = pr.parse_overrides(["skeptic=fakebe:sonnet-x:medium"])
        recs = pr.replay([_row()], ["skeptic", "advocate"], res, SpendTracker(cap_usd=5), overrides=overrides,
                         effort="low", sources_fn=lambda c: "")
        assert made == ["sonnet-x"]
        by_seat = {r["seat"]: r for r in recs}
        assert by_seat["skeptic"]["verdict"] == "ACCEPT" and by_seat["skeptic"]["model"] == "sonnet-x"
        assert by_seat["skeptic"]["effort"] == "low" and by_seat["advocate"]["effort"] == "low"
        assert res.calls and res.calls[0]["role"] == "advocate"               # profile invoker used only for advocate

    def test_bad_override_rejected(self):
        with pytest.raises(SystemExit):
            pr.parse_overrides(["skeptic=nomodel"])
        with pytest.raises(SystemExit):
            pr.parse_overrides(["thinker=bedrock:m"])

    def test_cap_stops_the_experiment(self):
        res = _FakeResolver({s: "ACCEPT" for s in pr.SEATS})
        tracker = SpendTracker(cap_usd=0.000001)
        with pytest.raises(SpendCapExceeded):
            pr.replay([_row()], ["skeptic"], res, tracker, sources_fn=lambda c: "")

    def test_one_failing_call_is_recorded_not_fatal(self):
        res = _FakeResolver({s: "ACCEPT" for s in pr.SEATS})
        def boom(system_prompt, user_message, **kw): raise RuntimeError("throttled")
        res.invoker_for = lambda role: types.SimpleNamespace(invoke=boom)
        recs = pr.replay([_row()], ["skeptic", "advocate"], res, SpendTracker(cap_usd=5), sources_fn=lambda c: "")
        assert [r["verdict"] for r in recs] == ["ERROR", "ERROR"] and "throttled" in recs[0]["reasoning"]
        assert pr.matrix(recs, ["skeptic", "advocate"])["consensus"] == {}

    def test_dry_run_makes_no_calls_and_prompt_variant_is_used(self):
        res = _FakeResolver({s: "ACCEPT" for s in pr.SEATS})
        seen = []
        def variant(role, candidate_json, source_memories_content):
            seen.append(role); return f"VARIANT {role}"
        recs = pr.replay([_row()], ["skeptic"], res, SpendTracker(cap_usd=5), prompt_fn=variant, variant="standard",
                         dry_run=True, sources_fn=lambda c: "")
        assert res.calls == [] and seen == ["skeptic"]
        assert recs[0]["verdict"] is None and recs[0]["variant"] == "standard" and recs[0]["prompt_chars"] == len("VARIANT skeptic")


class TestNoWrites:
    def test_module_has_no_sql_that_modifies_candidates(self):
        import re
        src = open(pr.__file__, encoding="utf-8").read()
        assert not re.search(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+dream_cycle_candidates", src, re.I)
        assert src.count("SELECT") >= 1                                   # it does read them
