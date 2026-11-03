"""Phase 4 of docs/PLAN-skim-stage.md — judging order and budget-aware Thinker / panel.

Run 3a1263a6 (2026-09-18) generated candidates for every slice, judged them in
slice order with the guaranteed arrivals slice last, and the spend cap cut the
arrivals slice first. Now the guaranteed slice is judged first, Explorer slices
follow by arrival share, the Thinker is skipped for slices whose candidates the
remaining budget could not judge, and the panel stops softly instead of tripping
the hard cap mid-candidate. Hermetic: fake invokers, a SpendTracker with a small
cap, mocked DB.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from src.backends.cost import SpendCapExceeded, SpendTracker
from src.dream_cycle import allocation
from src.dream_cycle import orchestrator as o
from src.dream_cycle.orchestrator import DreamCycleOrchestrator
from src.models import CandidateInsight, EvaluatorVerdict, MemorySlice



@pytest.fixture(autouse=True)
def _coordination_only_authorization(monkeypatch):
    """This suite isolates pipeline coordination with placeholder memory ids
    ("m1", "uuid-1") and no slice snapshots. Pre-spend authorization (R3 fix 4)
    is pinned with real UUIDs on the disposable database in
    tests/test_discovery_integrity.py and tests/test_integration.py."""
    monkeypatch.setattr("src.dream_cycle.orchestrator.authorize_candidate", lambda candidate, context: set())

def _slice(name, strategy="stale_synthesis_check"):
    return MemorySlice(name=name, strategy=strategy, memory_ids=["m1", "m2"], memory_titles=["a", "b"])


def _row(origin="explorer", arrival_share=None):
    metrics = {} if arrival_share is None else {"arrival_share": arrival_share, "resolved": 2}
    return {"origin": origin, "metrics": metrics}


def _cand(title):
    return CandidateInsight(title=title, type="insight", operation="CREATE", content=f"WHAT: {title}",
                            source_memories=["m1"], relationships=[], strategy_that_found_it="arrivals_synthesis")


def _orch(cap=9.0, spent=0.0):
    with patch("src.dream_cycle.orchestrator.default_resolver"):
        orch = DreamCycleOrchestrator()
    orch._spend = SpendTracker(cap_usd=cap)
    orch._spend.total_usd = spent
    return orch


REJECT = EvaluatorVerdict(role="x", verdict="REJECT", reasoning="no")


# --------------------------------------------------------------------------- #
# order_slices                                                                 #
# --------------------------------------------------------------------------- #
class TestOrderSlices:
    def test_guaranteed_first_then_arrival_share_desc_then_unmetered_stable(self):
        slices = [_slice("stale"), _slice("mixed"), _slice("unknown"), _slice("Arrivals", "arrivals_synthesis"),
                  _slice("fresh"), _slice("also-mixed")]
        rows = [_row(arrival_share=0.0), _row(arrival_share=0.5), _row(), _row(allocation.ARRIVALS_ORIGIN, 1.0),
                _row(arrival_share=0.9), _row(arrival_share=0.5)]
        ordered = allocation.order_slices(slices, rows)
        assert [s.name for s, _ in ordered] == ["Arrivals", "fresh", "mixed", "also-mixed", "stale", "unknown"]
        assert ordered[0][1]["origin"] == allocation.ARRIVALS_ORIGIN          # rows travel with their slices
        assert ordered[-1][1] == rows[2]

    def test_missing_or_misaligned_rows_keep_the_explorer_order(self):
        slices = [_slice("b"), _slice("a")]
        assert [s.name for s, r in allocation.order_slices(slices, None)] == ["b", "a"]
        assert [s.name for s, r in allocation.order_slices(slices, [])] == ["b", "a"]
        assert [(s.name, r) for s, r in allocation.order_slices(slices, [_row(arrival_share=1.0)])] == [("b", {}), ("a", {})]
        assert allocation.order_slices([], []) == []

    def test_describe_slice_order(self):
        ordered = allocation.order_slices([_slice("stale"), _slice("Arrivals")],
                                          [_row(arrival_share=0.25), _row(allocation.ARRIVALS_ORIGIN, 1.0)])
        assert allocation.describe_slice_order(ordered) == \
               "1. [guaranteed_arrivals] Arrivals (arrivals 100%); 2. [explorer] stale (arrivals 25%)"
        assert allocation.describe_slice_order([(_slice("x"), {})]) == "1. [explorer] x"


# --------------------------------------------------------------------------- #
# Pipeline: ordering                                                           #
# --------------------------------------------------------------------------- #
def _run(orch, slices, rows, thinker, evaluator=None):
    """Drive run('scheduled') with the ledger rows injected the way _allocate_attention sets them.
    run() would replace the tracker from the environment; keep the test's tracker (cap + prior spend)."""
    def allocate(run_id, run_type, started_at, sl):
        orch._slice_rows = rows
        return sl

    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
         patch("src.dream_cycle.orchestrator.SpendTracker.from_env", return_value=orch._spend), \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "invoke_explorer", return_value=slices), \
         patch.object(orch, "_allocate_attention", side_effect=allocate), \
         patch.object(orch, "invoke_thinker", side_effect=thinker) as th, \
         patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
         patch.object(orch, "invoke_evaluator", side_effect=evaluator or (lambda c, r: REJECT)) as ev, \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
         patch("src.dream_cycle.orchestrator.generate_digest", return_value="digest.md") as digest:
        db.create_run.return_value = "run-b"
        db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
        result = orch.run("scheduled")
    return result, db, th, ev, digest


