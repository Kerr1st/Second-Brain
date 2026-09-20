"""Hermetic tests for scripts/skim_probe.py (fake invoker, no DB, no model calls).

Synthetic documents only. The probe's stdout is aggregates-only: the tests
assert that no title, summary, quote or document text ever reaches it.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest

from scripts import skim_probe as sp
from src.backends.cost import SpendCapExceeded, SpendTracker
from src.prompts.skim import PROMPT_VERSION, SKIM_SYSTEM_PROMPT
from src.skim.schema import SKIM_SCHEMA

MODEL = "us.amazon.nova-2-lite-v1:0"

DOC_A = ("Orrery sync — 18 Sep\nPriya: after the load test we agreed to move the beta launch to the 14th.\n"
         "Tomasz: the pricing tier question is still open: which tier goes first?\n")
DOC_B = ("Weekly digest: three articles on container scheduling. Nothing actionable; the author "
         "recommends reading the second one for the benchmark method.\n")


def _doc(content, channel="slack", title="Synthetic title"):
    return {"id": str(uuid.uuid4()), "title": title, "source_type": channel,
            "created_at": "2026-09-18T09:00:00+00:00", "content": content}


def _record(summary="Meeting notes agreeing to move the beta launch to the 14th.", decisions=(), questions=(),
            salience=3, **over):
    rec = {"summary": summary, "people": ["Priya", "Tomasz"], "entities": ["Orrery"], "topics": ["beta launch"],
           "decisions": [{"text": "Launch moves.", "quote": q} for q in decisions],
           "open_questions": [{"text": "Tier order open.", "quote": q} for q in questions],
           "novelty": 2, "salience": salience, "contradiction_hint": None, "doc_kind": "conversation"}
    rec.update(over)
    return rec


class _FakeInvoker:
    """Returns the queued outputs in order; records every call."""

    def __init__(self, outputs, usage=(3000, 200), tier="flex"):
        self.outputs = list(outputs)
        self.calls = []
        self._usage = usage
        self._tier = tier

    def invoke(self, system_prompt, user_message, **kw):
        self.calls.append({"system": system_prompt, "user": user_message, **kw})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        usage = {"inputTokens": self._usage[0], "outputTokens": self._usage[1]}
        if self._tier:
            usage["serviceTier"] = self._tier
        return {"output": out, "raw": json.dumps(out), "usage": usage, "usage_source": "real"}


class TestSkimDocument:
    def test_request_and_record_shape(self):
        doc = _doc(DOC_A)
        inv = _FakeInvoker([_record(decisions=["we agreed to move the beta launch to the 14th"],
                                    questions=["which tier goes first?"])])
        rec = sp.skim_document(doc, inv, MODEL, SpendTracker(cap_usd=1), run_id="r1")
        call = inv.calls[0]
        assert call["system"] == SKIM_SYSTEM_PROMPT
        assert DOC_A in call["user"] and "channel: slack" in call["user"]
        assert call["schema"] is SKIM_SCHEMA and call["effort"] == "low" and call["stage"] == "skim_probe"
        assert rec["valid"] is True and rec["error"] is None
        assert rec["faithfulness"]["quotes_emitted"] == 2 and rec["faithfulness"]["all_verbatim"] is True
        assert rec["input_tokens"] == 3000 and rec["output_tokens"] == 200
        assert rec["service_tier_served"] == "flex" and rec["prompt_version"] == PROMPT_VERSION
        # Flex price: 3000 * 0.165/M + 200 * 1.375/M
        assert rec["cost_usd"] == pytest.approx(3000 * 0.165e-6 + 200 * 1.375e-6, abs=1e-7)

    def test_invalid_record_is_recorded_not_fatal(self):
        inv = _FakeInvoker([_record(salience=4)])
        rec = sp.skim_document(_doc(DOC_A), inv, MODEL, SpendTracker(cap_usd=1))
        assert rec["valid"] is False and "salience" in rec["validation_error"]
        assert rec["faithfulness"] is None and rec["record"] is None
        assert rec["cost_usd"] > 0                     # the call still cost money and is metered

    def test_fabricated_quote_is_counted(self):
        inv = _FakeInvoker([_record(decisions=["we decided to cancel the launch"])])
        rec = sp.skim_document(_doc(DOC_A), inv, MODEL, SpendTracker(cap_usd=1))
        assert rec["valid"] is True
        assert rec["faithfulness"]["quotes_fabricated"] == 1 and rec["faithfulness"]["all_verbatim"] is False

    def test_list_output_takes_first_element(self):
        inv = _FakeInvoker([[_record()]])
        rec = sp.skim_document(_doc(DOC_A), inv, MODEL, SpendTracker(cap_usd=1))
        assert rec["valid"] is True

    def test_invoker_error_is_recorded_not_fatal(self):
        inv = _FakeInvoker([RuntimeError("Throttled: slow down")])
        rec = sp.skim_document(_doc(DOC_A), inv, MODEL, SpendTracker(cap_usd=1))
        assert rec["error"].startswith("RuntimeError: Throttled") and rec["valid"] is None
        assert rec["cost_usd"] == 0.0

    def test_dry_run_makes_no_call(self):
        inv = _FakeInvoker([])
        rec = sp.skim_document(_doc(DOC_A), inv, MODEL, SpendTracker(cap_usd=1), dry_run=True)
        assert inv.calls == [] and rec["dry_run"] is True and rec["prompt_chars"] > len(DOC_A)


class TestRunProbeAndAggregate:
    def test_aggregates(self):
        docs = [_doc(DOC_A, "slack"), _doc(DOC_B, "article"), _doc(DOC_A, "slack"), _doc(DOC_B, "email")]
        inv = _FakeInvoker([
            _record(decisions=["we agreed to move the beta launch to the 14th"], questions=["which tier goes first?"]),
            _record(summary="", salience=0, decisions=["the author recommends reading the second one",
                                                        "everyone must migrate by Monday"]),  # 1 verbatim, 1 fabricated
            _record(salience=4),                                                                # invalid
            RuntimeError("boom"),                                                               # error
        ])
        seen = []
        recs = sp.run_probe(docs, inv, MODEL, SpendTracker(cap_usd=1), sink=seen.append)
        assert len(recs) == 4 and seen == recs
        agg = sp.aggregate(recs)
        assert agg["docs"] == 4 and agg["calls"] == 4 and agg["errors"] == 1
        assert agg["json_valid"] == 2 and agg["json_valid_pct"] == 50.0
        assert agg["invalid_reasons"] == {"salience must be between N and N": 1}
        assert agg["with_decision_or_question_pct"] == 100.0
        assert agg["quotes_emitted"] == 4 and agg["quotes_verbatim"] == 3 and agg["quote_verbatim_pct"] == 75.0
        assert agg["fabricated_quotes"] == 1 and agg["records_with_fabrication"] == 1
        assert agg["empty_summaries"] == 1
        assert agg["input_tokens"] == 9000 and agg["output_tokens"] == 600      # three replies
        assert agg["total_cost_usd"] == pytest.approx(3 * (3000 * 0.165e-6 + 200 * 1.375e-6), abs=1e-6)
        assert agg["service_tiers_served"] == {"flex": 3, "n/a": 1}
        assert agg["latency_mean_s"] is not None and agg["latency_max_s"] is not None
        assert agg["per_channel"] == {
            "article": {"docs": 1, "valid": 1, "quotes_fabricated": 1},
            "email": {"docs": 1, "errors": 1},
            "slack": {"docs": 2, "valid": 1, "quotes_fabricated": 0, "invalid": 1},
        }

    def test_cap_stops_the_probe(self):
        inv = _FakeInvoker([_record(), _record()], usage=(2_000_000, 0))
        with pytest.raises(SpendCapExceeded):
            sp.run_probe([_doc(DOC_A), _doc(DOC_A)], inv, MODEL, SpendTracker(cap_usd=0.30))
        assert len(inv.calls) == 1

    def test_dry_run_aggregate(self):
        recs = sp.run_probe([_doc(DOC_A), _doc(DOC_B, "article")], None, MODEL, SpendTracker(cap_usd=1), dry_run=True)
        agg = sp.aggregate(recs)
        assert agg == {**agg, "docs": 2, "dry_run_docs": 2, "calls": 0, "json_valid_pct": None,
                       "quote_verbatim_pct": None, "total_cost_usd": 0.0}
        assert agg["per_channel"] == {"article": {"docs": 1}, "slack": {"docs": 1}}


class TestStdoutIsAggregatesOnly:
    @pytest.mark.parametrize("message, key", [
        ("open_questions[0].quote is 163 characters; the limit is 160", "open_questions[].quote is N characters"),
        ("decisions[2] is missing its verbatim quote", "decisions[] is missing its verbatim quote"),
        ("doc_kind must be one of ['article', 'note'], got 'Secret Project Title'", "doc_kind must be one of ['article', 'note']"),
        ("salience must be between 0 and 3, got 4", "salience must be between N and N"),
        ("skim record has unknown field(s): ['confidence']", "skim record has unknown field(s): ['confidence']"),
    ])
    def test_reason_key_generalises_and_drops_model_values(self, message, key):
        assert sp.reason_key(message) == key

    def test_print_aggregates_leaks_no_document_text(self, capsys):
        title = "Synthetic title that must not be printed"
        docs = [_doc(DOC_A, "slack", title=title), _doc(DOC_A, "slack", title=title)]
        summary = "A summary sentence that must not be printed."
        quote = "we agreed to move the beta launch to the 14th"
        inv = _FakeInvoker([_record(summary=summary, decisions=[quote]),
                            _record(doc_kind=title)])            # invalid: model value lands in the error text
        recs = sp.run_probe(docs, inv, MODEL, SpendTracker(cap_usd=1))
        assert title in recs[1]["validation_error"]              # the jsonl may carry it; stdout must not
        sp.print_aggregates(sp.aggregate(recs), model=MODEL, out_path="/tmp/x.jsonl")
        out = capsys.readouterr().out
        for forbidden in (title, summary, quote, "Priya", "Tomasz", "Orrery", DOC_A.splitlines()[0]):
            assert forbidden not in out
        assert "JSON-valid            1/2" in out and "quotes verbatim       1/1" in out
        assert "invalid:   1 × doc_kind must be one of" in out
        assert "slack" in out and "records: /tmp/x.jsonl" in out

    def test_aggregate_dict_carries_no_text_fields(self):
        inv = _FakeInvoker([_record(decisions=["we agreed to move the beta launch to the 14th"])])
        recs = sp.run_probe([_doc(DOC_A)], inv, MODEL, SpendTracker(cap_usd=1))
        flat = json.dumps(sp.aggregate(recs))
        assert "beta launch" not in flat and "Synthetic" not in flat


class TestCli:
    def test_dry_run_end_to_end_writes_jsonl_and_prints_aggregates(self, tmp_path, monkeypatch, capsys):
        docs = [_doc(DOC_A), _doc(DOC_B, "article")]
        monkeypatch.setattr(sp, "fetch_documents", lambda ids, since, limit: docs)
        monkeypatch.setattr(sp, "OUT_DIR", str(tmp_path))
        assert sp.main(["--ids", docs[0]["id"], docs[1]["id"], "--dry-run"]) == 0
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = [json.loads(l) for l in files[0].read_text().splitlines()]
        assert [l["memory_id"] for l in lines] == [d["id"] for d in docs] and all(l["dry_run"] for l in lines)
        out = capsys.readouterr().out
        assert "docs 2  calls 0  errors 0  dry-run 2" in out and DOC_A.splitlines()[0] not in out

    def test_profile_sets_aws_profile_and_unknown_model_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "fetch_documents", lambda ids, since, limit: [])
        monkeypatch.setattr(sp, "OUT_DIR", str(tmp_path))
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        sp.main(["--limit", "1", "--dry-run", "--profile", "second-brain-jobs"])
        import os
        assert os.environ["AWS_PROFILE"] == "second-brain-jobs"
        with pytest.raises(SystemExit, match="_PRICE_TABLE"):
            sp.main(["--limit", "1", "--dry-run", "--model", "vendor.mystery-model"])

    def test_selection_required(self):
        with pytest.raises(SystemExit, match="need --ids"):
            sp.fetch_documents(None, None, None)

    def test_live_invoker_is_built_with_the_skim_settings(self, monkeypatch, tmp_path):
        made = {}
        class FakeBedrock:
            def __init__(self, model, **kw):
                made.update(model=model, **kw)
            def invoke(self, *a, **kw):
                raise AssertionError("no live call in tests")
        monkeypatch.setattr(sp, "BedrockInvoker", FakeBedrock)
        monkeypatch.setattr(sp, "fetch_documents", lambda ids, since, limit: [])
        monkeypatch.setattr(sp, "OUT_DIR", str(tmp_path))
        sp.main(["--limit", "1"])
        assert made == {"model": MODEL, "service_tier": "flex", "temperature": 0.0, "max_tokens": 4096}
        sp.main(["--limit", "1", "--service-tier", "none", "--temperature", "0.3", "--max-tokens", "512",
                 "--model", "google.gemma-3-12b-it"])
        assert made == {"model": "google.gemma-3-12b-it", "service_tier": None, "temperature": 0.3, "max_tokens": 512}


class TestNoWrites:
    def test_module_reads_memories_but_never_writes(self):
        src = open(sp.__file__, encoding="utf-8").read()
        assert not re.search(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+memories", src, re.I)
        assert "SELECT id, title, source_type, created_at, content FROM memories" in src
