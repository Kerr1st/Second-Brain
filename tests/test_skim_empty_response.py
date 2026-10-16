"""Triage I1/I5 (2026-10-15): empty Nova skim replies, their retry, and the backlog.

* Bedrock: empty content (no text, no tool call) with a non-``max_tokens`` stop
  raises a typed :class:`EmptyModelResponse` carrying the stop reason, instead of
  handing ``""`` to the JSON parser; the metrics line records ``stop_reason`` and
  ``served_tier``. Refusal/content-filter and ``max_tokens`` paths are unchanged.
* Skim runner: one retry on ``EmptyModelResponse`` (standard tier when the
  backend supports a per-call override), metered against the skim sub-cap.
* Backlog: earlier-window arrivals an earlier pass had in scope but stored
  nothing for are re-selected, bounded; never-in-scope ones are not.
* I5: coercion classes are in ``SkimResult.to_stats()``.

Synthetic documents and an injected fake ``bedrock-runtime`` client only.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.backends.base import ModelRefusalError
from src.backends.bedrock import BedrockInvoker
from src.backends.cost import SpendTracker, estimate_cost_usd
from src.backends.direct_api import EmptyModelResponse
from src.prompts.skim import PROMPT_VERSION
from src.skim import runner
from src.skim.runner import SkimResult, skim_window
from src.skim.schema import SKIM_SCHEMA

MODEL = "us.amazon.nova-2-lite-v1:0"
WINDOW = datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)
DOC = ("Orrery sync — 18 Sep\nPriya: after the load test we agreed to move the beta launch to the 14th.\n"
       "Tomasz: the pricing tier question is still open: which tier goes first?\n")

#: The live failure shape (10-14 / 10-16 metrics: zero usage, no content).
EMPTY_REPLY = {"stopReason": "malformed_tool_use", "output": {"message": {"content": []}},
               "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}}


def _record(**over):
    rec = {"summary": "Meeting notes agreeing to move the beta launch to the 14th.",
           "people": ["Priya", "Tomasz"], "entities": ["Orrery"], "topics": ["beta launch"],
           "decisions": [], "open_questions": [], "novelty": 2, "salience": 3,
           "contradiction_hint": None, "doc_kind": "conversation"}
    rec.update(over)
    return rec


def _tool_reply(record=None, tier="flex", usage=(3000, 200)):
    return {"stopReason": "tool_use",
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": "structured_output", "input": record or _record()}}]}},
            "usage": {"inputTokens": usage[0], "outputTokens": usage[1], "totalTokens": sum(usage)},
            "serviceTier": {"type": tier}}


class _Client:
    """Fake ``bedrock-runtime``: queued responses (or exceptions); records kwargs."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _nova(client, tier="flex"):
    return BedrockInvoker(model=MODEL, client=client, temperature=0.0, service_tier=tier, max_tokens=4096)


@pytest.fixture(autouse=True)
def metrics_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.backends.direct_api.METRICS_DIR", str(tmp_path / "llm_metrics"))
    return tmp_path / "llm_metrics"


def _metrics(metrics_dir, run_id):
    return [json.loads(line) for line in (metrics_dir / f"{run_id}.jsonl").read_text().splitlines()]


