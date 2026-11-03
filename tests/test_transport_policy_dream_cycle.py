"""Dream-cycle resilience under the transport-failure reservation policy.

Through ``DreamCycleOrchestrator._invoke_metered`` (the real metering layer) with
fake adapters that fail the way Bedrock / Claude Code fail before a response
exists: throttle, timeout, CLI crash. 1071a81 semantics that must hold:

* Explorer crash on a scheduled run -> continue with the guaranteed arrivals slice
* Thinker failure on one slice -> skip that slice, judge the rest
* each failed attempt is charged a conservative reservation (never free) and the
  run record carries ``spend_reserved_usd``
* a routing-guarantee failure still blocks the tracker and aborts the run
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.backends.base import CorpusRoutingError
from src.backends.resolver import Resolver, RoleBackend
from src.models import CandidateInsight, EvaluatorVerdict, MemorySlice

EXPLORER = RoleBackend("explorer", "claude_code", "global.anthropic.claude-sonnet-5", "high")
THINKER = RoleBackend("thinker", "bedrock", "global.anthropic.claude-opus-5", "high")
SEATS = {r: RoleBackend(r, "bedrock", "global.anthropic.claude-sonnet-5", "high")
         for r in ("skeptic", "advocate", "epistemologist", "methodologist")}


class FakeAdapter:
    """Minimal Invoker: queued outcomes, records calls, exposes model/backend/max_tokens."""

    def __init__(self, backend, model, outcomes, max_tokens=1000):
        self.BACKEND_NAME, self.model, self._max_tokens = backend, model, max_tokens
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, system_prompt, user_message, **kw):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"output": out, "raw": "", "usage": {"inputTokens": 1000, "outputTokens": 100}, "usage_source": "real"}


@pytest.fixture(autouse=True)
def _coordination_only_authorization(monkeypatch):
    """Placeholder memory ids ("m1") isolate the transport policy; pre-spend
    authorization (R3 fix 4) is pinned on the disposable database in
    tests/test_discovery_integrity.py."""
    monkeypatch.setattr("src.dream_cycle.orchestrator.authorize_candidate", lambda candidate, context: set())


def _slice(name):
    return MemorySlice(name=name, strategy="pattern_emergence", memory_ids=["m1"], memory_titles=["M1"],
                       hypothesis="h")


def _candidate(title="Insight"):
    return CandidateInsight(title=title, type="insight", operation="CREATE", content=f"Content {title}",
                            source_memories=["m1"], relationships=[], strategy_that_found_it="pattern_emergence")


def _orchestrator(adapters: dict):
    from src.dream_cycle.orchestrator import DreamCycleOrchestrator
    orch = DreamCycleOrchestrator()
    orch.resolver = Resolver({"explorer": EXPLORER, "thinker": THINKER, **SEATS})
    orch._invoker_for = lambda role: adapters[role]
    return orch


def _stats():
    return {"total_count": 100, "recent_activity": 5, "date_range": {"min": None, "max": None},
            "type_distribution": {}}


def _run(orch, adapters, *, slices_from_allocation, thinker_results):
    """Drive ``orch.run('scheduled')`` with the DB mocked out but the metering real.

    Explorer and Thinker go through ``_invoke_metered`` (their prompt assembly,
    which needs the ledger/DB, is bypassed); evaluators are stubbed to ACCEPT.
    """
    def explorer_via_metering(feedback, run_type, scope=None, stats=None):
        res = orch._invoke_metered("explorer", system_prompt="explorer prompt", user_message="go",
                                   tools=True, stage="explorer", run_id="run-1")
        return [_slice(s["name"]) for s in res["output"]]

    def thinker_via_metering(s):
        orch._invoke_metered("thinker", system_prompt="thinker prompt", user_message=s.name,
                             tools=False, stage="thinker", run_id="run-1")
        return thinker_results[s.name]

    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "_load_skim_records", return_value=[]), \
         patch.object(orch, "invoke_explorer", side_effect=explorer_via_metering), \
         patch.object(orch, "_allocate_attention",
                      side_effect=lambda run_id, run_type, started_at, slices: slices + slices_from_allocation), \
         patch.object(orch, "invoke_thinker", side_effect=thinker_via_metering), \
         patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
         patch.object(orch, "invoke_evaluator",
                      side_effect=lambda c, role, **kw: EvaluatorVerdict(role=role, verdict="ACCEPT", reasoning="ok")), \
         patch("src.dream_cycle.orchestrator.allocation.order_slices", side_effect=lambda s, rows: [(x, None) for x in s]), \
         patch("src.dream_cycle.orchestrator.allocation.describe_slice_order", return_value=""), \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
         patch("src.dream_cycle.orchestrator.generate_digest", return_value="digest"), \
         patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
         patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-id"):
        mock_db.create_run.return_value = "run-1"
        mock_db.get_memory_stats.return_value = _stats()
        mock_db.store_candidate.return_value = "cand-id"
        mock_db.expire_stale_relationships.return_value = 0
        result = orch.run(run_type="scheduled")
        stats = mock_db.complete_run.call_args.kwargs.get("stats") or mock_db.complete_run.call_args[0][1]
    return result, stats


def test_explorer_transport_failure_continues_with_the_arrivals_slice():
    """The 2026-09-30 failure mode: the Explorer CLI dies. The night must go on
    with the guaranteed arrivals slice, and the dead call is not free."""
    adapters = {
        "explorer": FakeAdapter("claude_code", EXPLORER.model, [RuntimeError("claude exited 1: credential expired")]),
        "thinker": FakeAdapter("bedrock", THINKER.model, [{"ok": True}]),
    }
    orch = _orchestrator(adapters)
    arrivals = _slice("arrivals")
    result, stats = _run(orch, adapters, slices_from_allocation=[arrivals],
                         thinker_results={"arrivals": [_candidate()]})

    assert result.aborted_early is False
    assert result.candidates_generated == 1 and result.candidates_accepted == 1
    assert adapters["explorer"].calls == 1 and adapters["thinker"].calls == 1
    assert orch._spend.reserved_calls == 1 and orch._spend.reserved_usd > 0
    assert orch._spend.blocked_reason is None
    assert stats["spend_reserved_usd"] == pytest.approx(orch._spend.reserved_usd)
    assert stats["spend_unknown"] is False
    assert stats["cost_usd"] == pytest.approx(orch._spend.total_usd)


def test_thinker_timeout_skips_the_slice_and_judges_the_rest():
    adapters = {
        "explorer": FakeAdapter("claude_code", EXPLORER.model, [[{"name": "A"}, {"name": "B"}]]),
        "thinker": FakeAdapter("bedrock", THINKER.model, [TimeoutError("Bedrock read timeout"), {"ok": True}]),
    }
    orch = _orchestrator(adapters)
    result, stats = _run(orch, adapters, slices_from_allocation=[],
                         thinker_results={"A": [_candidate("from A")], "B": [_candidate("from B")]})

    assert result.aborted_early is False
    assert result.candidates_generated == 1 and result.candidates_accepted == 1
    assert adapters["thinker"].calls == 2
    assert orch._spend.reserved_calls == 1
    # The Thinker floor (EST_THINKER_CALL_USD) applies when larger than the token price.
    from src.dream_cycle.orchestrator import EST_THINKER_CALL_USD
    assert orch._spend.reserved_usd == pytest.approx(EST_THINKER_CALL_USD)
    assert stats["spend_reserved_usd"] == pytest.approx(EST_THINKER_CALL_USD)


def test_reservation_counts_toward_the_run_cap_and_the_budget_gate(monkeypatch):
    """A reservation is real budget: with a cap that cannot also afford the
    Thinker's estimated needs after the Explorer's failed call, the budget gate
    skips the Thinker and the run completes with nothing generated."""
    adapters = {
        "explorer": FakeAdapter("claude_code", EXPLORER.model, [RuntimeError("Throttled")], max_tokens=32768),
        "thinker": FakeAdapter("bedrock", THINKER.model, [{"ok": True}]),
    }
    # Sonnet-5 at 32768 output tokens ≈ $0.33 reserved; a $2.00 cap then leaves
    # less than the Thinker gate needs ($0.30 + 2 × $0.70). run() rebuilds the
    # tracker from the environment, so the cap is set there.
    monkeypatch.setenv("SECOND_BRAIN_RUN_COST_CAP_USD", "2.0")
    orch = _orchestrator(adapters)
    result, stats = _run(orch, adapters, slices_from_allocation=[_slice("arrivals")],
                         thinker_results={"arrivals": [_candidate()]})
    assert adapters["thinker"].calls == 0
    assert result.candidates_generated == 0 and result.aborted_early is False
    assert stats["slices_skipped_for_budget"] == 1
    assert stats["spend_reserved_usd"] > 0.3


def test_routing_guarantee_failure_blocks_and_aborts_the_run():
    adapters = {
        "explorer": FakeAdapter("claude_code", EXPLORER.model, [[{"name": "A"}]]),
        "thinker": FakeAdapter("bedrock", THINKER.model, [CorpusRoutingError("Requested corpus model was not honored")]),
    }
    orch = _orchestrator(adapters)
    result, stats = _run(orch, adapters, slices_from_allocation=[], thinker_results={"A": [_candidate()]})
    assert result.aborted_early is True
    assert adapters["thinker"].calls == 1
    assert orch._spend.blocked_reason
    assert stats["aborted"] is True and stats["spend_unknown"] is True
    assert orch._spend.reserved_calls == 0
