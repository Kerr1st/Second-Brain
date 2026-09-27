"""Pre-panel grounding audit + one-shot revision (hermetic).

docs/PLAN-panel-acceptance.md, Decisions 2026-09-18: one graded audit call by the
Skeptic seat's backend; if it finds defects, one tool-less repair by the Thinker's
backend; the panel then votes on the repaired candidate. Never aborts a run.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.backends.cost import SpendCapExceeded, SpendTracker
from src.dream_cycle import orchestrator as o
from src.dream_cycle.orchestrator import DreamCycleOrchestrator
from src.models import CandidateInsight, EvaluatorVerdict, MemorySlice
from src.prompts.revision import REVISION_PROMPT, REVISION_SCHEMA, build_revision_request

SRC_A, SRC_B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def _cand(**kw):
    base = dict(title="Every Amazon message says no action needed", type="insight", operation="CREATE",
                content="WHAT: every message... EVIDENCE: ... WHY IT MATTERS: ...", confidence="high",
                source_memories=[SRC_A, SRC_B], strategy_that_found_it="arrivals_synthesis")
    base.update(kw)
    return CandidateInsight(**base)


def _orch(cap=6.0, spent=0.0):
    orch = DreamCycleOrchestrator()
    orch._spend = SpendTracker(cap_usd=cap)
    orch._spend.total_usd = spent
    orch._slice_memory_cache = {SRC_A: {"id": SRC_A, "title": "Notice 1", "content": "no action needed"},
                                SRC_B: {"id": SRC_B, "title": "Notice 2", "content": "decision letter will follow"}}
    orch._current_run_id = "run-g"
    return orch


def _audit(blocking=(), advisory=(), score=30, verdict="REJECT"):
    return EvaluatorVerdict(role="skeptic", verdict=verdict, reasoning="r", score=score,
                            blocking_issues=list(blocking), advisory_issues=list(advisory))


class TestGradedVerdictParsing:
    def test_invoke_evaluator_parses_score_and_issue_lists(self):
        orch = _orch()
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": {"verdict": "REJECT", "score": "31", "reasoning": "(a) fails",
                                                  "blocking_issues": ["fax number not in sources", ""],
                                                  "advisory_issues": "overreach"},
                                       "usage": {"input_tokens": 10, "output_tokens": 5}}
        with patch.object(orch, "_invoker_for", return_value=invoker), patch.object(orch, "_role_has_tools", return_value=False), \
             patch.object(orch, "_capture"):
            v = orch.invoke_evaluator(_cand(), "skeptic", stage="grounding_audit")
        assert (v.verdict, v.score) == ("REJECT", 31)
        assert v.blocking_issues == ["fax number not in sources"] and v.advisory_issues == ["overreach"]
        assert v.has_blocking
        kwargs = invoker.invoke.call_args.kwargs
        assert kwargs["stage"] == "grounding_audit" and kwargs["schema"]["required"] == [
            "verdict", "score", "blocking_issues", "advisory_issues", "reasoning"]
        assert "'score', 'blocking_issues'" in invoker.invoke.call_args.args[1]

    def test_legacy_output_without_graded_fields_still_parses(self):
        orch = _orch()
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": [{"verdict": "ACCEPT", "reasoning": "ok"}], "usage": {"input_tokens": 0, "output_tokens": 0}, "usage_source": "real"}
        with patch.object(orch, "_invoker_for", return_value=invoker), patch.object(orch, "_role_has_tools", return_value=True), \
             patch.object(orch, "_capture"):
            v = orch.invoke_evaluator(_cand(), "advocate")
        assert v.verdict == "ACCEPT" and v.score is None and v.blocking_issues == [] and not v.has_blocking


class TestGroundCandidate:
    def test_clean_audit_returns_candidate_unchanged(self):
        orch = _orch(); cand = _cand()
        with patch.object(orch, "invoke_evaluator", return_value=_audit(score=80, verdict="ACCEPT")) as audit, \
             patch.object(orch, "_revise_candidate") as revise:
            out = orch._ground_candidate(cand)
        assert out is cand
        audit.assert_called_once_with(cand, "grounding_auditor", stage="grounding_audit")
        revise.assert_not_called()

    def test_defects_trigger_one_revision(self):
        orch = _orch(); cand = _cand()
        revised = _cand(title="In the two captured notices, neither mentioned the 5-day deadline", confidence="medium")
        with patch.object(orch, "invoke_evaluator", return_value=_audit(blocking=["x"], advisory=["y"])), \
             patch.object(orch, "_revise_candidate", return_value=revised) as revise:
            out = orch._ground_candidate(cand)
        assert out is revised
        revise.assert_called_once()

    def test_skipped_when_spend_near_cap(self):
        orch = _orch(cap=6.0, spent=5.0)   # 83% >= 80%
        with patch.object(orch, "invoke_evaluator") as audit:
            out = orch._ground_candidate(_cand())
        audit.assert_not_called()
        assert out.revision is None

    def test_audit_failure_is_non_fatal(self):
        orch = _orch(); cand = _cand()
        with patch.object(orch, "invoke_evaluator", side_effect=RuntimeError("astra down")):
            assert orch._ground_candidate(cand) is cand

    def test_revision_failure_is_non_fatal(self):
        orch = _orch(); cand = _cand()
        with patch.object(orch, "invoke_evaluator", return_value=_audit(blocking=["x"])), \
             patch.object(orch, "_revise_candidate", side_effect=ValueError("bad json")):
            assert orch._ground_candidate(cand) is cand

    def test_spend_cap_propagates(self):
        orch = _orch(); cand = _cand()
        with patch.object(orch, "invoke_evaluator", side_effect=SpendCapExceeded("cap")):
            with pytest.raises(SpendCapExceeded):
                orch._ground_candidate(cand)


class TestReviseCandidate:
    def _invoker(self, output):
        inv = MagicMock(); inv.invoke.return_value = {"output": output, "usage": {"input_tokens": 100, "output_tokens": 50}}
        return inv

    def test_revision_repairs_fields_keeps_provenance_and_shrinks_sources_only(self):
        orch = _orch(); cand = _cand()
        inv = self._invoker({"title": "Two captured notices said no action needed", "content": "WHAT: scoped\nEVIDENCE: ...\nclaim_scope: two notices",
                             "confidence": "medium", "confidence_reasoning": "two notices from one day",
                             "source_memories": [SRC_A, "33333333-3333-3333-3333-333333333333"]})
        with patch.object(orch, "_invoker_for", return_value=inv), patch.object(orch, "_role_has_tools", return_value=False), \
             patch.object(orch, "_capture"):
            out = orch._revise_candidate(cand, _audit(blocking=["fax number"], advisory=["every"], score=30))
        assert out.title.startswith("Two captured notices")
        assert out.confidence == "medium" and "claim_scope" in out.content
        assert out.source_memories == [SRC_A]                                   # foreign id dropped, subset kept
        assert out.strategy_that_found_it == "arrivals_synthesis" and out.operation == "CREATE"  # provenance kept
        assert out.revision == {"original_title": cand.title, "audit_score": 30,
                                "blocking_issues": ["fax number"], "advisory_issues": ["every"]}
        kwargs = inv.invoke.call_args.kwargs
        assert kwargs["tools"] is False and kwargs["stage"] == "revision" and kwargs["schema"] is REVISION_SCHEMA
        assert inv.invoke.call_args.args[0] == REVISION_PROMPT
        req = json.loads(inv.invoke.call_args.args[1])
        assert req["blocking_issues"] == ["fax number"] and "no action needed" in req["cited_sources"]
        assert orch._spend.calls == 1
        assert orch._spend.total_usd == pytest.approx(0.00175)  # 100 input + 50 output tokens, Opus 5

    def test_schema_note_and_relationship_notes_are_revised_but_targets_never_added(self):
        orch = _orch()
        cand = _cand(schema_note="the feed will never supply the trigger",
                     relationships=[{"target_id": SRC_A, "relation_type": "extends", "note": "cannot supply it"}])
        inv = self._invoker({"title": "t", "content": "c", "confidence": "medium", "source_memories": [SRC_A, SRC_B],
                             "schema_note": "the two captured notices did not mention the trigger",
                             "relationships": [{"target_id": SRC_A, "relation_type": "extends", "note": "two notices, no mention"},
                                               {"target_id": "99999999-9999-9999-9999-999999999999", "relation_type": "supports", "note": "new"}]})
        with patch.object(orch, "_invoker_for", return_value=inv), patch.object(orch, "_role_has_tools", return_value=False), \
             patch.object(orch, "_capture"):
            out = orch._revise_candidate(cand, _audit(blocking=["unsupported system-capability claim"]))
        assert out.schema_note == "the two captured notices did not mention the trigger"
        assert out.relationships == [{"target_id": SRC_A, "relation_type": "extends", "note": "two notices, no mention"}]

    def test_nested_candidate_json_in_content_is_unwrapped(self):
        """Observed live: the revision returned the whole candidate as a JSON string in `content`."""
        orch = _orch()
        inner = {"title": "Inner title", "content": "WHAT: the real body", "confidence": "medium",
                 "source_memories": [SRC_A], "schema_note": "inner note"}
        inv = self._invoker({"title": "Outer", "content": json.dumps(inner), "confidence": "high", "source_memories": [SRC_A, SRC_B]})
        with patch.object(orch, "_invoker_for", return_value=inv), patch.object(orch, "_role_has_tools", return_value=False), \
             patch.object(orch, "_capture"):
            out = orch._revise_candidate(_cand(), _audit(blocking=["x"]))
        assert out.content == "WHAT: the real body" and out.title == "Inner title"
        assert out.confidence == "medium" and out.source_memories == [SRC_A] and out.schema_note == "inner note"

    def test_tool_capable_thinker_gets_no_schema_kwarg(self):
        orch = _orch()
        inv = self._invoker({"title": "t", "content": "c", "confidence": "low", "source_memories": [SRC_A]})
        with patch.object(orch, "_invoker_for", return_value=inv), patch.object(orch, "_role_has_tools", return_value=True), \
             patch.object(orch, "_capture"):
            orch._revise_candidate(_cand(), _audit(blocking=["x"]))
        assert "schema" not in inv.invoke.call_args.kwargs

    def test_missing_content_raises(self):
        orch = _orch()
        inv = self._invoker({"title": "t"})
        with patch.object(orch, "_invoker_for", return_value=inv), patch.object(orch, "_role_has_tools", return_value=False), \
             patch.object(orch, "_capture"):
            with pytest.raises(ValueError):
                orch._revise_candidate(_cand(), _audit(blocking=["x"]))

    def test_all_sources_dropped_falls_back_to_originals(self):
        orch = _orch()
        inv = self._invoker({"title": "t", "content": "c", "confidence": "low", "source_memories": []})
        with patch.object(orch, "_invoker_for", return_value=inv), patch.object(orch, "_role_has_tools", return_value=False), \
             patch.object(orch, "_capture"):
            out = orch._revise_candidate(_cand(), _audit(blocking=["x"]))
        assert out.source_memories == [SRC_A, SRC_B]


class TestPipelineIntegration:
    """The panel votes on the revised candidate; an empty revision is recorded as rejected."""

    def _run(self, orch, candidate, ground_result, verdicts):
        slice_ = MemorySlice(name="S", strategy="arrivals_synthesis", memory_ids=[SRC_A, SRC_B])
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[slice_]), \
             patch.object(orch, "_allocate_attention", side_effect=lambda r, t, s, sl: sl), \
             patch.object(orch, "invoke_thinker", return_value=[candidate]), \
             patch.object(orch, "_ground_candidate", return_value=ground_result), \
             patch.object(orch, "invoke_evaluator", side_effect=verdicts) as ev, \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
             patch("src.dream_cycle.orchestrator.check_duplicate", return_value=None), \
             patch("src.dream_cycle.orchestrator.store_accepted", side_effect=_stored):
            db.create_run.return_value = "run-p"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("scheduled")
        return result, db, ev

    def test_panel_judges_the_revised_candidate_and_details_are_persisted(self):
        orch = _orch(); original = _cand(); revised = _cand(title="Revised", revision={"original_title": original.title})
        verdicts = [EvaluatorVerdict(role=r, verdict="ACCEPT", reasoning="ok", score=80) for r in
                    ("skeptic", "advocate", "epistemologist", "methodologist")]
        result, db, ev = self._run(orch, original, revised, verdicts)
        assert all(call.args[0] is revised for call in ev.call_args_list)
        stored_candidate, stored_verdicts, final = db.store_candidate.call_args.args[1:4]
        assert stored_candidate["title"] == "Revised" and stored_candidate["revision"]["original_title"] == original.title
        assert final == "ACCEPTED" and stored_verdicts["evaluator_details"]["skeptic"]["score"] == 80
        assert result.candidates_accepted == 1

    def test_empty_revision_is_rejected_without_panel(self):
        orch = _orch(); original = _cand(); emptied = _cand(content="   ")
        result, db, ev = self._run(orch, original, emptied, [])
        ev.assert_not_called()
        assert db.store_candidate.call_args.args[3] == "WITHDRAWN"
        assert result.candidates_rejected == 0 and result.candidates_accepted == 0
        assert db.store_candidate.call_args.args[2] == {}


class TestPerSliceCap:
    def test_scheduled_runs_keep_at_most_the_cap(self):
        orch = _orch()
        cands = [_cand(title=f"c{i}") for i in range(5)]
        slice_ = MemorySlice(name="S", strategy="arrivals_synthesis", memory_ids=[SRC_A, SRC_B])
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_skim_arrivals", return_value=None), \
             patch.object(orch, "invoke_explorer", return_value=[slice_]), \
             patch.object(orch, "_allocate_attention", side_effect=lambda r, t, s, sl: sl), \
             patch.object(orch, "invoke_thinker", return_value=cands), \
             patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
             patch.object(orch, "invoke_evaluator", return_value=EvaluatorVerdict(role="x", verdict="REJECT")), \
             patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
             patch("src.dream_cycle.orchestrator.generate_digest", return_value=""):
            db.create_run.return_value = "run-c"
            db.get_memory_stats.return_value = {"total_count": 1, "recent_activity": 1, "date_range": {}}
            result = orch.run("scheduled")
        assert result.candidates_generated == o.MAX_CANDIDATES_PER_SLICE == 2


class TestRevisionRequest:
    def test_request_is_json_with_candidate_sources_and_issues(self):
        req = json.loads(build_revision_request('{"title": "t"}', "SRC TEXT", ["b1"], ["a1"]))
        assert req == {"candidate": {"title": "t"}, "cited_sources": "SRC TEXT", "blocking_issues": ["b1"], "advisory_issues": ["a1"]}


def _stored(candidate, *, run_id, verdicts, authorized_memories):
    from dataclasses import asdict
    from src.dream_cycle import orchestrator
    orchestrator.dream_cycle_db.store_candidate(run_id, asdict(candidate), verdicts, "ACCEPTED", "mem-1")
    return "mem-1"