# --------------------------------------------------------------------------- #
# Bedrock: typed error + observability                                         #
# --------------------------------------------------------------------------- #
class TestBedrockEmptyResponse:
    def test_empty_content_raises_typed_error_with_stop_reason(self, metrics_dir):
        inv = _nova(_Client(json.loads(json.dumps(EMPTY_REPLY))))
        with pytest.raises(EmptyModelResponse) as ei:
            inv.invoke("sys", "x" * 64_000, schema=SKIM_SCHEMA, stage="skim", run_id="r-empty")
        exc = ei.value
        assert exc.stop_reason == "malformed_tool_use"
        # Response-bearing: invoke_metered bills it (I2's conservative chars/3 usage).
        assert exc.model_response_received is True and exc.usage_source == "real"
        assert exc.usage["inputTokensEstimated"] is True
        (line,) = _metrics(metrics_dir, "r-empty")
        assert line["stop_reason"] == "malformed_tool_use"
        assert line["error"] == "EmptyModelResponse" and line["success"] is False
        assert line["output_chars"] == 0
        assert "served_tier" in line and line["served_tier"] is None  # the empty reply echoed no tier

    def test_backward_compatible_with_both_old_handler_families(self):
        exc = EmptyModelResponse("x", stop_reason="end_turn")
        # Every handler that caught the old ValueError (JSON parse failure) or a
        # RuntimeError (provider failure) still catches it; refusal handlers do not.
        assert isinstance(exc, ValueError) and isinstance(exc, RuntimeError)
        assert not isinstance(exc, ModelRefusalError)

    @pytest.mark.parametrize("content", [
        [],
        [{"reasoningContent": {"reasoningText": {"text": "thinking only"}}}],
        [{"text": "   "}],
    ])
    def test_no_text_or_tool_block_is_empty(self, content):
        reply = {"stopReason": "end_turn", "output": {"message": {"content": content}},
                 "usage": {"inputTokens": 10, "outputTokens": 0, "totalTokens": 10}}
        with pytest.raises(EmptyModelResponse) as ei:
            _nova(_Client(reply)).invoke("sys", "user", schema=SKIM_SCHEMA)
        assert ei.value.stop_reason == "end_turn"

    @pytest.mark.parametrize("stop", ["content_filtered", "guardrail_intervened", "refusal"])
    def test_refusals_still_map_to_model_refusal_error(self, stop):
        reply = dict(EMPTY_REPLY, stopReason=stop)
        with pytest.raises(ModelRefusalError) as ei:
            _nova(_Client(reply)).invoke("sys", "user", schema=SKIM_SCHEMA)
        assert not isinstance(ei.value, EmptyModelResponse)

    def test_max_tokens_stays_the_truncation_runtime_error(self):
        reply = dict(EMPTY_REPLY, stopReason="max_tokens",
                     usage={"inputTokens": 10, "outputTokens": 4096, "totalTokens": 4106})
        with pytest.raises(RuntimeError, match="max_tokens") as ei:
            _nova(_Client(reply)).invoke("sys", "user", schema=SKIM_SCHEMA)
        assert not isinstance(ei.value, EmptyModelResponse)

    def test_success_records_stop_reason_and_served_tier(self, metrics_dir):
        out = _nova(_Client(_tool_reply(tier="flex"))).invoke("sys", "user", schema=SKIM_SCHEMA, run_id="r-ok")
        assert out["output"]["summary"]
        (line,) = _metrics(metrics_dir, "r-ok")
        assert line["stop_reason"] == "tool_use" and line["served_tier"] == "flex" and line["success"] is True
        assert "requested_tier" not in line  # no per-call override

    def test_per_call_service_tier_override(self, metrics_dir):
        client = _Client(_tool_reply(tier="flex"), _tool_reply(tier="default"))
        inv = _nova(client, tier="flex")
        inv.invoke("sys", "user", schema=SKIM_SCHEMA, run_id="r-tier")
        inv.invoke("sys", "user", schema=SKIM_SCHEMA, run_id="r-tier", service_tier="default")
        assert [c["serviceTier"] for c in client.calls] == [{"type": "flex"}, {"type": "default"}]
        assert inv._service_tier == "flex"  # the override does not stick
        lines = _metrics(metrics_dir, "r-tier")
        assert [ln["served_tier"] for ln in lines] == ["flex", "default"]
        assert lines[1]["requested_tier"] == "default"

    def test_invalid_tier_override_rejected_before_any_call(self):
        client = _Client(_tool_reply())
        with pytest.raises(ValueError, match="service_tier"):
            _nova(client).invoke("sys", "user", schema=SKIM_SCHEMA, service_tier="cheap")
        assert client.calls == []

    def test_wrapped_like_any_response_bearing_failure_in_codex_semantic_path(self):
        # capture/codex.py wraps response-bearing failures by type name; the empty
        # reply must stay on that path (model_response_received), as the ValueError did.
        try:
            _nova(_Client(json.loads(json.dumps(EMPTY_REPLY)))).invoke("sys", "user")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "model_response_received", False) is True
            assert type(exc).__name__ == "EmptyModelResponse"
        else:  # pragma: no cover
            pytest.fail("expected EmptyModelResponse")


