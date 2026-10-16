"""Issue I4 (2026-10-14): Explorer slice ids are canonicalised before the
provenance lookup, a dropped slice logs a sample of its unresolved ids (ids only),
and the pre-budget Explorer slices are persisted in the run stats (hermetic)."""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.dream_cycle import allocation, ledger
from src.dream_cycle.agents import explorer
from src.dream_cycle.orchestrator import DreamCycleOrchestrator
from src.models import MemorySlice

NOW = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)
WINDOW = NOW - timedelta(days=1)
FABRICATED = "00000000-dead-beef-0000-000000000000"   # well-formed, does not exist
SECRET = "CONFIDENTIAL memory body text that must never be logged"


def _uid():
    return str(uuid.uuid4())


def _prov(mid, source_type="slack"):
    return {"id": mid, "type": "source", "source_type": source_type, "created_at": NOW,
            "parent_id": None, "project": None}


def _variants(u):
    """Forms that resolve in the database (uuid.UUID accepts them) but are not canonical."""
    return [u.upper(), "{" + u + "}", u.replace("-", ""), "urn:uuid:" + u.upper()]


def _orch():
    with patch("src.dream_cycle.orchestrator.default_resolver"):
        return DreamCycleOrchestrator()


def _invoke(orch, output):
    invoker = MagicMock()
    invoker.invoke.return_value = {"output": output, "raw": "[]", "usage": None, "usage_source": "estimate"}
    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
         patch.object(orch, "_invoke_metered", side_effect=lambda role, **kw: invoker.invoke(**kw)), \
         patch.object(orch, "_capture"):
        db.get_previous_run_started_at.return_value = WINDOW
        db.get_arrivals_summary.return_value = []
        db.get_strategy_usage.return_value = {}
        orch._current_run_id = "run-i4"
        orch._current_started_at = NOW
        return orch.invoke_explorer("", "scheduled", None,
                                    stats={"total_count": 10, "recent_activity": 9,
                                           "date_range": {"min": NOW, "max": NOW}})


class TestCanonicalIds:
    def test_variants_canonicalise_and_dedupe_in_order(self):
        a, b = _uid(), _uid()
        assert ledger.canonical_ids([a.upper(), "{" + b + "}", a, a.replace("-", "")]) == [a, b]

    def test_non_uuid_strings_kept_verbatim(self):
        assert ledger.canonical_ids(["mem-1", "MEM-1", "mem-1", 7]) == ["mem-1", "MEM-1", "7"]

    def test_slice_metrics_resolves_non_canonical_forms(self):
        ids = [_uid() for _ in range(4)]
        prov = {i: _prov(i) for i in ids}
        raw = [_variants(i)[k] for k, i in enumerate(ids)]
        m = ledger.slice_metrics(raw, prov, WINDOW)
        assert m["requested"] == 4 and m["resolved"] == 4

    def test_fetch_provenance_keys_are_canonical(self):
        u = _uid()
        with patch("src.dream_cycle.ledger.get_memory_provenance",
                   return_value=[_prov(uuid.UUID(u))]):
            prov = ledger.fetch_provenance([MemorySlice(name="s", strategy="x", memory_ids=[u.upper()])],
                                           dcdb=MagicMock())
        assert list(prov) == [u]


class TestExplorerParse:
    def test_ids_canonicalised_and_deduped_titles_follow(self):
        a, b = _uid(), _uid()
        out = _invoke(_orch(), [{"name": "Sony engagement", "strategy": "x",
                                 "memory_ids": [a.upper(), b.replace("-", ""), "{" + a + "}", "made-up"],
                                 "memory_titles": ["A", "B", "A again", "M"]}])
        assert out[0].memory_ids == [a, b, "made-up"]
        assert out[0].memory_titles == ["A", "B", "M"]

    def test_unaligned_titles_left_alone(self):
        a = _uid()
        out = _invoke(_orch(), [{"name": "s", "strategy": "x", "memory_ids": [a, a.upper()],
                                 "memory_titles": ["only one title list entry", "x", "y"]}])
        assert out[0].memory_ids == [a]
        assert out[0].memory_titles == ["only one title list entry", "x", "y"]

    def test_pre_budget_summary_recorded_and_bounded(self):
        many = [_uid() for _ in range(explorer.PRE_BUDGET_MAX_IDS + 5)]
        raw = [{"name": f"s{i}", "strategy": "x", "memory_ids": many, "memory_titles": [SECRET] * len(many),
                "hypothesis": SECRET} for i in range(explorer.PRE_BUDGET_MAX_SLICES + 2)]
        orch = _orch()
        _invoke(orch, raw + ["not-a-dict"])
        pre = orch._explorer_pre_budget
        assert pre["raw_slices"] == len(raw) + 1 and pre["parsed_slices"] == len(raw)
        assert len(pre["slices"]) == explorer.PRE_BUDGET_MAX_SLICES
        first = pre["slices"][0]
        assert first["requested"] == len(many) and first["memory_ids"] == many[:explorer.PRE_BUDGET_MAX_IDS]
        assert SECRET not in repr(pre)                         # ids and counts, no content