class TestPipelineOrdering:
    def test_thinker_sees_the_guaranteed_slice_first_and_the_order_is_logged(self, caplog):
        orch = _orch()
        slices = [_slice("stale"), _slice("fresh"), _slice("Arrivals", "arrivals_synthesis")]
        rows = [_row(arrival_share=0.1), _row(arrival_share=0.8), _row(allocation.ARRIVALS_ORIGIN, 1.0)]
        seen = []
        with caplog.at_level(logging.INFO, logger="src.dream_cycle.orchestrator"):
            result, db, th, ev, _ = _run(orch, slices, rows, thinker=lambda s: seen.append(s.name) or [])
        assert seen == ["Arrivals", "fresh", "stale"]
        assert result.aborted_early is False
        assert any("Slice order for the Thinker and the panel: 1. [guaranteed_arrivals] Arrivals (arrivals 100%); "
                   "2. [explorer] fresh (arrivals 80%); 3. [explorer] stale (arrivals 10%)" in r.getMessage()
                   for r in caplog.records)
        # explorer_output on the run record follows the judging order too.
        assert '"name": "Arrivals"' in db.complete_run.call_args.kwargs["explorer_output"].split('"name": "fresh"')[0]

    def test_without_ledger_rows_the_explorer_order_is_kept(self):
        orch = _orch()
        seen = []
        _run(orch, [_slice("b"), _slice("a")], [], thinker=lambda s: seen.append(s.name) or [])
        assert seen == ["b", "a"]


