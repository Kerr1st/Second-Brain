"""Tests for src/skim/runner.py — skim_window with a fake invoker and an in-memory store.

Synthetic documents only. Covers idempotency, the sub-cap stop, per-document
failure isolation, truncation, coercion counting, run-cap propagation and the
counts-only logging.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import pytest

from src.backends.cost import SpendCapExceeded, SpendTracker
from src.prompts.skim import PROMPT_VERSION, SKIM_SYSTEM_PROMPT
from src.skim import runner
from src.skim.runner import SkimResult, skim_window
from src.skim.schema import SKIM_SCHEMA

MODEL = "us.amazon.nova-2-lite-v1:0"
WINDOW = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
DOC_A = ("Orrery sync — 18 Sep\nPriya: after the load test we agreed to move the beta launch to the 14th.\n"
         "Tomasz: the pricing tier question is still open: which tier goes first?\n")
DOC_B = "Weekly digest: three articles on container scheduling. Nothing actionable.\n"


def _doc(content=DOC_A, channel="slack", title="Synthetic title", doc_id=None):
    return {"id": doc_id or str(uuid.uuid4()), "title": title, "source_type": channel,
            "created_at": WINDOW, "content": content}


def _record(summary="Meeting notes agreeing to move the beta launch to the 14th.", decisions=(), questions=(),
            **over):
    rec = {"summary": summary, "people": ["Priya", "Tomasz"], "entities": ["Orrery"], "topics": ["beta launch"],
           "decisions": [{"text": "Launch moves.", "quote": q} for q in decisions],
           "open_questions": [{"text": "Tier order open.", "quote": q} for q in questions],
           "novelty": 2, "salience": 3, "contradiction_hint": None, "doc_kind": "conversation"}
    rec.update(over)
    return rec


class FakeInvoker:
    """Returns queued outputs (or raises queued exceptions); records every call."""

    def __init__(self, outputs, usage=(3000, 200), tier="flex"):
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self._usage, self._tier = usage, tier

    def invoke(self, system_prompt, user_message, **kw):
        self.calls.append({"system": system_prompt, "user": user_message, **kw})
        out = self.outputs.pop(0) if self.outputs else _record()
        if isinstance(out, Exception):
            raise out
        usage = {"inputTokens": self._usage[0], "outputTokens": self._usage[1]}
        if self._tier:
            usage["serviceTier"] = self._tier
        return {"output": out, "raw": json.dumps(out), "usage": usage, "usage_source": "real"}


class FakeStore:
    """In-memory memory_skim keyed by (memory_id, model, prompt_version)."""

    def __init__(self, arrivals=(), documents=(), fail_upsert_for=()):
        self.rows: dict[tuple, dict] = {}
        self.arrivals = list(arrivals)
        self.documents = {d["id"]: d for d in documents}
        self.fail_upsert_for = set(fail_upsert_for)
        self.calls: list[str] = []

    def list_arrivals(self, window_start, limit=5000):
        self.calls.append("list_arrivals")
        return [{k: v for k, v in a.items() if k != "content"} for a in self.arrivals]

    def missing_ids(self, ids, model, prompt_version):
        self.calls.append("missing_ids")
        return [i for i in dict.fromkeys(str(x) for x in ids) if (i, model, prompt_version) not in self.rows]

    def fetch_documents(self, ids):
        self.calls.append("fetch_documents")
        return [self.documents[i] for i in ids if i in self.documents]

    def upsert_skim(self, memory_id, **kw):
        self.calls.append("upsert_skim")
        if memory_id in self.fail_upsert_for:
            raise RuntimeError("db down")
        self.rows[(memory_id, kw["model"], kw["prompt_version"])] = {"memory_id": memory_id, **kw}
        return "row-id"


def _run(docs, outputs, *, store=None, tracker=None, cap=1.0, **kw):
    store = store if store is not None else FakeStore()
    inv = FakeInvoker(outputs)
    res = skim_window(WINDOW, invoker=inv, model=MODEL, prompt_version=PROMPT_VERSION,
                      tracker=tracker or SpendTracker(cap_usd=9.0), cap_usd=cap, run_id="run-1",
                      fetch_docs=lambda ws: docs, store=store, **kw)
    return res, inv, store


class TestHappyPath:
    def test_skims_every_document_and_stores_records(self):
        docs = [_doc(), _doc(DOC_B, "article")]
        res, inv, store = _run(docs, [
            _record(decisions=["we agreed to move the beta launch to the 14th"], questions=["which tier goes first?"]),
            _record(summary="A digest of three articles.", salience=0, doc_kind="article"),
        ])
        assert isinstance(res, SkimResult)
        assert (res.docs_total, res.skimmed, res.reused, res.failed, res.invalid) == (2, 2, 0, 0, 0)
        assert res.quotes_emitted == 2 and res.quotes_verbatim == 2
        assert res.cost_usd == pytest.approx(2 * (3000 * 0.165e-6 + 200 * 1.375e-6), abs=1e-7)
        assert res.aborted_on_cap is False and res.duration_s >= 0
        assert res.model == MODEL and res.prompt_version == PROMPT_VERSION and res.window_start == WINDOW
        # The call: system prompt, document in the user message, forced schema, tool-less, low effort.
        call = inv.calls[0]
        assert call["system"] == SKIM_SYSTEM_PROMPT and DOC_A in call["user"] and "channel: slack" in call["user"]
        assert call["schema"] is SKIM_SCHEMA and call["tools"] is False and call["effort"] == "low"
        assert call["stage"] == "skim" and call["run_id"] == "run-1"
        # Stored rows: record in schema shape, attribution, tokens, cost.
        row = store.rows[(docs[0]["id"], MODEL, PROMPT_VERSION)]
        assert row["run_id"] == "run-1" and row["input_tokens"] == 3000 and row["output_tokens"] == 200
        assert row["skim"]["decisions"][0]["quote"] == "we agreed to move the beta launch to the 14th"
        assert row["skim"]["quotes_unverified"] == 0 and res.quotes_unverified == 0
        assert "coercions" not in row["skim"] and row["truncated"] is False
        assert row["cost_usd"] == pytest.approx(3000 * 0.165e-6 + 200 * 1.375e-6, abs=1e-7)

    def test_spend_is_metered_through_the_run_tracker(self):
        tracker = SpendTracker(cap_usd=9.0)
        res, _, _ = _run([_doc(), _doc()], [_record(), _record()], tracker=tracker)
        assert tracker.calls == 2 and tracker.total_usd == pytest.approx(res.cost_usd)

    def test_callable_tracker_is_accepted(self):
        seen = []
        def record_spend(model, usage):
            seen.append((model, usage["inputTokens"]))
            return 0.01
        res, _, _ = _run([_doc()], [_record()], tracker=record_spend)
        assert seen == [(MODEL, 3000)] and res.cost_usd == pytest.approx(0.01)

    def test_bad_tracker_type_is_rejected(self):
        with pytest.raises(TypeError):
            skim_window(WINDOW, invoker=FakeInvoker([]), model=MODEL, prompt_version=PROMPT_VERSION,
                        tracker=object(), cap_usd=1.0, run_id="r", fetch_docs=lambda ws: [], store=FakeStore())

    def test_to_stats_and_to_dict(self):
        res, _, _ = _run([_doc()], [_record()])
        stats = res.to_stats()
        assert {"skim_docs", "skim_skimmed", "skim_reused", "skim_failed", "skim_cost_usd"} <= set(stats)
        assert stats["skim_docs"] == 1 and stats["skim_skimmed"] == 1 and stats["skim_model"] == MODEL
        assert res.to_dict()["window_start"] == WINDOW.isoformat()

    def test_empty_window(self):
        res, inv, store = _run([], [])
        assert res.docs_total == 0 and res.skimmed == 0 and inv.calls == [] and store.rows == {}


class TestIdempotency:
    def test_already_skimmed_documents_are_skipped(self):
        docs = [_doc(), _doc(DOC_B, "article")]
        store = FakeStore()
        store.rows[(docs[0]["id"], MODEL, PROMPT_VERSION)] = {"memory_id": docs[0]["id"]}
        res, inv, _ = _run(docs, [_record()], store=store)
        assert (res.docs_total, res.reused, res.skimmed) == (2, 1, 1)
        assert len(inv.calls) == 1 and DOC_B in inv.calls[0]["user"]

    def test_second_pass_costs_nothing(self):
        docs = [_doc(), _doc(DOC_B, "article")]
        store = FakeStore()
        first, _, _ = _run(docs, [_record(), _record()], store=store)
        second, inv, _ = _run(docs, [], store=store)
        assert first.skimmed == 2 and second.skimmed == 0 and second.reused == 2
        assert inv.calls == [] and second.cost_usd == 0.0

    def test_a_different_prompt_version_or_model_skims_again(self):
        docs = [_doc()]
        store = FakeStore()
        _run(docs, [_record()], store=store)
        res = skim_window(WINDOW, invoker=FakeInvoker([_record()]), model=MODEL, prompt_version=PROMPT_VERSION + "-next",
                          tracker=SpendTracker(9.0), cap_usd=1.0, run_id="r2", fetch_docs=lambda ws: docs, store=store)
        assert res.skimmed == 1 and len(store.rows) == 2

    def test_default_store_path_loads_content_only_for_missing_docs(self):
        a, b = _doc(doc_id="a" * 8), _doc(DOC_B, "article", doc_id="b" * 8)
        store = FakeStore(arrivals=[a, b], documents=[a, b])
        store.rows[(a["id"], MODEL, PROMPT_VERSION)] = {}
        inv = FakeInvoker([_record()])
        res = skim_window(WINDOW, invoker=inv, model=MODEL, prompt_version=PROMPT_VERSION,
                          tracker=SpendTracker(9.0), cap_usd=1.0, run_id="r", store=store)
        assert store.calls[:3] == ["list_arrivals", "missing_ids", "fetch_documents"]
        assert (res.docs_total, res.reused, res.skimmed) == (2, 1, 1)
        assert DOC_B in inv.calls[0]["user"]

    def test_nothing_missing_fetches_no_content(self):
        a = _doc(doc_id="a" * 8)
        store = FakeStore(arrivals=[a], documents=[a])
        store.rows[(a["id"], MODEL, PROMPT_VERSION)] = {}
        res = skim_window(WINDOW, invoker=FakeInvoker([]), model=MODEL, prompt_version=PROMPT_VERSION,
                          tracker=SpendTracker(9.0), cap_usd=1.0, run_id="r", store=store)
        assert "fetch_documents" not in store.calls and res.reused == 1


class TestFailureIsolation:
    def test_model_error_counts_as_failed_and_the_rest_continue(self):
        docs = [_doc(), _doc(DOC_B, "article"), _doc()]
        res, inv, store = _run(docs, [RuntimeError("throttled"), _record(), TimeoutError("slow")])
        assert (res.skimmed, res.failed, res.invalid) == (1, 2, 0)
        assert len(inv.calls) == 3 and len(store.rows) == 1
        assert res.cost_usd == pytest.approx(3000 * 0.165e-6 + 200 * 1.375e-6, abs=1e-7)  # only the reply

    def test_invalid_reply_counts_as_invalid_and_is_still_metered(self):
        res, _, store = _run([_doc(), _doc()], [_record(salience=4), _record()])
        assert (res.skimmed, res.invalid, res.failed) == (1, 1, 0)
        assert len(store.rows) == 1
        assert res.cost_usd == pytest.approx(2 * (3000 * 0.165e-6 + 200 * 1.375e-6), abs=1e-7)

    def test_store_failure_counts_as_failed(self):
        docs = [_doc(), _doc()]
        store = FakeStore(fail_upsert_for={docs[0]["id"]})
        res, _, _ = _run(docs, [_record(), _record()], store=store)
        assert (res.skimmed, res.failed) == (1, 1) and len(store.rows) == 1

    def test_selection_failure_propagates_with_no_spend(self):
        class BrokenStore(FakeStore):
            def missing_ids(self, ids, model, prompt_version):
                raise RuntimeError("db unreachable")
        inv = FakeInvoker([_record()])
        with pytest.raises(RuntimeError):
            skim_window(WINDOW, invoker=inv, model=MODEL, prompt_version=PROMPT_VERSION, tracker=SpendTracker(9.0),
                        cap_usd=1.0, run_id="r", fetch_docs=lambda ws: [_doc()], store=BrokenStore())
        assert inv.calls == []

    def test_list_output_takes_first_element(self):
        res, _, _ = _run([_doc()], [[_record()]])
        assert res.skimmed == 1


class TestCaps:
    def test_sub_cap_stops_cleanly(self):
        docs = [_doc() for _ in range(4)]
        # Each call costs 3000*0.165e-6 + 200*1.375e-6 ≈ $0.00077; cap after two calls.
        per_call = 3000 * 0.165e-6 + 200 * 1.375e-6
        res, inv, store = _run(docs, [_record()] * 4, cap=2 * per_call - 1e-9)
        assert res.aborted_on_cap is True
        assert len(inv.calls) == 2 and res.skimmed == 2 and len(store.rows) == 2
        assert res.docs_total == 4 and res.reused == 0

    def test_run_cap_propagates_after_storing_the_paid_record(self):
        docs = [_doc(), _doc()]
        store = FakeStore()
        inv = FakeInvoker([_record(), _record()], usage=(2_000_000, 0))   # ≈ $0.33 per call at Flex
        with pytest.raises(SpendCapExceeded):
            skim_window(WINDOW, invoker=inv, model=MODEL, prompt_version=PROMPT_VERSION,
                        tracker=SpendTracker(cap_usd=0.30), cap_usd=5.0, run_id="r",
                        fetch_docs=lambda ws: docs, store=store)
        assert len(inv.calls) == 1 and len(store.rows) == 1        # the call that tripped it is kept
        assert store.rows[(docs[0]["id"], MODEL, PROMPT_VERSION)]["cost_usd"] == pytest.approx(0.33, abs=1e-6)

    def test_run_cap_propagates_even_when_the_reply_is_invalid(self):
        inv = FakeInvoker([_record(salience=9)], usage=(2_000_000, 0))
        with pytest.raises(SpendCapExceeded):
            skim_window(WINDOW, invoker=inv, model=MODEL, prompt_version=PROMPT_VERSION,
                        tracker=SpendTracker(cap_usd=0.30), cap_usd=5.0, run_id="r",
                        fetch_docs=lambda ws: [_doc()], store=FakeStore())

    def test_cap_exceeded_raised_by_the_invoker_propagates(self):
        with pytest.raises(SpendCapExceeded):
            _run([_doc(), _doc()], [SpendCapExceeded("cap"), _record()])


class TestTruncationAndCoercion:
    def test_long_documents_are_cut_and_flagged(self):
        long_doc, short_doc = _doc(content="x" * 500 + DOC_A), _doc(DOC_B)
        res, inv, store = _run([long_doc, short_doc], [_record(), _record()], max_doc_chars=400)
        assert res.truncated == 1
        assert len(inv.calls[0]["user"]) < 400 + 200          # header + cut body
        assert DOC_A not in inv.calls[0]["user"]
        assert store.rows[(long_doc["id"], MODEL, PROMPT_VERSION)]["truncated"] is True
        assert store.rows[(short_doc["id"], MODEL, PROMPT_VERSION)]["truncated"] is False

    def test_quote_check_runs_against_the_text_the_model_saw(self):
        """A quote from the cut-off tail is fabricated relative to what was sent."""
        body = DOC_B * 20 + "Decision: we agreed to move the beta launch to the 14th."
        doc = _doc(content=body)
        res, _, store = _run([doc], [_record(decisions=["we agreed to move the beta launch to the 14th"])],
                             max_doc_chars=len(DOC_B) * 5)
        assert res.truncated == 1 and res.quotes_emitted == 1 and res.quotes_verbatim == 0
        assert res.quotes_unverified == 1
        assert store.rows[(doc["id"], MODEL, PROMPT_VERSION)]["skim"]["decisions"][0]["quote"] is None


class TestUnverifiedQuotePolicy:
    """Policy (2026-09-20): a decision / open question whose quote fails both
    faithfulness paths keeps its text, its quote is stored as null, and the
    record carries ``quotes_unverified``; the counts reach SkimResult and the
    run stats."""

    def test_failing_quote_is_nulled_and_counted_but_the_item_stays(self):
        doc = _doc()
        res, _, store = _run([doc], [_record(
            decisions=["we agreed to move the beta launch to the 14th"],           # verbatim
            questions=["the pricing question was deferred to the steering group"],  # not in the text
        )])
        stored = store.rows[(doc["id"], MODEL, PROMPT_VERSION)]["skim"]
        assert stored["decisions"][0]["quote"] == "we agreed to move the beta launch to the 14th"
        assert stored["open_questions"][0] == {"text": "Tier order open.", "quote": None}
        assert stored["quotes_unverified"] == 1
        assert (res.quotes_emitted, res.quotes_verbatim, res.quotes_unverified) == (2, 1, 1)
        assert res.to_stats()["skim_quotes_unverified"] == 1

    def test_near_verbatim_markdown_differences_still_verify(self):
        """The token path accepts markdown / punctuation drift, so only real
        paraphrase is nulled."""
        doc = _doc(content="- **Decision:** we agreed to move the *beta launch* to the 14th.\n")
        res, _, store = _run([doc], [_record(decisions=["Decision: we agreed to move the beta launch to the 14th"])])
        stored = store.rows[(doc["id"], MODEL, PROMPT_VERSION)]["skim"]
        assert stored["decisions"][0]["quote"] is not None and stored["quotes_unverified"] == 0
        assert res.quotes_unverified == 0

    def test_counts_are_only_for_stored_records(self):
        """An invalid reply or a failed store contributes no quote counts."""
        docs = [_doc(), _doc()]
        store = FakeStore(fail_upsert_for={docs[1]["id"]})
        res, _, _ = _run(docs, [_record(questions=["invented passage"]), _record(questions=["invented passage"])],
                         store=store)
        assert res.skimmed == 1 and res.failed == 1
        assert (res.quotes_emitted, res.quotes_unverified) == (1, 1)

    def test_coerced_records_are_counted_by_class(self):
        docs = [_doc(), _doc(), _doc(DOC_B)]
        res, _, store = _run(docs, [
            _record(summary="Sentence one is here. " * 20),                 # > 300 chars: summary trimmed
            _record(topics=[f"t{i}" for i in range(8)]),                   # topics truncated
            _record(),
        ])
        assert res.skimmed == 3 and res.coerced == 2
        assert res.coercion_classes == {"summary_trimmed": 1, "topics_truncated": 1}
        stored = store.rows[(docs[0]["id"], MODEL, PROMPT_VERSION)]["skim"]
        assert len(stored["summary"]) <= 300 and stored["summary"].endswith("here.")
        assert res.to_stats()["skim_coerced"] == 2

    def test_strict_mode_counts_overruns_as_invalid(self):
        res, _, _ = _run([_doc()], [_record(topics=[f"t{i}" for i in range(8)])], coerce=False)
        assert res.invalid == 1 and res.skimmed == 0


class TestLoggingIsCountsOnly:
    def test_no_document_text_titles_or_quotes_in_logs(self, caplog):
        title = "Synthetic title that must not be logged"
        summary = "A summary sentence that must not be logged."
        quote = "we agreed to move the beta launch to the 14th"
        docs = [_doc(title=title), _doc(title=title), _doc(title=title, content="x" * 300)]
        with caplog.at_level(logging.DEBUG, logger="src.skim.runner"):
            _run(docs, [_record(summary=summary, decisions=[quote]), RuntimeError(f"failed on {title}"),
                        _record(doc_kind=title)], max_doc_chars=100)
        text = caplog.text
        for forbidden in (title, summary, quote, "Priya", "Tomasz", "Orrery", DOC_A.splitlines()[0]):
            assert forbidden not in text
        assert "1 skimmed" in text and "1 failed" in text and "1 invalid" in text and "3 truncated" in text
        assert "RuntimeError" in text                    # the class is logged, not the message