# --------------------------------------------------------------------------- #
# Skim runner: one retry                                                      #
# --------------------------------------------------------------------------- #
def _doc(doc_id=None, created_at=WINDOW, content=DOC):
    return {"id": doc_id or str(uuid.uuid4()), "title": "Synthetic", "source_type": "slack",
            "created_at": created_at, "content": content}


def _skim(docs, client, *, cap=1.0, tracker=None, **kw):
    tracker = tracker or SpendTracker(cap_usd=9.0)
    res = skim_window(WINDOW, invoker=_nova(client), model=MODEL, prompt_version=PROMPT_VERSION,
                      tracker=tracker, cap_usd=cap, run_id="run-1", fetch_docs=lambda ws: docs,
                      store=_Store(), **kw)
    return res, tracker


class TestSkimRetry:
    def test_empty_then_success_is_skimmed_on_default_tier_and_charged(self):
        client = _Client(json.loads(json.dumps(EMPTY_REPLY)), _tool_reply(tier="default"))
        res, tracker = _skim([_doc(content="y" * 60_013)], client)
        assert (res.skimmed, res.failed, res.invalid) == (1, 0, 0)
        assert (res.empty_responses, res.retries, res.retries_recovered) == (1, 1, 1)
        assert "serviceTier" in client.calls[0] and client.calls[0]["serviceTier"] == {"type": "flex"}
        assert client.calls[1]["serviceTier"] == {"type": "default"}
        # Both attempts are on the run tracker and on the skim's own spend.
        assert tracker.total_usd > 0 and res.cost_usd == pytest.approx(tracker.total_usd)
        retry_cost = estimate_cost_usd(MODEL, {"inputTokens": 3000, "outputTokens": 200, "serviceTier": "default"})
        assert res.cost_usd > retry_cost  # the empty attempt was billed too (chars/3, I2)
        stats = res.to_stats()
        assert stats["skim_retries"] == 1 and stats["skim_retries_recovered"] == 1
        assert stats["skim_empty_responses"] == 1

    def test_empty_twice_fails_after_one_retry(self):
        client = _Client(json.loads(json.dumps(EMPTY_REPLY)), json.loads(json.dumps(EMPTY_REPLY)),
                         _tool_reply())
        res, _ = _skim([_doc(), _doc()], client)
        assert (res.skimmed, res.failed) == (1, 1)
        assert (res.empty_responses, res.retries, res.retries_recovered) == (2, 1, 0)
        assert len(client.calls) == 3  # 2 for the first document, 1 for the second

    def test_other_errors_are_not_retried(self):
        client = _Client(RuntimeError("Throttled: slow down"), _tool_reply())
        res, _ = _skim([_doc(), _doc()], client)
        assert (res.skimmed, res.failed, res.retries) == (1, 1, 0) and len(client.calls) == 2

    def test_no_retry_once_the_sub_cap_is_reached(self):
        client = _Client(json.loads(json.dumps(EMPTY_REPLY)), _tool_reply())
        # 120k chars → ~40k conservative input tokens ≈ $0.0066 at Flex: over a $0.001 sub-cap.
        res, _ = _skim([_doc(content="z" * 120_000)], client, cap=0.001)
        assert (res.failed, res.retries, res.empty_responses) == (1, 0, 1) and len(client.calls) == 1

    def test_retry_without_tier_override_support_reuses_the_call(self):
        class _NoOverride:
            model, BACKEND_NAME = MODEL, "bedrock"

            def __init__(self):
                self.kw = []

            def invoke(self, system_prompt, user_message, **kw):
                self.kw.append(kw)
                if len(self.kw) == 1:
                    exc = EmptyModelResponse("empty", stop_reason="malformed_model_output")
                    exc.model_response_received, exc.usage_source = True, "real"
                    exc.usage = {"inputTokens": 100, "outputTokens": 0, "serviceTier": "flex"}
                    raise exc
                return {"output": _record(), "raw": "{}", "usage_source": "real",
                        "usage": {"inputTokens": 100, "outputTokens": 10, "serviceTier": "flex"}}

        inv = _NoOverride()
        res = skim_window(WINDOW, invoker=inv, model=MODEL, prompt_version=PROMPT_VERSION,
                          tracker=SpendTracker(9.0), cap_usd=1.0, run_id="r", fetch_docs=lambda ws: [_doc()],
                          store=_Store())
        assert res.skimmed == 1 and res.retries == 1 and all("service_tier" not in k for k in inv.kw)


