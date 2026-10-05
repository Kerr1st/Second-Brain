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
    p = {r: RoleBackend(r, "claude_code", "global.anthropic.claude-opus-4-8", "high") for r in AGENT_ROLES}
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
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.stages.skim_stage"), \
             patch("src.dream_cycle.stages.skim_stage.skim_window") as sw, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "not configured" in warnings[0].getMessage()

    @pytest.mark.parametrize("value", ["1", "true", "YES"])
    def test_env_off_switch_skips_the_stage_with_one_warning(self, value, caplog, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_SKIM_DISABLED", value)
        orch = _orch(_profile())
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.stages.skim_stage"), \
             patch("src.dream_cycle.stages.skim_stage.skim_window") as sw, \
             patch.object(orch, "_invoker_for") as inv, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called(); inv.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "SECOND_BRAIN_SKIM_DISABLED" in warnings[0].getMessage()

    def test_env_off_switch_ignores_other_values(self, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_SKIM_DISABLED", "0")
        orch = _orch(_profile(skim=False))  # falls through to the normal path
        with patch("src.dream_cycle.stages.skim_stage.skim_window"), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None  # unconfigured, not disabled

    @pytest.mark.parametrize("backend", ["kiro", "claude_code", "codex"])
    def test_agentic_backend_returns_none_and_never_invokes(self, backend, caplog):
        orch = _orch(_profile(skim_backend=backend))
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.stages.skim_stage"), \
             patch("src.dream_cycle.stages.skim_stage.skim_window") as sw, \
             patch.object(orch, "_invoker_for") as inv, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called(); inv.assert_not_called()
        assert any("must run on the direct Bedrock backend" in r.getMessage() for r in caplog.records)

    def test_mocked_resolver_returns_none(self):
        """A MagicMock resolver (most orchestrator tests) yields an unknown backend → None."""
        orch = _orch()
        with patch("src.dream_cycle.stages.skim_stage.skim_window") as sw:
            assert orch._skim_arrivals("run-s", "scheduled", STARTED) is None
        sw.assert_not_called()

    def test_exception_inside_returns_none_with_warning(self, caplog):
        orch = _orch(_profile())
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.stages.skim_stage"), \
             patch("src.dream_cycle.stages.skim_stage.skim_window", side_effect=RuntimeError("db unreachable")), \
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
        with patch("src.dream_cycle.stages.skim_stage.skim_window", side_effect=SpendCapExceeded("cap")), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            with pytest.raises(SpendCapExceeded):
                orch._skim_arrivals("run-s", "scheduled", STARTED)


class TestSkimArrivalsHappyPath:
    def test_runner_is_called_with_the_run_tracker_window_and_sub_cap(self):
        orch = _orch(_profile(), cap=9.0)
        result = SkimResult(docs_total=3, skimmed=2, reused=1, cost_usd=0.002)
        db = MagicMock()
        db.get_previous_run_started_at.return_value = STARTED - timedelta(days=2)
        with patch("src.dream_cycle.stages.skim_stage.skim_window", return_value=result) as sw, \
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
        assert not any(key.startswith("skim_") for key in stats)
        assert stats["cost_usd"] == 0 and stats["spend_unknown"] is False

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
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.stages.skim_stage"):
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



# --------------------------------------------------------------------------- #
# Phase 3 — Explorer consumption (docs/PLAN-skim-stage.md)                     #
# --------------------------------------------------------------------------- #
def _skim_record(rid, channel="slack", salience=2, novelty=1, summary="A synthetic summary.", created=None):
    return {"id": rid, "title": "t", "source_type": channel, "created_at": created or STARTED,
            "skim": {"summary": summary, "people": [], "entities": [], "topics": [], "decisions": [],
                     "open_questions": [], "novelty": novelty, "salience": salience, "contradiction_hint": None,
                     "doc_kind": "conversation", "quotes_unverified": 0},
            "cluster_id": None, "truncated": False}


class TestLoadSkimRecords:
    def test_none_or_nothing_stored_fetches_nothing(self):
        orch = _orch()
        with patch("src.dream_cycle.stages.skim_stage.skim_store") as st, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            assert orch._load_skim_records(None, "run-s", "scheduled", STARTED) == []
            assert orch._load_skim_records(SkimResult(docs_total=3, skimmed=0, reused=0), "run-s", "scheduled",
                                           STARTED) == []
        st.fetch_window_records.assert_not_called()

    def test_fetches_the_window_for_the_skim_key_and_clusters(self):
        orch = _orch()
        recs = [_skim_record("a"), _skim_record("b")]
        skim = SkimResult(docs_total=2, skimmed=1, reused=1, model=SKIM_MODEL, prompt_version=PROMPT_VERSION)
        db = MagicMock()
        db.get_previous_run_started_at.return_value = STARTED - timedelta(days=2)
        with patch("src.dream_cycle.stages.skim_stage.skim_store") as st, \
             patch("src.dream_cycle.stages.skim_stage.cluster_window") as cw, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db", db):
            st.fetch_window_records.return_value = recs
            out = orch._load_skim_records(skim, "run-s", "scheduled", STARTED)
        assert out is recs
        st.fetch_window_records.assert_called_once_with(STARTED - timedelta(days=2), SKIM_MODEL, PROMPT_VERSION)
        cw.assert_called_once_with(recs, model=SKIM_MODEL, prompt_version=PROMPT_VERSION)

    def test_store_failure_and_clustering_failure_are_non_fatal(self, caplog):
        orch = _orch()
        skim = SkimResult(docs_total=2, skimmed=2, model=SKIM_MODEL, prompt_version=PROMPT_VERSION)
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.stages.skim_stage"), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            # Store offline (conftest): the real module raises.
            assert orch._load_skim_records(skim, "run-s", "scheduled", STARTED) == []
        assert any("Skim records unavailable" in r.getMessage() for r in caplog.records)
        recs = [_skim_record("a"), _skim_record("b")]
        with patch("src.dream_cycle.stages.skim_stage.skim_store") as st, \
             patch("src.dream_cycle.stages.skim_stage.cluster_window", side_effect=RuntimeError("no vectors")), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            st.fetch_window_records.return_value = recs
            assert orch._load_skim_records(skim, "run-s", "scheduled", STARTED) is recs

    def test_empty_result_set_falls_back(self):
        orch = _orch()
        skim = SkimResult(docs_total=2, skimmed=2, model=SKIM_MODEL, prompt_version=PROMPT_VERSION)
        with patch("src.dream_cycle.stages.skim_stage.skim_store") as st, \
             patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            st.fetch_window_records.return_value = []
            assert orch._load_skim_records(skim, "run-s", "scheduled", STARTED) == []


class TestExplorerInventory:
    ARRIVALS = [{"source_type": "slack", "documents": 3, "memories": 9,
                 "samples": [{"id": "id-9", "title": "Slack: #eng — 2026-09-19", "created_at": "2026-09-19T08:00:00+00:00"}]},
                {"source_type": "article", "documents": 5, "memories": 20, "samples": []}]

    def _explore(self, orch, caplog):
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": [], "raw": "[]", "usage": None, "usage_source": "estimate"}
        with caplog.at_level(logging.INFO, logger="src.dream_cycle.agents.explorer"), \
             patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_invoker_for", return_value=invoker), \
             patch.object(orch, "_invoke_metered", side_effect=lambda role, **kw: invoker.invoke(**kw)), patch.object(orch, "_capture"):
            db.get_previous_run_started_at.return_value = STARTED - timedelta(days=1)
            db.get_arrivals_summary.return_value = self.ARRIVALS
            db.get_strategy_usage.return_value = {}
            orch._current_started_at = STARTED
            orch.invoke_explorer("", "scheduled", None,
                                 stats={"total_count": 10, "recent_activity": 9, "date_range": {"min": STARTED, "max": STARTED}})
        return invoker.invoke.call_args.kwargs["user_message"]

    def test_records_make_the_user_message_an_inventory(self, caplog):
        orch = _orch()
        orch._skim_records = [_skim_record("id-a", "slack", 3, 2, "Priya moved the launch."),
                              _skim_record("id-b", "article", 0, 1, "A generic article.")]
        orch._skim_records[0]["cluster_id"] = "c1"
        msg = self._explore(orch, caplog)
        assert "## What arrived since the previous run (2026-09-19 02:00 UTC)" in msg
        assert "8 documents across 2 channels; 2 of them were read by the skim stage" in msg
        assert "- article: 5 articles / web pages" in msg and "- slack: 3 conversation-days" in msg
        assert "id-a | slack | S3 N2 | - | Priya moved the launch. [c1]" in msg
        assert msg.index("id-a | slack") < msg.index("id-b | article")
        assert "[id-9]" not in msg                                   # no manifest samples in inventory mode
        assert "meet the slice budget" in msg
        assert orch._explorer_input == {"explorer_input": "inventory", "inventory_listed": 2, "inventory_docs": 2,
                                        "inventory_chars": len(msg.split("\n\n", 1)[1])}
        assert any("Explorer arrivals input: inventory — 2 of 2 skimmed documents listed" in r.getMessage()
                   for r in caplog.records)

    def test_without_records_the_manifest_is_used_and_logged(self, caplog):
        orch = _orch()
        msg = self._explore(orch, caplog)
        assert "- slack: 3 conversation-days (9 memories)" in msg and "[id-9]" in msg
        assert " | S" not in msg
        assert orch._explorer_input == {"explorer_input": "manifest"}
        assert any("Explorer arrivals input: manifest — 2 channels" in r.getMessage() for r in caplog.records)

    def test_pipeline_records_the_explorer_input_in_run_stats(self):
        """run(): the choice travels to complete_run (here the circuit-breaker path, since a
        mocked DB resolves no slices)."""
        orch = _orch()
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": [], "raw": "[]", "usage": None, "usage_source": "estimate"}
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "_invoker_for", return_value=invoker), \
             patch.object(orch, "_invoke_metered", side_effect=lambda role, **kw: invoker.invoke(**kw)), patch.object(orch, "_capture"), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            db.get_arrivals_summary.return_value = []
            db.get_strategy_usage.return_value = {}
            result = orch.run("scheduled")
        assert result.aborted_early is True
        assert db.complete_run.call_args.kwargs["stats"]["explorer_input"] == "manifest"


class TestWeightedArrivalsSliceWiring:
    def _prov(self, mid, source_type="slack", created=None):
        return {"id": mid, "type": "source", "source_type": source_type, "created_at": created or STARTED,
                "parent_id": None, "project": None}

    def test_allocate_attention_weights_the_guaranteed_slice_and_records_metrics(self):
        orch = _orch()
        new_rows = [{"id": f"slack-{i}", "title": f"Slack day {i}", "type": "source", "source_type": "slack",
                     "created_at": STARTED - timedelta(hours=i)} for i in range(6)] + \
                   [{"id": f"mail-{i}", "title": f"Email {i}", "type": "source", "source_type": "outlook_email",
                     "created_at": STARTED - timedelta(hours=i)} for i in range(6)]
        prov = [self._prov(r["id"], r["source_type"], r["created_at"]) for r in new_rows]
        # The two oldest documents are the salient ones; stratification alone (newest first) skips them.
        orch._skim_records = [_skim_record("slack-5", "slack", 3, 3), _skim_record("mail-5", "outlook_email", 3, 2)]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db:
            db.get_previous_run_started_at.return_value = STARTED - timedelta(days=1)
            db.get_recent_slice_memory_ids.return_value = set()
            _provenance.side_effect = lambda ids: [r for r in prov if r["id"] in set(ids)]
            db.get_recent_parent_memories.return_value = new_rows
            out = orch._allocate_attention("run-1", "scheduled", STARTED, [])
        assert [s.strategy for s in out] == ["arrivals_synthesis"]
        assert out[0].memory_ids[:2] == ["slack-5", "mail-5"]
        assert len(out[0].memory_ids) == 8                                  # 4 + 4 under the per-channel cap
        rows = db.record_slices.call_args[0][1]
        assert rows[0]["origin"] == "guaranteed_arrivals"
        assert rows[0]["metrics"]["skim_weighted"] is True and rows[0]["metrics"]["skim_picked"] == 2
        assert rows[0]["metrics"]["arrival_share"] == 1.0
        assert orch._slice_rows == rows                                     # kept for the judging order

    def test_without_records_the_metrics_carry_no_skim_keys(self):
        orch = _orch()
        new_rows = [{"id": f"slack-{i}", "title": "t", "type": "source", "source_type": "slack", "created_at": STARTED}
                    for i in range(3)]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db:
            db.get_previous_run_started_at.return_value = STARTED - timedelta(days=1)
            db.get_recent_slice_memory_ids.return_value = set()
            _provenance.side_effect = lambda ids: [self._prov(i) for i in ids]
            db.get_recent_parent_memories.return_value = new_rows
            orch._allocate_attention("run-1", "scheduled", STARTED, [])
        rows = db.record_slices.call_args[0][1]
        assert "skim_weighted" not in rows[0]["metrics"] and "skim_picked" not in rows[0]["metrics"]

    def test_slice_rows_are_empty_when_the_ledger_is_unavailable(self):
        orch = _orch()
        orch._slice_rows = [{"stale": True}]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db"):
            out = orch._allocate_attention("run-1", "scheduled", STARTED, [MemorySlice(name="s", strategy="x", memory_ids=["m"])])
        assert len(out) == 1 and orch._slice_rows == []


_provenance = MagicMock()


@pytest.fixture(autouse=True)
def provenance_repository(monkeypatch):
    """``get_memory_provenance`` lives in ``src.db`` since R3; the ledger imports it directly."""
    _provenance.reset_mock(return_value=True, side_effect=True)
    monkeypatch.setattr("src.dream_cycle.ledger.get_memory_provenance", _provenance)