# --------------------------------------------------------------------------- #
# Pipeline: budget-aware Thinker                                               #
# --------------------------------------------------------------------------- #
class TestBudgetAwareThinker:
    def test_constants_document_the_source_and_values(self):
        assert o.EST_THINKER_CALL_USD == 0.30 and o.EST_JUDGE_CANDIDATE_USD == 0.70

    def test_remaining_budget_reads_the_tracker(self):
        orch = _orch(cap=9.0, spent=2.5)
        assert orch._budget_remaining() == pytest.approx(6.5)
        with pytest.raises(ValueError, match="finite and positive"):
            SpendTracker(cap_usd=0)

    def test_slices_are_skipped_once_pending_judging_exhausts_the_budget(self, caplog):
        # $3.00 left. Slice 1 yields 3 candidates ($2.10 of judging pending); slice 2 would
        # need $0.30 + $1.40 + $2.10 = $3.80 → skipped, and so is slice 3.
        orch = _orch(cap=3.0)
        slices = [_slice("first"), _slice("second"), _slice("third")]
        rows = [_row(arrival_share=1.0), _row(arrival_share=0.5), _row(arrival_share=0.0)]
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.orchestrator"):
            result, db, th, ev, digest = _run(orch, slices, rows,
                                              thinker=lambda s: [_cand(f"{s.name}-{i}") for i in range(3)])
        assert th.call_count == 1 and th.call_args.args[0].name == "first"
        assert result.candidates_generated == 2 and result.candidates_rejected == 2
        assert result.aborted_early is False
        stats = db.complete_run.call_args.kwargs["stats"]
        assert stats["slices_skipped_for_budget"] == 2 and "candidates_unjudged_for_budget" not in stats
        msg = [r.getMessage() for r in caplog.records if "skipping the Thinker" in r.getMessage()]
        assert len(msg) == 1 and "$3.00 left of $3.00" in msg[0] and "needs ~$3.10" in msg[0]
        assert "2 pending candidates" in msg[0] and "remaining 2 slice(s): second; third" in msg[0]
        digest.assert_called_once()

    def test_enough_budget_runs_every_slice_and_records_no_budget_keys(self):
        orch = _orch(cap=9.0)
        slices = [_slice("first"), _slice("second")]
        result, db, th, *_ = _run(orch, slices, [_row(arrival_share=1.0), _row(arrival_share=0.0)],
                                  thinker=lambda s: [_cand(s.name)])
        assert th.call_count == 2 and result.candidates_generated == 2
        stats = db.complete_run.call_args.kwargs["stats"]
        assert "slices_skipped_for_budget" not in stats and "candidates_unjudged_for_budget" not in stats

    def test_sufficient_finite_cap_means_no_skipping(self):
        orch = _orch(cap=1000.0)
        slices = [_slice("first"), _slice("second"), _slice("third")]
        result, db, th, *_ = _run(orch, slices, [], thinker=lambda s: [_cand(f"{s.name}-{i}") for i in range(3)])
        assert th.call_count == 3 and result.candidates_generated == 6

    def test_thinker_skip_uses_real_spend_not_estimates(self):
        """$9 cap but $7.50 already spent: the very first slice is skipped."""
        orch = _orch(cap=9.0, spent=7.50)
        result, db, th, *_ = _run(orch, [_slice("only")], [_row(allocation.ARRIVALS_ORIGIN, 1.0)],
                                  thinker=lambda s: [_cand("x")])
        th.assert_not_called()
        assert result.candidates_generated == 0 and result.aborted_early is False
        assert db.complete_run.call_args.kwargs["stats"]["slices_skipped_for_budget"] == 1

    def test_thinker_cap_stops_later_slices_and_preserves_generated_count(self, monkeypatch):
        monkeypatch.setattr(o, "EST_THINKER_CALL_USD", 0.0)
        monkeypatch.setattr(o, "EST_JUDGE_CANDIDATE_USD", 0.0)
        orch = _orch(cap=1.0)
        def thinker(memory_slice):
            if memory_slice.name == "first":
                return [_cand("already generated")]
            raise SpendCapExceeded("synthetic budget stop")
        result, db, th, ev, digest = _run(
            orch, [_slice("first"), _slice("second"), _slice("never reached")], [], thinker)
        assert th.call_count == 2
        ev.assert_not_called()
        digest.assert_not_called()
        assert result.aborted_early is True
        assert result.candidates_generated == 1
        stats = db.complete_run.call_args.kwargs["stats"]
        assert stats["candidates_generated"] == 1 and stats["aborted"] is True