# --------------------------------------------------------------------------- #
# Backlog: failed-before arrivals from earlier windows                         #
# --------------------------------------------------------------------------- #
class _Store:
    """In-memory store honouring ``window_start`` (unlike the runner tests' fake)."""

    def __init__(self, arrivals=()):
        self.docs = {d["id"]: d for d in arrivals}
        self.rows: dict[tuple, dict] = {}
        self.fetched: list[list[str]] = []

    def add_row(self, memory_id, skimmed_at, prompt_version=PROMPT_VERSION):
        self.rows[(memory_id, MODEL, prompt_version)] = {"memory_id": memory_id, "created_at": skimmed_at}

    def list_arrivals(self, window_start, limit=5000):
        rows = [{k: v for k, v in d.items() if k != "content"}
                for d in self.docs.values() if d["created_at"] >= window_start]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    def missing_ids(self, ids, model, prompt_version):
        return [i for i in dict.fromkeys(map(str, ids)) if (i, model, prompt_version) not in self.rows]

    def fetch_records(self, ids, model, prompt_version):
        return {i: self.rows[(i, model, prompt_version)] for i in ids if (i, model, prompt_version) in self.rows}

    def fetch_documents(self, ids):
        ids = list(ids)
        self.fetched.append(ids)
        return [self.docs[i] for i in ids if i in self.docs]

    def upsert_skim(self, memory_id, **kw):
        self.rows[(memory_id, kw["model"], kw["prompt_version"])] = {"memory_id": memory_id,
                                                                     "created_at": WINDOW + timedelta(hours=1)}
        return "row"


def _hours(h):
    return WINDOW - timedelta(hours=h)


def _skim_store(store, client, **kw):
    return skim_window(WINDOW, invoker=_nova(client), model=MODEL, prompt_version=PROMPT_VERSION,
                       tracker=SpendTracker(9.0), cap_usd=1.0, run_id="run-2", store=store, **kw)


