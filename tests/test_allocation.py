"""Attention allocation: feedback bounds, arrivals manifest, slice budget,
guaranteed arrivals slice, prompt contracts, and the orchestrator hook (hermetic)."""
import uuid
from collections import Counter
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
def _inventory_text(*args, **kwargs):
    return allocation.render_arrivals_inventory(*args, **kwargs).text


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

    def test_manifest_without_memory_counts(self):
        text = allocation.build_arrivals_manifest(WINDOW, [{"source_type": "slack", "documents": 3, "samples": []}])
        assert "3 documents across 1 channels" in text and "- slack: 3 conversation-days" in text
        assert "memories" not in text.split("\n")[1].split(".")[0]


# --------------------------------------------------------------------------- #
# Inventory (skim records)                                                     #
# --------------------------------------------------------------------------- #
def _rec(channel="slack", salience=2, novelty=1, created=NOW, summary="A synthetic summary.", decisions=0,
         questions=0, people=0, hint=None, unverified=0, cluster=None, rid=None):
    items = [{"text": f"item {i}", "quote": None if i < unverified else "a verbatim passage"} for i in range(decisions)]
    qs = [{"text": f"q {i}", "quote": "a verbatim passage"} for i in range(questions)]
    return {"id": rid or _uid(), "title": "t", "source_type": channel, "created_at": created,
            "skim": {"summary": summary, "people": [f"P{i}" for i in range(people)], "entities": [], "topics": [],
                     "decisions": items, "open_questions": qs, "novelty": novelty, "salience": salience,
                     "contradiction_hint": hint, "doc_kind": "conversation", "quotes_unverified": unverified},
            "cluster_id": cluster, "truncated": False}


class TestInventory:
    COUNTS = {"slack": 40, "article": 120, "outlook_email": 12}

    def test_header_legend_and_line_format(self):
        r = _rec("slack", 3, 2, summary="Priya moved the beta launch to the 14th.", decisions=1, questions=1,
                 people=2, hint="reverses the earlier date", rid="id-1")
        text = _inventory_text(WINDOW, [r], counts_by_channel=self.COUNTS)
        assert text.startswith("## What arrived since the previous run (2026-09-17 09:00 UTC)")
        assert "172 documents across 3 channels; 1 of them were read by the skim stage" in text
        assert "- article: 120 articles / web pages" in text and "- slack: 40 conversation-days" in text
        assert allocation.INVENTORY_LEGEND in text
        assert "id-1 | slack | S3 N2 | D1 Q1 C P2 | Priya moved the beta launch to the 14th." in text

    def test_flags_unverified_marker_cluster_tag_and_empty_flags(self):
        bang = _rec(decisions=2, unverified=1, rid="id-b")
        plain = _rec(salience=0, novelty=0, summary="", rid="id-p", cluster="c2")
        text = _inventory_text(WINDOW, [bang, plain], counts_by_channel=self.COUNTS)
        assert "id-b | slack | S2 N1 | D2 ! | A synthetic summary." in text
        assert "id-p | slack | S0 N0 | - | - [c2]" in text

    def test_unverified_marker_also_from_null_quotes_without_the_count(self):
        r = _rec(decisions=1, rid="id-n")
        r["skim"]["decisions"][0]["quote"] = None
        del r["skim"]["quotes_unverified"]
        assert "id-n | slack | S2 N1 | D1 ! |" in _inventory_text(
            WINDOW, [r], counts_by_channel=self.COUNTS)

    def test_order_salience_then_novelty_then_round_robin_across_channels(self):
        recs = [_rec("article", 1, 1, NOW - timedelta(hours=h), rid=f"art-{h}") for h in range(4)]
        recs += [_rec("slack", 1, 1, NOW - timedelta(hours=9), rid="slack-old"),
                 _rec("slack", 1, 1, NOW - timedelta(hours=1), rid="slack-new"),
                 _rec("outlook_email", 1, 1, NOW - timedelta(hours=2), rid="mail-1"),
                 _rec("outlook_email", 3, 0, NOW - timedelta(days=1), rid="mail-top"),
                 _rec("article", 2, 3, NOW - timedelta(days=2), rid="art-novel")]
        order = [r["id"] for r in allocation.rank_skim_records(recs)]
        assert order[:2] == ["mail-top", "art-novel"]                          # S3 first, then S2
        tier = order[2:]
        assert tier[:3] == ["art-0", "slack-new", "mail-1"]                     # round-robin: biggest channel first
        assert tier[3:5] == ["art-1", "slack-old"] and tier[5:] == ["art-2", "art-3"]
        assert [r["id"] for r in allocation.rank_skim_records(recs)] == order  # deterministic

    def test_char_cap_lists_a_prefix_and_counts_the_rest(self):
        recs = [_rec("article", 1, 1, NOW - timedelta(minutes=i), summary="x" * 120, rid=f"a{i:03d}") for i in range(200)]
        render = allocation.render_arrivals_inventory(WINDOW, recs, counts_by_channel={"article": 200}, max_chars=6000)
        assert len(render.text) <= 6000
        assert 0 < render.listed < 200 and render.total == 200 and render.fallback is False
        assert render.text.endswith(f"… {200 - render.listed} more documents not listed (all ids are retrievable by tools)")
        full = allocation.render_arrivals_inventory(WINDOW, recs, counts_by_channel={"article": 200}, max_chars=200_000)
        assert full.listed == 200 and "more documents not listed" not in full.text

    def test_summary_is_trimmed_for_the_line(self):
        r = _rec(summary="word " * 60, rid="id-l")
        line = [l for l in _inventory_text(WINDOW, [r], counts_by_channel=self.COUNTS).split("\n")
                if l.startswith("id-l")][0]
        summary = line.split(" | ")[-1]
        assert summary.endswith("…") and len(summary) <= allocation.INVENTORY_SUMMARY_CHARS + 1

    def test_empty_records_fall_back_to_the_manifest(self):
        render = allocation.render_arrivals_inventory(WINDOW, [], counts_by_channel=self.COUNTS)
        assert render.fallback is True and render.listed == 0
        assert render.text == allocation.build_arrivals_manifest(
            WINDOW, [{"source_type": "article", "documents": 120, "samples": []},
                     {"source_type": "slack", "documents": 40, "samples": []},
                     {"source_type": "outlook_email", "documents": 12, "samples": []}])
        assert "Nothing new was captured" in _inventory_text(WINDOW, [], counts_by_channel={})

    def test_counts_fall_back_to_the_records_when_the_summary_is_missing(self):
        text = _inventory_text(WINDOW, [_rec("slack"), _rec("article")], counts_by_channel={})
        assert "2 documents across 2 channels" in text


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