# --------------------------------------------------------------------------- #
# Pipeline: soft stop in the panel                                             #
# --------------------------------------------------------------------------- #
class TestPanelSoftStop:
    def _spending_evaluator(self, orch, per_vote):
        def evaluate(candidate, role):
            orch._spend.total_usd += per_vote          # what strict invocation accounting would have added
            return REJECT
        return evaluate

    def test_remaining_candidates_are_left_unjudged_and_the_run_completes(self, caplog):
        # $2.00 cap; four votes at $0.40 = $1.60 for the first candidate → $0.40 left < $0.70.
        orch = _orch(cap=2.0)
        with caplog.at_level(logging.WARNING, logger="src.dream_cycle.orchestrator"):
            result, db, th, ev, digest = _run(orch, [_slice("s")], [_row(allocation.ARRIVALS_ORIGIN, 1.0)],
                                              thinker=lambda s: [_cand("judged"), _cand("left"), _cand("also left")],
                                              evaluator=self._spending_evaluator(orch, 0.40))
        assert ev.call_count == 4                                    # one candidate, four votes, then stop
        assert result.aborted_early is False
        assert result.candidates_generated == 2 and result.candidates_rejected == 1 and result.candidates_accepted == 0
        assert db.store_candidate.call_count == 1                    # no verdict stored or fabricated for the rest
        assert db.store_candidate.call_args.args[1]["title"] == "judged"
        stats = db.complete_run.call_args.kwargs["stats"]
        assert stats["candidates_unjudged_for_budget"] == 1 and stats["candidates_rejected"] == 1
        digest.assert_called_once()                                  # digest + complete_run: a normal completion
        db.complete_run.assert_called_once()
        msg = [r.getMessage() for r in caplog.records if "unjudged" in r.getMessage()]
        assert len(msg) == 1 and "$0.40 left of $2.00" in msg[0] and "leaving 1 candidate(s) unjudged: left" in msg[0]

    def test_soft_stop_before_any_candidate_when_the_thinker_used_the_budget(self):
        orch = _orch(cap=2.0)

        def thinker(s):
            orch._spend.total_usd += 1.50                            # an expensive Thinker call
            return [_cand("c1")]

        result, db, th, ev, _ = _run(orch, [_slice("s")], [_row(allocation.ARRIVALS_ORIGIN, 1.0)], thinker=thinker)
        ev.assert_not_called()
        assert result.candidates_generated == 1 and result.candidates_rejected == 0
        assert db.complete_run.call_args.kwargs["stats"]["candidates_unjudged_for_budget"] == 1

    def test_hard_cap_stays_the_backstop_when_the_soft_stop_is_bypassed(self, monkeypatch):
        monkeypatch.setattr(o, "EST_THINKER_CALL_USD", 0.0)
        monkeypatch.setattr(o, "EST_JUDGE_CANDIDATE_USD", 0.0)
        orch = _orch(cap=0.01)

        def evaluate(candidate, role):
            raise SpendCapExceeded("cap")

        result, db, th, ev, digest = _run(orch, [_slice("s")], [], thinker=lambda s: [_cand("c1")], evaluator=evaluate)
        assert ev.call_count == 1 and result.aborted_early is True
        digest.assert_not_called()
        db.complete_run.assert_called_once()
        assert db.complete_run.call_args.kwargs["stats"]["candidates_generated"] == 1
        assert db.complete_run.call_args.kwargs["stats"]["aborted"] is True

    def test_grounding_skip_band_still_votes(self):
        """Between 80% of cap (grounding audit skipped) and cap-$0.70 (soft stop) the panel still votes."""
        orch = _orch(cap=5.0)

        def thinker(s):
            orch._spend.total_usd += 4.10                            # 82% spent, $0.90 left ≥ $0.70
            return [_cand("c1")]

        result, db, th, ev, _ = _run(orch, [_slice("s")], [], thinker=thinker)
        assert ev.call_count == 4 and result.candidates_rejected == 1
        assert "candidates_unjudged_for_budget" not in db.complete_run.call_args.kwargs["stats"]