class TestBacklog:
    def _previous_pass(self):
        """Prior window [-48h, 0): A, C skimmed at -1h by the previous run; F (between them) failed."""
        a, f, c = _doc("a" * 8, _hours(40)), _doc("f" * 8, _hours(30)), _doc("c" * 8, _hours(20))
        now_doc = _doc("n" * 8, WINDOW + timedelta(minutes=5))
        store = _Store([a, f, c, now_doc])
        store.add_row(a["id"], _hours(1))
        store.add_row(c["id"], _hours(1))
        return store, a, f, c, now_doc

    def test_prior_window_doc_without_a_row_is_picked_up(self):
        store, _, f, _, now_doc = self._previous_pass()
        res = _skim_store(store, _Client(_tool_reply(), _tool_reply()))
        assert res.docs_total == 1  # the window itself is unchanged
        assert res.backlog == 1 and res.backlog_ids == [f["id"]]
        assert res.skimmed == 2 and (f["id"], MODEL, PROMPT_VERSION) in store.rows
        assert store.fetched[0] == [now_doc["id"]] and store.fetched[1] == [f["id"]]
        assert res.to_stats()["skim_backlog"] == 1

    def test_idempotent_second_run_selects_nothing(self):
        store, *_ = self._previous_pass()
        _skim_store(store, _Client(_tool_reply(), _tool_reply()))
        client = _Client()
        res = _skim_store(store, client)
        assert res.backlog == 0 and res.skimmed == 0 and client.calls == []

    def test_never_in_scope_docs_are_not_selected(self):
        # Prompt-version bump / skim disabled: the old docs have no row under the
        # current key, and no current-key row covers them, so no re-skim storm.
        old1, old2 = _doc("1" * 8, _hours(40)), _doc("2" * 8, _hours(30))
        store = _Store([old1, old2])
        store.add_row(old1["id"], _hours(1), prompt_version="older-prompt")
        store.add_row(old2["id"], _hours(1), prompt_version="older-prompt")
        res = _skim_store(store, _Client())
        assert res.backlog == 0 and res.docs_total == 0

    def test_doc_older_than_every_skimmed_arrival_is_not_selected(self):
        # The only rows are for arrivals newer than D: no proof D was in a pass's window.
        d, newer = _doc("d" * 8, _hours(60)), _doc("e" * 8, _hours(10))
        store = _Store([d, newer])
        store.add_row(newer["id"], _hours(1))
        assert _skim_store(store, _Client()).backlog == 0

    def test_doc_that_arrived_after_the_covering_pass_is_not_selected(self):
        a, late = _doc("a" * 8, _hours(40)), _doc("b" * 8, _hours(5))
        store = _Store([a, late])
        store.add_row(a["id"], _hours(30))  # skimmed before `late` arrived
        assert _skim_store(store, _Client()).backlog == 0

    def test_bounded_by_max_docs_and_lookback(self):
        anchor = _doc("0" * 8, _hours(24 * 13 + 23))
        failed = [_doc(None, _hours(24 * 13 - i)) for i in range(1, 31)]
        too_old = _doc("o" * 8, _hours(24 * 15))  # outside the 14-day lookback, no row
        store = _Store([anchor, too_old, *failed])
        store.add_row(anchor["id"], _hours(1))
        res = _skim_store(store, _Client(*[_tool_reply() for _ in range(25)]))
        assert res.backlog == runner.DEFAULT_BACKLOG_MAX_DOCS == 20
        newest_first = sorted(failed, key=lambda d: d["created_at"], reverse=True)
        assert res.backlog_ids == [d["id"] for d in newest_first[:20]]
        assert too_old["id"] not in res.backlog_ids

    def test_disabled_and_fetch_docs_override_skip_the_backlog(self):
        store, *_ = self._previous_pass()
        assert _skim_store(store, _Client(_tool_reply()), backlog_max_docs=0).backlog == 0

    def test_backlog_failure_is_non_fatal(self):
        store, *_ = self._previous_pass()

        def boom(*a, **k):
            raise RuntimeError("db down")

        store.fetch_records = boom
        res = _skim_store(store, _Client(_tool_reply()))
        assert res.backlog == 0 and res.skimmed == 1  # the window's own doc still skimmed

    def test_store_without_fetch_records_skips_backlog(self):
        assert runner._select_backlog(WINDOW, MODEL, PROMPT_VERSION, store=object(),
                                      lookback_days=14, max_docs=20) == []


# --------------------------------------------------------------------------- #
# I5: coercion classes persisted                                               #
# --------------------------------------------------------------------------- #
def test_to_stats_carries_coercion_classes():
    res = SkimResult(coercion_classes={"summary_trimmed": 2, "decisions[0].quote_trimmed": 1})
    stats = res.to_stats()
    assert stats["skim_coercion_classes"] == {"summary_trimmed": 2, "decisions[0].quote_trimmed": 1}
    stats["skim_coercion_classes"]["x"] = 1
    assert "x" not in res.coercion_classes  # a copy, not the live dict
    json.dumps(stats)  # persisted as JSON on dream_cycle_runs.stats


def test_skim_window_fills_coercion_classes_in_stats():
    long_summary = "A sentence about the launch. " * 20  # > 300 chars → trimmed
    res, _ = _skim([_doc()], _Client(_tool_reply(_record(summary=long_summary))))
    assert res.coerced == 1
    assert res.to_stats()["skim_coercion_classes"] == res.coercion_classes and res.coercion_classes
