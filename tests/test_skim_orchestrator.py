"""Tests for the orchestrator's Step 3.5 — ``DreamCycleOrchestrator._skim_arrivals``.

docs/PLAN-skim-stage.md §2: the skim runs after feedback and before the Explorer,
over the ledger's arrival window, metered through the run's SpendTracker under its
own sub-cap; it never aborts the run on its own failure (None → the Explorer gets
the plain manifest) and only SpendCapExceeded propagates. Synthetic data only; no
database (the skim store is offline in unit tests, see tests/conftest.py) and no
model calls (fake invoker).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.backends.cost import SpendCapExceeded, SpendTracker
from src.backends.resolver import AGENT_ROLES, DEFAULT_ADAPTERS, Resolver, RoleBackend
from src.dream_cycle import orchestrator as o
from src.dream_cycle.orchestrator import DreamCycleOrchestrator
from src.models import EvaluatorVerdict, MemorySlice
from src.prompts.skim import PROMPT_VERSION
from src.skim.runner import SkimResult

SKIM_MODEL = "us.amazon.nova-2-lite-v1:0"
DOC = ("Orrery sync — 18 Sep\nPriya: after the load test we agreed to move the beta launch to the 14th.\n"
       "Tomasz: the pricing tier question is still open: which tier goes first?\n")


def _record(**over):
    rec = {"summary": "Meeting notes agreeing to move the beta launch to the 14th.", "people": ["Priya", "Tomasz"],
           "entities": ["Orrery"], "topics": ["beta launch"],
           "decisions": [{"text": "Launch moves.", "quote": "we agreed to move the beta launch to the 14th"}],
           "open_questions": [], "novelty": 2, "salience": 3, "contradiction_hint": None, "doc_kind": "conversation"}
    rec.update(over)
    return rec


class FakeInvoker:
    def __init__(self, outputs=None, usage=(3000, 200)):
        self.outputs = list(outputs or [])
        self.calls: list[dict] = []
        self._usage = usage

    def invoke(self, system_prompt, user_message, **kw):
        self.calls.append({"system": system_prompt, "user": user_message, **kw})
        out = self.outputs.pop(0) if self.outputs else _record()
        if isinstance(out, Exception):
            raise out
        return {"output": out, "raw": json.dumps(out), "usage_source": "real",
                "usage": {"inputTokens": self._usage[0], "outputTokens": self._usage[1], "serviceTier": "flex"}}


class FakeStore:
    def __init__(self, docs):
        self.docs = {d["id"]: d for d in docs}
        self.rows: dict[tuple, dict] = {}

    def list_arrivals(self, window_start, limit=5000):
        return [{k: v for k, v in d.items() if k != "content"} for d in self.docs.values()]

    def missing_ids(self, ids, model, prompt_version):
        return [i for i in ids if (i, model, prompt_version) not in self.rows]

    def fetch_documents(self, ids):
        return [self.docs[i] for i in ids if i in self.docs]

    def upsert_skim(self, memory_id, **kw):
        self.rows[(memory_id, kw["model"], kw["prompt_version"])] = kw
        return "row"


def _docs(n=2):
    return [{"id": str(uuid.uuid4()), "title": f"Synthetic {i}", "source_type": "slack",
             "created_at": datetime.now(timezone.utc), "content": DOC} for i in range(n)]


def _profile(skim=True, skim_backend="bedrock"):
    p = {r: RoleBackend(r, "kiro", "claude-opus-4.8", "high") for r in AGENT_ROLES}
    if skim:
        p["skim"] = RoleBackend("skim", skim_backend, SKIM_MODEL, "low",
                                {"temperature": 0.0, "service_tier": "flex", "max_tokens": 4096})
    return p


def _orch(profile=None, cap=9.0):
    with patch("src.dream_cycle.orchestrator.default_resolver"):
        orch = DreamCycleOrchestrator()
    if profile is not None:
        # Built without Resolver.__init__ so deliberately misconfigured profiles
        # (skim on Kiro) get past the eager guard and exercise the orchestrator's own.
        resolver = Resolver.__new__(Resolver)
        resolver._profile, resolver._adapters, resolver._cache = profile, DEFAULT_ADAPTERS, {}
        orch.resolver = resolver
    orch._spend = SpendTracker(cap_usd=cap)
    orch._current_run_id = "run-s"
    return orch


STARTED = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)


class TestSkimArrivalsFallbacks:
    def test_unconfigured_role_returns_none_with_one_warning(self, caplog):
        orch = _orch(_profile(skim=False))
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.orchestrator"), \
             patch("src.dream_cycle.orchestrator.skim_window") as sw, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "not configured" in warnings[0].getMessage()

    @pytest.mark.parametrize("backend", ["kiro", "claude_code", "codex"])
    def test_agentic_backend_returns_none_and_never_invokes(self, backend, caplog):
        orch = _orch(_profile(skim_backend=backend))
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.orchestrator"), \
             patch("src.dream_cycle.orchestrator.skim_window") as sw, \
             patch.object(orch, "_invoker_for") as inv, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called(); inv.assert_not_called()
        assert any("must run on the direct Bedrock backend" in r.getMessage() for r in caplog.records)

    def test_mocked_resolver_returns_none(self):
        """A MagicMock resolver (most orchestrator tests) yields an unknown backend → None."""
        orch = _orch()
        with patch("src.dream_cycle.orchestrator.skim_window") as sw:
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called()

    def test_exception_inside_returns_none_with_warning(self, caplog):
        orch = _orch(_profile())
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.orchestrator"), \
             patch("src.dream_cycle.orchestrator.skim_window", side_effect=RuntimeError("db unreachable")), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        assert any("Skim failed (non-fatal)" in r.getMessage() for r in caplog.records)

    def test_store_offline_is_non_fatal(self):
        """The real runner against the (offline in tests) store: selection fails → None."""
        orch = _orch(_profile())
        with patch.object(orch, "_invoker_for", return_value=FakeInvoker()), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None

    def test_spend_cap_exceeded_propagates(self):
        orch = _orch(_profile())
        with patch("src.dream_cycle.orchestrator.skim_window", side_effect=SpendCapExceeded("cap")), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            with pytest.raises(SpendCapExceeded):
                orch._skim_arrivals("run-s", "scheduled", STARTED)


class TestSkimArrivalsHappyPath:
    def test_runner_is_called_with_the_run_tracker_window_and_sub_cap(self):
        orch = _orch(_profile(), cap=9.0)
        result = SkimResult(docs_total=3, skimmed=2, reused=1, cost_usd=0.002)
        db = MagicMock()
        db.get_previous_run_started_at.return_value = STARTED - timedelta(days=2)
        with patch("src.dream_cycle.orchestrator.skim_window", return_value=result) as sw, \
             patch.object(orch, "_invoker_for", return_value="INVOKER") as inv, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db", db):
            out = orch._skim_arrivals("run-s", "scheduled", STARTED)
        assert out is result
        inv.assert_called_once_with("skim")
        kw = sw.call_args.kwargs
        assert sw.call_args.args[0] == STARTED - timedelta(days=2)      # ledger window (2 days back)
        assert kw["invoker"] == "INVOKER" and kw["model"] == SKIM_MODEL
        assert kw["prompt_version"] == PROMPT_VERSION and kw["tracker"] is orch._spend
        assert kw["cap_usd"] == pytest.approx(1.00) and kw["run_id"] == "run-s" and kw["effort"] == "low"

    def test_end_to_end_stores_records_through_the_real_runner(self):
        """Real skim_window + fake invoker + in-memory store: records land in the store
        with attribution, spend lands on the run tracker."""
        docs = _docs(2)
        store = FakeStore(docs)
        orch = _orch(_profile(), cap=9.0)
        inv = FakeInvoker([_record(), _record()])
        with patch("src.skim.runner.default_store", store), \
             patch.object(orch, "_invoker_for", return_value=inv), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            res = orch._skim_arrivals("run-s", "scheduled", STARTED)
        assert res.docs_total == 2 and res.skimmed == 2 and res.quotes_verbatim == 2
        assert {k[0] for k in store.rows} == {d["id"] for d in docs}
        row = store.rows[(docs[0]["id"], SKIM_MODEL, PROMPT_VERSION)]
        assert row["run_id"] == "run-s" and row["skim"]["summary"].startswith("Meeting notes")
        assert orch._spend.calls == 2 and orch._spend.total_usd == pytest.approx(res.cost_usd)
        assert inv.calls[0]["schema"] is not None and inv.calls[0]["tools"] is False and inv.calls[0]["stage"] == "skim"

    def test_pipeline_persists_skim_counts_in_run_stats(self):
        """run(): the skim result's counters travel to complete_run's stats (→ stats JSONB)."""
        orch = _orch()
        skim_result = SkimResult(docs_total=40, skimmed=35, reused=5, failed=0, cost_usd=0.0312)
        slice_ = MemorySlice(name="S", strategy="arrivals_synthesis", memory_ids=["m1"])
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=skim_result) as skim, \
             patch.object(orch, "invoke_explorer", return_value=[slice_]), \
             patch.object(orch, "_allocate_attention", side_effect=lambda r, t, s, sl: sl), \
             patch.object(orch, "invoke_thinker", return_value=[]), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("scheduled")
        skim.assert_called_once()
        assert skim.call_args.args[0] == "run-p" and skim.call_args.args[1] == "scheduled"
        stats = db.complete_run.call_args.kwargs["stats"]
        assert stats["candidates_generated"] == 0
        assert (stats["skim_docs"], stats["skim_skimmed"], stats["skim_reused"], stats["skim_failed"]) == (40, 35, 5, 0)
        assert stats["skim_cost_usd"] == pytest.approx(0.0312) and stats["skim_model"] == ""
        assert result.aborted_early is False

    def test_pipeline_without_skim_has_no_skim_stats(self):
        orch = _orch()
        slice_ = MemorySlice(name="S", strategy="arrivals_synthesis", memory_ids=["m1"])
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[slice_]), \
             patch.object(orch, "_allocate_attention", side_effect=lambda r, t, s, sl: sl), \
             patch.object(orch, "invoke_thinker", return_value=[]), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            orch.run("scheduled")
        stats = db.complete_run.call_args.kwargs["stats"]
        assert set(stats) == {"candidates_generated", "candidates_accepted", "candidates_rejected"}

    def test_skim_runs_after_feedback_and_before_the_explorer(self):
        order = []
        orch = _orch()
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection",
                   side_effect=lambda: order.append("feedback") or ""), \
             patch.object(orch, "_skim_arrivals", side_effect=lambda *a: order.append("skim")), \
             patch.object(orch, "invoke_explorer", side_effect=lambda *a, **k: order.append("explorer") or []), \
             patch.object(orch, "_allocate_attention", side_effect=lambda r, t, s, sl: sl), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            orch.run("scheduled")
        assert order == ["feedback", "skim", "explorer"]

    def test_spend_cap_from_the_skim_aborts_the_run_like_any_stage(self):
        orch = _orch()
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", side_effect=SpendCapExceeded("cap")), \
             patch.object(orch, "invoke_explorer") as explorer, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("scheduled")
        assert result.aborted_early is True
        explorer.assert_not_called()
        db.complete_run.assert_called_once()      # the run record is closed, not left dangling

    def test_explorer_failure_abort_still_records_skim_stats(self):
        orch = _orch()
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=SkimResult(docs_total=5, skimmed=5)), \
             patch.object(orch, "invoke_explorer", side_effect=RuntimeError("crash")), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("user_triggered", scope={"topic": "x"})
        assert result.aborted_early is True
        stats = db.complete_run.call_args.kwargs["stats"]
        assert stats["candidates_generated"] == 0 and stats["skim_skimmed"] == 5


class TestSkimCap:
    def test_default_is_one_dollar_bounded_to_15_percent_of_the_run_cap(self, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_SKIM_CAP_USD", raising=False)
        assert o.SKIM_SPEND_CAP_USD == 1.00 and o.SKIM_RUN_CAP_SHARE == 0.15
        assert o._skim_cap_usd(9.0) == pytest.approx(1.00)
        assert o._skim_cap_usd(5.0) == pytest.approx(0.75)
        assert o._skim_cap_usd(0) == pytest.approx(1.00)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_SKIM_CAP_USD", "0.40")
        assert o._skim_cap_usd(9.0) == pytest.approx(0.40)
        monkeypatch.setenv("SECOND_BRAIN_SKIM_CAP_USD", "3")
        assert o._skim_cap_usd(9.0) == pytest.approx(1.35)      # still bounded to 15%

    @pytest.mark.parametrize("bad", ["abc", "0", "-1"])
    def test_bad_env_falls_back_to_default(self, monkeypatch, bad, caplog):
        monkeypatch.setenv("SECOND_BRAIN_SKIM_CAP_USD", bad)
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.orchestrator"):
            assert o._skim_cap_usd(9.0) == pytest.approx(1.00)
        assert any("SECOND_BRAIN_SKIM_CAP_USD" in r.getMessage() for r in caplog.records)


class TestProvenanceIncludesSkim:
    def test_backend_provenance_carries_the_skim_role_when_configured(self):
        orch = _orch(_profile())
        prov = orch._backend_provenance()
        assert prov["skim"] == {"backend": "bedrock", "model": SKIM_MODEL, "effort": "low"}
        assert set(prov) == set(AGENT_ROLES) | {"skim"}

    def test_backend_provenance_omits_an_unconfigured_skim(self):
        orch = _orch(_profile(skim=False))
        assert set(orch._backend_provenance()) == set(AGENT_ROLES)
