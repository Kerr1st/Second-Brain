"""Attention allocation: feedback bounds, arrivals manifest, slice budget,
guaranteed arrivals slice, prompt contracts, and the orchestrator hook (hermetic)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.dream_cycle import allocation, feedback, ledger
from src.dream_cycle.orchestrator import DreamCycleOrchestrator
from src.models import MemorySlice
from src.prompts import get_explorer_prompt, get_thinker_prompt

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
WINDOW = NOW - timedelta(days=1)


def _uid():
    return str(uuid.uuid4())


def _prov(mid, source_type="slack", type="source", created=NOW, parent=None):
    return {"id": mid, "type": type, "source_type": source_type, "created_at": created, "parent_id": parent, "project": None}


# --------------------------------------------------------------------------- #
# Feedback injection is bounded and reframed                                   #
# --------------------------------------------------------------------------- #
class TestFeedbackBounds:
    def _rows(self, n_cycles, per_cycle, reasoning_chars=1600):
        rows = []
        for c in range(n_cycles):
            for i in range(per_cycle):
                rows.append({
                    "run_id": f"run-{c}", "run_type": "scheduled",
                    "completed_at": datetime(2026, 9, 17 - c, tzinfo=timezone.utc),
                    "candidate_id": f"c{c}-{i}", "final_verdict": "REJECTED",
                    "evaluator_a_verdict": "REJECT",
                    "evaluator_a_reasoning": ("The engineering findings are grounded, but the claim overstates. "
                                              + "x" * reasoning_chars),
                    "evaluator_b_verdict": "ACCEPT", "evaluator_b_reasoning": "fine",
                    "evaluator_c_verdict": "REJECT",
                    "evaluator_c_reasoning": "Evidence independence is overstated.\n1. Detail " + "y" * reasoning_chars,
                    "evaluator_d_verdict": "ACCEPT", "evaluator_d_reasoning": "ok",
                })
        return rows

    def test_block_is_bounded_and_keeps_first_sentences(self):
        with patch("src.dream_cycle.feedback.dream_cycle_db") as db:
            db.get_recent_rejections.return_value = self._rows(3, 7)      # 21 candidates x 2 x 1.6K ≈ 70K raw
            db.get_accepted_dissents.return_value = []
            db.get_user_rejections.return_value = []
            out = feedback.build_feedback_injection()
        assert len(out) <= feedback.MAX_FEEDBACK_CHARS
        assert "## Lessons from recent cycles" in out and "NOT topics to revisit" in out
        assert 'for: "The engineering findings are grounded, but the claim overstates."' in out
        assert 'for: "Evidence independence is overstated."' in out
        assert "xxxxxxxx" not in out                                    # long tails cut
        assert "more rejected candidates omitted" in out               # per-cycle cap applied

    def test_oldest_cycle_trimmed_first(self):
        with patch("src.dream_cycle.feedback.dream_cycle_db") as db:
            db.get_recent_rejections.return_value = self._rows(2, 6, reasoning_chars=120)
            db.get_accepted_dissents.return_value = []
            db.get_user_rejections.return_value = []
            with patch.object(feedback, "MAX_FEEDBACK_CHARS", 1500):
                out = feedback.build_feedback_injection()
        assert "Cycle 2026-09-17" in out and "Cycle 2026-09-16" not in out

    def test_user_rejections_survive_trimming(self):
        with patch("src.dream_cycle.feedback.dream_cycle_db") as db:
            db.get_recent_rejections.return_value = self._rows(2, 6)
            db.get_accepted_dissents.return_value = []
            db.get_user_rejections.return_value = [{"candidate_json": {"title": "Wrong about Boeing"},
                                                    "user_rejection_reason": "Not what happened."}]
            out = feedback.build_feedback_injection()
        assert '"Wrong about Boeing": "Not what happened."' in out

    def test_first_sentence_helper(self):
        assert feedback._first_sentence("A. B. C.") == "A."
        assert feedback._first_sentence("No terminator here") == "No terminator here"
        assert feedback._first_sentence("REJECT on grounding.  1. The supplied memory\nis broad.") == "REJECT on grounding."
        assert feedback._first_sentence("w" * 500).endswith("…") and len(feedback._first_sentence("w" * 500)) == feedback.MAX_REASON_CHARS


# --------------------------------------------------------------------------- #
# Manifest                                                                     #
# --------------------------------------------------------------------------- #
class TestManifest:
    def test_manifest_lists_channels_units_and_ids(self):
        arrivals = [
            {"source_type": "slack", "documents": 79, "memories": 297,
             "samples": [{"id": "id-1", "title": "Slack: DM with grace — 2026-09-17", "created_at": "2026-09-17T10:00:00+00:00"}]},
            {"source_type": "outlook_email", "documents": 32, "memories": 64, "samples": []},
            {"source_type": "generated:insight", "documents": 3, "memories": 3, "samples": []},
        ]
        text = allocation.build_arrivals_manifest(WINDOW, arrivals)
        assert text.startswith("## What arrived since the previous run (2026-09-17 09:00 UTC)")
        assert "114 documents / 364 memories across 3 channels" in text
        assert "- slack: 79 conversation-days (297 memories)" in text
        assert "    - 2026-09-17 Slack: DM with grace — 2026-09-17 [id-1]" in text
        assert "- outlook_email: 32 emails (64 memories)" in text
        assert "- generated:insight: 3 documents (3 memories)" in text

    def test_manifest_truncates_and_handles_empty(self):
        arrivals = [{"source_type": f"ch{i}", "documents": 1, "memories": 1,
                     "samples": [{"id": _uid(), "title": "t" * 90, "created_at": "2026-09-17"}] * 4} for i in range(60)]
        text = allocation.build_arrivals_manifest(WINDOW, arrivals, max_chars=1200)
        assert len(text) <= 1200 and text.endswith("(truncated)")
        assert "Nothing new was captured" in allocation.build_arrivals_manifest(WINDOW, [])


# --------------------------------------------------------------------------- #
# Budget enforcement                                                           #
# --------------------------------------------------------------------------- #
def _m(resolved=12, arrival=0.0, sources=1, dominant="distilled_chat", conc=1.0):
    return {"requested": resolved, "resolved": resolved, "arrival_share": arrival, "source_types": sources,
            "dominant_source": dominant, "concentration": conc, "episodic_share": 0.0, "source_mix": {}}


class TestEnforceBudget:
    def test_required_anchored(self):
        assert [allocation.required_anchored(n) for n in range(6)] == [0, 1, 1, 2, 2, 3]

    def test_drops_unresolvable_and_requests_arrivals_slice(self):
        slices = [MemorySlice(name="ghost", strategy="x", memory_ids=["nope"]),
                  MemorySlice(name="stale", strategy="stale_synthesis_check", memory_ids=["a"] * 12)]
        kept, kept_m, needs = allocation.enforce_budget(slices, [_m(resolved=0), _m()])
        assert [s.name for s in kept] == ["stale"] and needs is True

    def test_anchored_slices_satisfy_budget(self):
        slices = [MemorySlice(name="week", strategy="arrivals_synthesis", memory_ids=["a"] * 12),
                  MemorySlice(name="old", strategy="orphan_archaeology", memory_ids=["b"] * 12)]
        kept, _, needs = allocation.enforce_budget(slices, [_m(arrival=0.8, sources=3, dominant="slack", conc=0.5), _m()])
        assert len(kept) == 2 and needs is False

    def test_concentration_cuts_to_two_regardless_of_topic(self):
        slices = [MemorySlice(name=f"s{i}", strategy="stale_synthesis_check", memory_ids=["a"] * 12) for i in range(4)]
        metrics = [_m(dominant="slack", conc=0.9) for _ in range(4)]   # four slices, all one channel, none anchored
        kept, _, needs = allocation.enforce_budget(slices, metrics)
        assert [s.name for s in kept] == ["s0", "s1"] and needs is True

    def test_two_from_one_channel_is_fine(self):
        slices = [MemorySlice(name=f"s{i}", strategy="x", memory_ids=["a"] * 12) for i in range(2)]
        kept, _, _ = allocation.enforce_budget(slices, [_m(dominant="slack", conc=0.9)] * 2)
        assert len(kept) == 2

    def test_empty_explorer_output_still_requests_arrivals(self):
        kept, _, needs = allocation.enforce_budget([], [])
        assert kept == [] and needs is True


# --------------------------------------------------------------------------- #
# Guaranteed arrivals slice                                                    #
# --------------------------------------------------------------------------- #
class TestArrivalsSlice:
    def test_allocation_is_sqrt_proportional_and_capped(self):
        alloc = allocation.allocate_stratified(
            {"quick_desktop_feed": 192, "article": 150, "slack": 79, "outlook_email": 32,
             "outlook_calendar": 25, "browser_history": 7, "kiro_cli_chat": 5}, size=14)
        assert sum(alloc.values()) == 14
        assert max(alloc.values()) <= allocation.ARRIVALS_PER_SOURCE_CAP
        assert all(v >= 1 for v in alloc.values())                       # every channel represented
        assert alloc["quick_desktop_feed"] <= alloc["slack"] + 2         # no channel crowds the rest out

    def test_allocation_edge_cases(self):
        assert allocation.allocate_stratified({}, 14) == {}
        assert allocation.allocate_stratified({"slack": 2}, 14) == {"slack": 2}
        assert allocation.allocate_stratified({"a": 100, "b": 100}, 0) == {}

    def test_compose_prefers_episodes_and_caps_semantic(self):
        rows = []
        for ch in ("slack", "outlook_email", "browser_history", "article"):
            rows += [{"id": _uid(), "title": f"{ch} doc {i}", "type": "source", "source_type": ch,
                      "created_at": NOW - timedelta(hours=i)} for i in range(6)]
        rows += [{"id": _uid(), "title": f"insight {i}", "type": "insight", "source_type": None,
                  "created_at": NOW} for i in range(10)]
        s = allocation.compose_arrivals_slice(WINDOW, rows, size=14)
        assert s.strategy == allocation.ARRIVALS_STRATEGY
        assert len(s.memory_ids) == 14 and len(s.memory_titles) == 14
        n_semantic = sum(1 for t in s.memory_titles if t.startswith("insight"))
        assert n_semantic <= allocation.MAX_SEMANTIC_IN_ARRIVALS
        assert s.name.startswith("Arrivals since 2026-09-17:") and "slack" in s.name
        assert "not abstract principles" in s.hypothesis

    def test_compose_with_only_generated_or_nothing(self):
        assert allocation.compose_arrivals_slice(WINDOW, []) is None
        rows = [{"id": _uid(), "title": "i", "type": "insight", "source_type": None, "created_at": NOW} for _ in range(5)]
        s = allocation.compose_arrivals_slice(WINDOW, rows)
        assert s is not None and len(s.memory_ids) == allocation.MAX_SEMANTIC_IN_ARRIVALS


# --------------------------------------------------------------------------- #
# Prompt contracts                                                             #
# --------------------------------------------------------------------------- #
class TestPromptContracts:
    def test_explorer_prompt_names_sources_budget_and_strategy_12(self):
        p = get_explorer_prompt(memory_count=1, date_range="x", feedback_injection="## Lessons from recent cycles\nfb",
                                run_type="scheduled", strategy_usage={"stale_synthesis_check": 8})
        assert "## Where memories come from" in p and "slack — one document per conversation-day" in p
        assert "12. ARRIVALS SYNTHESIS" in p and "memory_recent_activity" in p
        assert "## Slice budget (enforced by the orchestrator)" in p and "ARRIVAL-ANCHORED" in p
        assert "neither privileged nor excluded" in p                      # no topic cap
        assert "arrivals_synthesis" in p                                   # in UCB1 list and output format
        # Feedback comes after the strategies and budget, not before them.
        assert p.index("## Lessons from recent cycles") > p.index("## Slice budget")
        assert p.index("## Where memories come from") < p.index("1. TEMPORAL JUXTAPOSITION")

    def test_thinker_prompt_gates_meta_and_frames_arrivals(self):
        t = get_thinker_prompt()
        assert "10. ARRIVALS" in t and "arrivals_synthesis" in t and "Cite the episodes" in t
        assert "Only when the slice itself is about this system's history" in t


# --------------------------------------------------------------------------- #
# Orchestrator hook                                                            #
# --------------------------------------------------------------------------- #
class TestAllocateAttention:
    def _orch(self):
        with patch("src.dream_cycle.orchestrator.default_resolver"):
            return DreamCycleOrchestrator()

    def test_adds_guaranteed_slice_and_persists_ledger(self):
        old_ids = [_uid() for _ in range(12)]
        new_rows = [{"id": _uid(), "title": f"Slack day {i}", "type": "source", "source_type": "slack", "created_at": NOW}
                    for i in range(6)] + \
                   [{"id": _uid(), "title": f"Email {i}", "type": "source", "source_type": "outlook_email", "created_at": NOW}
                    for i in range(6)]
        prov_rows = [_prov(i, "distilled_chat", "insight", NOW - timedelta(days=40)) for i in old_ids] + \
                    [_prov(r["id"], r["source_type"]) for r in new_rows]
        explorer_slices = [MemorySlice(name="stale", strategy="stale_synthesis_check", memory_ids=old_ids)]

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db:
            db.get_previous_run_started_at.return_value = WINDOW
            db.get_recent_slice_memory_ids.return_value = set(old_ids[:6])
            db.get_memory_provenance.side_effect = lambda ids: [r for r in prov_rows if r["id"] in set(ids)]
            db.get_recent_parent_memories.return_value = new_rows
            out = self._orch()._allocate_attention("run-1", "scheduled", NOW, explorer_slices)

        assert [s.strategy for s in out] == ["stale_synthesis_check", allocation.ARRIVALS_STRATEGY]
        assert len(out[1].memory_ids) == 8                                  # 6 slack + 6 email, capped at 4 per channel
        db.record_slices.assert_called_once()
        rows = db.record_slices.call_args[0][1]
        assert [r["origin"] for r in rows] == ["explorer", allocation.ARRIVALS_ORIGIN]
        assert rows[0]["metrics"]["arrival_share"] == 0.0 and rows[0]["metrics"]["revisit_share"] == 0.5
        assert rows[1]["metrics"]["arrival_share"] == 1.0 and rows[1]["metrics"]["source_types"] == 2

    def test_scoped_runs_are_recorded_but_not_budgeted(self):
        ids = [_uid() for _ in range(3)]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db:
            db.get_previous_run_started_at.return_value = None
            db.get_recent_slice_memory_ids.return_value = set()
            db.get_memory_provenance.return_value = [_prov(i, "distilled_chat", "insight", NOW - timedelta(days=40)) for i in ids]
            out = self._orch()._allocate_attention("run-2", "user_triggered", NOW,
                                                   [MemorySlice(name="topic", strategy="x", memory_ids=ids)])
        assert [s.name for s in out] == ["topic"]
        db.get_recent_parent_memories.assert_not_called()
        db.record_slices.assert_called_once()

    def test_unavailable_provenance_leaves_slices_untouched(self):
        """Fully mocked DB (as in the older orchestrator tests): no judgement, no ledger."""
        slices = [MemorySlice(name="s", strategy="x", memory_ids=["mem-1", "mem-2"])]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db:   # every call returns MagicMock
            out = self._orch()._allocate_attention("run-3", "scheduled", NOW, slices)
        assert out == slices
        db.record_slices.assert_not_called()

    def test_failure_is_non_fatal(self):
        slices = [MemorySlice(name="s", strategy="x", memory_ids=[_uid()])]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db:
            db.get_previous_run_started_at.side_effect = RuntimeError("db down")
            out = self._orch()._allocate_attention("run-4", "scheduled", NOW, slices)
        assert out == slices

    def test_explorer_user_message_carries_manifest(self):
        orch = self._orch()
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": [], "raw": "[]", "usage": None, "usage_source": "estimate"}
        arrivals = [{"source_type": "slack", "documents": 3, "memories": 9,
                     "samples": [{"id": "id-9", "title": "Slack: #eng — 2026-09-17", "created_at": "2026-09-17T08:00:00+00:00"}]}]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_invoker_for", return_value=invoker), \
             patch.object(orch, "_record_spend"), patch.object(orch, "_capture"):
            db.get_previous_run_started_at.return_value = WINDOW
            db.get_arrivals_summary.return_value = arrivals
            db.get_strategy_usage.return_value = {}
            orch._current_run_id = "run-5"
            orch._current_started_at = NOW
            orch.invoke_explorer("", "scheduled", None,
                                 stats={"total_count": 10, "recent_activity": 9, "date_range": {"min": NOW, "max": NOW}})
        msg = invoker.invoke.call_args.kwargs["user_message"]
        assert "## What arrived since the previous run (2026-09-17 09:00 UTC)" in msg
        assert "- slack: 3 conversation-days (9 memories)" in msg and "[id-9]" in msg
        assert "meet the slice budget" in msg




# --------------------------------------------------------------------------- #
# Lessons from the first assembled run (2026-09-17 18:41)                      #
# --------------------------------------------------------------------------- #
class TestArrivalWindowClamp:
    """A re-run 13h after the nightly run saw 'nothing new'; the window now has a floor."""

    def _dcdb(self, prev):
        db = MagicMock()
        db.get_previous_run_started_at.return_value = prev
        return db

    def test_window_never_shorter_than_a_day(self):
        prev = NOW - timedelta(hours=13)
        assert ledger.arrival_window_start("r", "scheduled", NOW, dcdb=self._dcdb(prev)) == NOW - ledger.MIN_ARRIVAL_WINDOW

    def test_window_anchored_on_previous_run_when_older_than_a_day(self):
        prev = NOW - timedelta(days=3)
        assert ledger.arrival_window_start("r", "scheduled", NOW, dcdb=self._dcdb(prev)) == prev

    def test_window_never_longer_than_two_weeks(self):
        prev = NOW - timedelta(days=40)
        assert ledger.arrival_window_start("r", "scheduled", NOW, dcdb=self._dcdb(prev)) == NOW - ledger.MAX_ARRIVAL_WINDOW

    def test_no_previous_run_falls_back_to_seven_days(self):
        assert ledger.arrival_window_start("r", "scheduled", NOW, dcdb=self._dcdb(None)) == NOW - ledger.DEFAULT_ARRIVAL_WINDOW
        assert ledger.arrival_window_start("r", "scheduled", NOW, dcdb=MagicMock()) == NOW - ledger.DEFAULT_ARRIVAL_WINDOW


class TestExplorerFailureFallback:
    """On a scheduled run an Explorer timeout continues with the guaranteed arrivals slice."""

    def _orch(self):
        with patch("src.dream_cycle.orchestrator.default_resolver"):
            return DreamCycleOrchestrator()

    def test_scheduled_run_continues_with_arrivals_slice(self):
        orch = self._orch()
        arrivals_slice = MemorySlice(name="Arrivals", strategy=allocation.ARRIVALS_STRATEGY, memory_ids=[_uid()])
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", side_effect=TimeoutError("600s")), \
             patch.object(orch, "_allocate_attention", return_value=[arrivals_slice]) as alloc, \
             patch.object(orch, "invoke_thinker", return_value=[]), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):
            db.create_run.return_value = "run-x"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("scheduled")
        alloc.assert_called_once()
        assert alloc.call_args[0][3] == []                       # Explorer contributed nothing
        assert result.aborted_early is False                     # the run went on with the arrivals slice

    def test_user_triggered_run_still_aborts(self):
        orch = self._orch()
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", side_effect=RuntimeError("crash")), \
             patch.object(orch, "_allocate_attention") as alloc, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""):
            db.create_run.return_value = "run-y"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("user_triggered", scope={"topic": "x"})
        alloc.assert_not_called()
        assert result.aborted_early is True

    def test_explorer_timeout_is_generous_and_env_tunable(self):
        from src.dream_cycle import orchestrator as o
        assert o.EXPLORER_TIMEOUT_S >= 1200