class TestSkimWeightedArrivalsSlice:
    """With skim records half the slice goes to the most salient / novel documents;
    without them the selection is exactly the old one."""

    def _rows(self):
        rows = []
        for ch in ("slack", "outlook_email", "browser_history", "article"):
            rows += [{"id": f"{ch}-{i}", "title": f"{ch} doc {i}", "type": "source", "source_type": ch,
                      "created_at": NOW - timedelta(hours=i)} for i in range(10)]
        rows += [{"id": f"insight-{i}", "title": f"insight {i}", "type": "insight", "source_type": None,
                  "created_at": NOW} for i in range(4)]
        return rows

    def test_without_records_is_the_old_selection(self):
        rows = self._rows()
        sel = allocation.select_arrivals(rows, 14)
        assert sel.skim_weighted is False and sel.skim_picked == 0 and sel.metrics == {}
        old = allocation.compose_arrivals_slice(WINDOW, rows, 14)
        assert old.memory_ids == [r["id"] for r in sel.rows]
        assert allocation.compose_arrivals_slice(WINDOW, rows, 14, skim_records=None) == old
        assert allocation.compose_arrivals_slice(WINDOW, rows, 14, skim_records=[]) == old

    def test_half_from_salience_half_stratified_under_the_caps(self):
        rows = self._rows()
        # The oldest article / slack docs are the salient ones — stratification (newest first) would miss them.
        recs = [_rec("article", 3, 3, NOW - timedelta(hours=9), rid="article-9"),
                _rec("article", 3, 2, NOW - timedelta(hours=8), rid="article-8"),
                _rec("slack", 3, 1, NOW - timedelta(hours=9), rid="slack-9"),
                _rec("slack", 2, 3, NOW - timedelta(hours=8), rid="slack-8"),
                _rec("outlook_email", 2, 2, NOW - timedelta(hours=9), rid="outlook_email-9"),
                _rec("browser_history", 1, 1, NOW - timedelta(hours=9), rid="browser_history-9"),
                _rec("article", 0, 0, NOW - timedelta(hours=7), rid="article-7")] + \
               [_rec("article", 3, 3, NOW - timedelta(hours=i), rid=f"article-{i}") for i in range(3, 6)]  # S3 backlog > cap
        sel = allocation.select_arrivals(rows, 14, skim_records=recs)
        ids = [r["id"] for r in sel.rows]
        assert len(ids) == 14 == len(set(ids))
        assert sel.skim_weighted is True and sel.skim_picked == 7
        assert sel.metrics == {"skim_weighted": True, "skim_picked": 7}
        # Salience half first, in inventory order: four S3 articles (cap), then slack S3/S2, then the email.
        assert ids[:7] == ["article-3", "article-4", "article-5", "article-9", "slack-9", "slack-8", "outlook_email-9"]
        # Stratified half over the rest, newest first, spread across the channels with room left.
        assert {"slack-0", "outlook_email-0", "browser_history-0"} <= set(ids[7:])
        assert not any(i.startswith("article") for i in ids[7:])            # article had no room left
        by_channel = Counter(i.rsplit("-", 1)[0] for i in ids)
        assert max(by_channel.values()) <= allocation.ARRIVALS_PER_SOURCE_CAP   # cap holds across both halves
        assert "article-7" not in ids                                        # S0 never beats the S3 backlog

    def test_semantic_cap_holds_for_the_skim_half(self):
        rows = self._rows()
        recs = [_rec(None, 3, 3, rid=f"insight-{i}") for i in range(4)]
        sel = allocation.select_arrivals(rows, 14, skim_records=recs)
        assert sum(1 for r in sel.rows if r["id"].startswith("insight")) == allocation.MAX_SEMANTIC_IN_ARRIVALS
        assert sel.skim_picked == allocation.MAX_SEMANTIC_IN_ARRIVALS and len(sel.rows) == 14

    def test_partial_skim_leaves_more_to_the_stratified_half_so_the_slice_does_not_shrink(self):
        rows = self._rows()
        sel = allocation.select_arrivals(rows, 14, skim_records=[_rec("article", 3, 3, rid="article-9")])
        assert len(sel.rows) == 14 and sel.skim_picked == 1 and sel.rows[0]["id"] == "article-9"

    def test_records_for_unknown_ids_are_ignored(self):
        rows = self._rows()
        sel = allocation.select_arrivals(rows, 14, skim_records=[_rec("article", 3, 3, rid="not-in-window")])
        assert "not-in-window" not in [r["id"] for r in sel.rows] and sel.skim_picked == 0

    def test_build_arrivals_selection_reads_through_dcdb(self):
        db = MagicMock()
        db.get_recent_parent_memories.return_value = self._rows()
        slice_, sel = allocation.build_arrivals_selection(WINDOW, dcdb=db, skim_records=[_rec("slack", 3, 3, rid="slack-9")])
        assert slice_.strategy == allocation.ARRIVALS_STRATEGY and "slack-9" in slice_.memory_ids
        assert sel.skim_weighted is True and sel.skim_picked >= 1
        assert allocation.build_arrivals_selection(WINDOW, dcdb=db)[0].memory_ids == \
               allocation.compose_arrivals_slice(WINDOW, self._rows()).memory_ids


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

    def test_explorer_prompt_explains_the_inventory(self):
        """Phase 3 (docs/SKIM-STAGE.md): the Explorer knows the inventory semantics and
        is told to build arrival-anchored slices from it across channels and salience levels."""
        p = get_explorer_prompt(memory_count=1, date_range="x", feedback_injection="", run_type="scheduled")
        assert "## The arrivals inventory (in the user message)" in p
        assert "`id | channel | S<n> N<n> | flags | summary`" in p
        assert "S = salience to the user's own work and life, 0-3" in p and "N = novelty, 0-3" in p
        assert "`!` a quoted passage could not be verified" in p and "`[c<k>]`" in p
        assert "Every id in the inventory is retrievable with memory_read" in p
        assert "high salience first but not exclusively" in p
        assert "Do not exclude any channel; this is the user's whole digital day" in p
        assert "Without skim records the block is the older MANIFEST" in p
        # Placed with the source description, before the strategies; strategy 12 and TIME refer to it.
        assert p.index("## Where memories come from") < p.index("## The arrivals inventory") < p.index("1. TEMPORAL JUXTAPOSITION")
        assert "Start from the arrivals inventory (or" in p and "the inventory (or manifest) already lists ids" in p
        # The slice-budget rules themselves are unchanged.
        assert "at least 60% of their memories created since the previous run" in p
        assert "Three or more slices drawn almost entirely from one channel will be cut to two" in p
        # session_start mode gets the same explanation (the block is in every user message).
        s = get_explorer_prompt(memory_count=1, date_range="x", feedback_injection="", run_type="session_start")
        assert "## The arrivals inventory (in the user message)" in s

    def test_thinker_prompt_gates_meta_and_frames_arrivals(self):
        t = get_thinker_prompt()
        assert "10. ARRIVALS" in t and "arrivals_synthesis" in t and "Cite the episodes" in t
        assert "Only when the slice itself is about this system's history" in t