class TestAllocateKeepsCanonicalisedSlice:
    def test_upper_braced_unhyphenated_kept_fabricated_dropped(self, caplog):
        good = [_uid() for _ in range(4)]
        rows = [_prov(g, st) for g, st in zip(good, ["slack", "slack", "outlook_email", "outlook_email"])]
        raw = [{"name": "Sony engagement: variants", "strategy": "x",
                "memory_ids": [_variants(g)[k] for k, g in enumerate(good)], "memory_titles": [SECRET] * 4},
               {"name": "Sony engagement: fabricated", "strategy": "y",
                "memory_ids": [FABRICATED, "fake-id-2", "fake-id-3", "fake-id-4"], "memory_titles": [SECRET] * 4,
                "hypothesis": SECRET}]
        orch = _orch()
        slices = _invoke(orch, raw)
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.ledger.get_memory_provenance") as provenance, \
             caplog.at_level(logging.WARNING, logger="src.dream_cycle.allocation"):
            db.get_previous_run_started_at.return_value = WINDOW
            db.get_recent_slice_memory_ids.return_value = set()
            db.get_recent_parent_memories.return_value = []
            provenance.side_effect = lambda ids: [r for r in rows if r["id"] in set(ids)]
            out = orch._allocate_attention("run-i4", "scheduled", NOW, slices)

        assert [s.name for s in out][:1] == ["Sony engagement: variants"]
        assert "Sony engagement: fabricated" not in [s.name for s in out]
        assert db.record_slices.call_args[0][1][0]["metrics"]["resolved"] == 4

        drops = [r.getMessage() for r in caplog.records if "Dropping slice" in r.getMessage()]
        assert len(drops) == 1
        msg = drops[0]
        assert "none of its 4 memory ids resolve" in msg and "0/4 resolved" in msg
        assert FABRICATED in msg and "fake-id-2" in msg and "fake-id-3" in msg
        assert "fake-id-4" not in msg                              # first three only
        assert SECRET not in msg

        pre = orch._explorer_pre_budget["slices"]
        assert [(p["requested"], p["resolved"]) for p in pre] == [(4, 4), (4, 0)]
        assert pre[0]["memory_ids"] == good                         # canonical form persisted

    def test_enforce_budget_drop_log_handles_empty_and_non_list(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.allocation"):
            allocation.enforce_budget([MemorySlice(name="e", strategy="x", memory_ids=[])],
                                      [{"requested": 0, "resolved": 0}])
        assert "first unresolved: none" in caplog.text


class TestPipelinePersistsPreBudget:
    def test_circuit_breaker_run_stats_carry_pre_budget_slices(self):
        """The 10-14 shape: every Explorer slice dropped. Nothing arrived, so the run
        aborts — and the run row still says what the Explorer proposed."""
        orch = _orch()
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": [{"name": "Sony engagement: a", "strategy": "x",
                                                   "memory_ids": [FABRICATED, "fake-2"], "memory_titles": [SECRET] * 2}],
                                       "raw": "[]", "usage": None, "usage_source": "estimate"}
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.ledger.get_memory_provenance", return_value=[]), \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "_invoke_metered", side_effect=lambda role, **kw: invoker.invoke(**kw)), \
             patch.object(orch, "_capture"), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            db.get_arrivals_summary.return_value = []
            db.get_strategy_usage.return_value = {}
            db.get_previous_run_started_at.return_value = WINDOW
            db.get_recent_slice_memory_ids.return_value = set()
            db.get_recent_parent_memories.return_value = []
            result = orch.run("scheduled")
        assert result.aborted_early is True
        stats = db.complete_run.call_args.kwargs["stats"]
        pre = stats["explorer_pre_budget"]
        assert pre["parsed_slices"] == 1
        assert pre["slices"] == [{"name": "Sony engagement: a", "strategy": "x", "requested": 2,
                                  "memory_ids": [FABRICATED, "fake-2"], "resolved": 0}]
        assert SECRET not in repr(stats)