# --------------------------------------------------------------------------- #
# Orchestrator hook                                                            #
# --------------------------------------------------------------------------- #
class TestAllocateAttention:
    def _orch(self):
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

        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.ledger.get_memory_provenance") as provenance:
            db.get_previous_run_started_at.return_value = WINDOW
            db.get_recent_slice_memory_ids.return_value = set(old_ids[:6])
            provenance.side_effect = lambda ids: [r for r in prov_rows if r["id"] in set(ids)]
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
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.ledger.get_memory_provenance") as provenance:
            db.get_previous_run_started_at.return_value = None
            db.get_recent_slice_memory_ids.return_value = set()
            provenance.return_value = [_prov(i, "distilled_chat", "insight", NOW - timedelta(days=40)) for i in ids]
            out = self._orch()._allocate_attention("run-2", "user_triggered", NOW,
                                                   [MemorySlice(name="topic", strategy="x", memory_ids=ids)])
        assert [s.name for s in out] == ["topic"]
        db.get_recent_parent_memories.assert_not_called()
        db.record_slices.assert_called_once()

    def test_unavailable_provenance_leaves_slices_untouched(self):
        """Fully mocked DB (as in the older orchestrator tests): no judgement, no ledger."""
        slices = [MemorySlice(name="s", strategy="x", memory_ids=["mem-1", "mem-2"])]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.ledger.get_memory_provenance"):
            out = self._orch()._allocate_attention("run-3", "scheduled", NOW, slices)
        assert out == slices
        db.record_slices.assert_not_called()

    def test_failure_is_non_fatal(self):
        slices = [MemorySlice(name="s", strategy="x", memory_ids=[_uid()])]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch("src.dream_cycle.ledger.get_memory_provenance") as provenance:
            db.get_previous_run_started_at.side_effect = OSError("db down")
            out = self._orch()._allocate_attention("run-4", "scheduled", NOW, slices)
        assert out == slices

    def test_explorer_user_message_carries_manifest(self):
        orch = self._orch()
        invoker = MagicMock()
        invoker.invoke.return_value = {"output": [], "raw": "[]", "usage": {"inputTokens": 0, "outputTokens": 0}, "usage_source": "real"}
        arrivals = [{"source_type": "slack", "documents": 3, "memories": 9,
                     "samples": [{"id": "id-9", "title": "Slack: #eng — 2026-09-17", "created_at": "2026-09-17T08:00:00+00:00"}]}]
        with patch("src.dream_cycle.orchestrator.dream_cycle_db") as db, \
             patch.object(orch, "_invoker_for", return_value=invoker), \
             patch.object(orch, "_capture"):
            db.get_previous_run_started_at.return_value = WINDOW
            db.get_arrivals_summary.return_value = arrivals
            db.get_strategy_usage.return_value = {}
            orch._current_run_id = "run-5"
            orch._current_started_at = NOW
            orch.invoke_explorer("", "scheduled", None,
                                 stats={"total_count": 10, "recent_activity": 9, "date_range": {"min": NOW, "max": NOW}})
        msg = invoker.invoke.call_args.args[1]
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
