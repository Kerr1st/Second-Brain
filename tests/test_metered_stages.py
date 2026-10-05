"""Hermetic routing/accounting regressions for corpus-bearing standalone stages."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import math

from src.backends.base import CorpusRoutingError, ModelRefusalError
from src.backends.cost import (
    DEFAULT_RESERVED_OUTPUT_TOKENS, SpendTracker, SpendCapExceeded, UsageUnavailable, cap_from_env,
    invoke_metered, reservation_usd,
)
from src.backends.direct_api import DirectApiInvoker
from src.backends.resolver import EDITOR_ROLE, RoleBackend

MODEL = "us.amazon.nova-2-lite-v1:0"
SPEC = RoleBackend(EDITOR_ROLE, "bedrock", MODEL, "low")
USAGE = {"inputTokens": 1000, "outputTokens": 100}


class FakeDirect(DirectApiInvoker):
    BACKEND_NAME = "bedrock"

    def __init__(self, *, raw='{"ok":true}', usage=None, error=None, stop=None):
        super().__init__(MODEL)
        self.raw = raw
        self.usage = USAGE if usage is None else usage
        self.error = error
        self.stop = stop
        self.calls = 0
        self.metrics = []

    def _invoke_provider(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return {"stopReason": self.stop}

    def _extract_raw(self, response):
        if self.raw == "truncate":
            raise RuntimeError("truncated")
        return self.raw

    def _extract_usage(self, **kwargs):
        return self.usage, "real"

    def _record_metrics(self, metrics, run_id):
        self.metrics.append(metrics)


def invoke(invoker, tracker):
    return invoke_metered(invoker, SPEC, tracker, "synthetic system", "synthetic input")


def test_success_records_once():
    invoker = FakeDirect()
    tracker = SpendTracker(1)
    assert invoke(invoker, tracker)["output"] == {"ok": True}
    assert invoker.calls == tracker.calls == 1
    assert tracker.total_usd == pytest.approx(0.000605)


@pytest.mark.parametrize("raw,error", [("not JSON", ValueError), ("truncate", RuntimeError)])
def test_invalid_provider_output_still_billed(raw, error):
    invoker = FakeDirect(raw=raw)
    tracker = SpendTracker(1)
    with pytest.raises(error) as raised:
        invoke(invoker, tracker)
    assert raised.value.model_response_received is True
    assert raised.value.usage == USAGE
    assert tracker.calls == 1
    assert tracker.total_usd > 0
    assert invoker.metrics[0]["real_input_tokens"] == 1000


@pytest.mark.parametrize("reason", ["content_filtered", "guardrail_intervened", "refusal"])
def test_explicit_refusal_is_typed_and_billed(reason):
    invoker = FakeDirect(stop=reason)
    tracker = SpendTracker(1)
    with pytest.raises(ModelRefusalError):
        invoke(invoker, tracker)
    assert tracker.calls == 1


@pytest.mark.parametrize("usage", [
    {}, {"inputTokens": 3}, {"inputTokens": -1, "outputTokens": 2},
    {"inputTokens": True, "outputTokens": 2},
    {"inputTokens": 1, "outputTokens": float("nan")},
    {"total_cost_usd": float("inf")}, {"total_cost_usd": -0.1},
])
def test_unmeterable_response_blocks_following_calls(usage):
    invoker = FakeDirect(usage=usage)
    tracker = SpendTracker(1)
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)
    assert invoker.calls == 1


def test_missing_usage_blocks_following_calls():
    invoker = FakeDirect()
    invoker.usage = None
    tracker = SpendTracker(1)
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)
    assert tracker.blocked_reason


@pytest.mark.parametrize("error", [TimeoutError("synthetic timeout"), RuntimeError("Throttled: rate exceeded"),
                                   RuntimeError("Service unavailable"), ConnectionError("DNS failed")])
def test_transport_failure_reserves_worst_case_and_keeps_original_error(error):
    """No response, no usage: charge the worst case for the attempt (input chars/3
    + the adapter's max_tokens of output at list price), keep the error type so
    callers' bounded retry/skip paths work, and do NOT block the tracker."""
    invoker = FakeDirect(error=error)
    invoker._max_tokens = 1000
    tracker = SpendTracker(1)
    with pytest.raises(type(error)) as raised:
        invoke(invoker, tracker)
    expected = reservation_usd(MODEL, len("synthetic system") + len("synthetic input"), 1000)
    assert expected == pytest.approx((math.ceil(31 / 3) * 0.33 + 1000 * 2.75) / 1e6)
    assert raised.value.spend_reserved_usd == pytest.approx(expected)
    assert tracker.total_usd == pytest.approx(expected)
    assert tracker.reserved_usd == pytest.approx(expected)
    assert tracker.calls == tracker.reserved_calls == 1
    assert tracker.blocked_reason is None
    # A retry is allowed and is charged again: bounded by money, not blocked.
    with pytest.raises(type(error)):
        invoke(invoker, tracker)
    assert invoker.calls == 2
    assert tracker.total_usd == pytest.approx(2 * expected)


def test_reservation_uses_the_stage_estimate_when_larger():
    invoker = FakeDirect(error=TimeoutError("synthetic timeout"))
    invoker._max_tokens = 10
    tracker = SpendTracker(1)
    with pytest.raises(TimeoutError):
        invoke_metered(invoker, SPEC, tracker, "s", "u", reserve_floor_usd=0.30)
    assert tracker.total_usd == pytest.approx(0.30)
    assert invoker.metrics  # the adapter still wrote its metrics line for the failed call


def test_reservation_defaults_to_32k_output_when_adapter_has_no_max_tokens():
    invoker = Mock()
    invoker.model, invoker.BACKEND_NAME = MODEL, "bedrock"
    invoker.invoke.side_effect = TimeoutError("cli hung")
    del invoker.max_tokens, invoker._max_tokens
    tracker = SpendTracker(1)
    with pytest.raises(TimeoutError):
        invoke(invoker, tracker)
    assert tracker.total_usd == pytest.approx(reservation_usd(MODEL, 31, DEFAULT_RESERVED_OUTPUT_TOKENS))
    assert tracker.total_usd > 32768 * 2.75 / 1e6


def test_reservation_that_crosses_the_cap_raises_spend_cap_exceeded():
    invoker = FakeDirect(error=RuntimeError("Throttled"))
    tracker = SpendTracker(0.0001)
    with pytest.raises(SpendCapExceeded) as raised:
        invoke(invoker, tracker)
    assert isinstance(raised.value.transport_error, RuntimeError)
    assert raised.value.__cause__ is raised.value.transport_error
    assert tracker.total_usd > tracker.cap_usd
    # Cap exhausted: ensure_available stops the next attempt before the provider.
    with pytest.raises(SpendCapExceeded):
        invoke(invoker, tracker)
    assert invoker.calls == 1


def test_routing_guarantee_failure_without_response_still_blocks():
    invoker = FakeDirect(error=CorpusRoutingError("Requested corpus model was not honored"))
    tracker = SpendTracker(1)
    with pytest.raises(CorpusRoutingError):
        invoke(invoker, tracker)
    assert tracker.blocked_reason and "guarantee" in tracker.blocked_reason
    assert tracker.reserved_calls == 0
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)
    assert invoker.calls == 1


def test_routing_guarantee_failure_with_response_is_billed_and_blocks():
    err = CorpusRoutingError("Requested corpus model was not honored")
    err.model_response_received, err.usage, err.usage_source = True, USAGE, "real"
    invoker = FakeDirect(error=err)
    tracker = SpendTracker(1)
    with pytest.raises(CorpusRoutingError):
        invoke(invoker, tracker)
    assert tracker.blocked_reason and tracker.calls == 1 and tracker.total_usd > 0
    assert tracker.reserved_calls == 0
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)


def test_one_call_can_cross_cap_but_no_next_call_runs():
    invoker = FakeDirect()
    tracker = SpendTracker(0.0001)
    with pytest.raises(SpendCapExceeded):
        invoke(invoker, tracker)
    with pytest.raises(SpendCapExceeded):
        invoke(invoker, tracker)
    assert tracker.total_usd > tracker.cap_usd
    assert invoker.calls == tracker.calls == 1


def test_kill_switch_blocks_before_provider(monkeypatch):
    invoker = FakeDirect()
    monkeypatch.setenv("SECOND_BRAIN_KILL_SWITCH", "1")
    with pytest.raises(SpendCapExceeded):
        invoke(invoker, SpendTracker(1))
    assert invoker.calls == 0


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "bad"])
def test_invalid_environment_cap_falls_back(monkeypatch, value):
    monkeypatch.setenv("SYNTHETIC_CAP", value)
    assert cap_from_env("SYNTHETIC_CAP", 0.25) == 0.25


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, True])
def test_invalid_direct_cap_rejected(value):
    with pytest.raises(ValueError):
        SpendTracker(value)


def test_adapter_cannot_differ_from_safe_spec():
    invoker = FakeDirect()
    invoker.model = "claude-fable-5-1"
    with pytest.raises(ValueError):
        invoke(invoker, SpendTracker(1))
    assert invoker.calls == 0


def test_legacy_direct_invocation_does_not_double_bill():
    # Original Dream Cycle still owns its tracker.record path.
    invoker = FakeDirect()
    tracker = SpendTracker(1)
    result = invoker.invoke("synthetic", "synthetic", stage=EDITOR_ROLE)
    assert tracker.calls == 0
    tracker.record(SPEC.model, result["usage"])
    assert tracker.calls == 1


def test_estimated_usage_cannot_bypass_spend_guard():
    invoker = Mock()
    invoker.invoke.return_value = {"output": {}, "usage": USAGE, "usage_source": "estimate"}
    tracker = SpendTracker(1)
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)
    assert tracker.blocked_reason


def test_unknown_corpus_stage_cannot_bypass_routing():
    invoker = FakeDirect()
    spec = RoleBackend("typo", "bedrock", MODEL)
    with pytest.raises(ValueError):
        invoke_metered(invoker, spec, SpendTracker(1), "synthetic", "synthetic")
    assert invoker.calls == 0


def _candidate():
    from src.models import CandidateInsight
    return CandidateInsight(title="Synthetic", type="insight", operation="CREATE", content="Synthetic insight",
                            source_memories=[], relationships=[], strategy_that_found_it="synthetic")


def _dream(invoker, cap=1):
    from src.dream_cycle.orchestrator import DreamCycleOrchestrator
    from src.backends.resolver import Resolver
    orch = DreamCycleOrchestrator()
    orch.resolver = Resolver({"skeptic": RoleBackend("skeptic", "bedrock", MODEL, "low")})
    orch._invoker_for = lambda role: invoker
    orch._spend = SpendTracker(cap)
    return orch


def test_dream_failed_output_is_billed_before_cap_prevents_retry():
    invoker = FakeDirect(raw="invalid JSON")
    orch = _dream(invoker, cap=0.0001)
    with pytest.raises(SpendCapExceeded):
        orch._invoke_evaluator_safe(_candidate(), "skeptic")
    assert invoker.calls == orch._spend.calls == 1
    assert orch._spend.total_usd > orch._spend.cap_usd


def test_dream_retries_paid_contract_failure_without_double_billing(monkeypatch):
    class Recovering(FakeDirect):
        def _extract_raw(self, response):
            return "invalid JSON" if self.calls == 1 else '{"verdict":"ACCEPT","reasoning":"synthetic"}'
    invoker = Recovering()
    orch = _dream(invoker)
    monkeypatch.setattr("src.dream_cycle.orchestrator.time.sleep", lambda *_: None)
    assert orch._invoke_evaluator_safe(_candidate(), "skeptic").verdict == "ACCEPT"
    assert invoker.calls == orch._spend.calls == 2
    assert orch._spend.total_usd == pytest.approx(0.00121)


def test_dream_unknown_usage_aborts_without_retry():
    invoker = FakeDirect()
    invoker.usage = None
    orch = _dream(invoker)
    with pytest.raises(UsageUnavailable):
        orch._invoke_evaluator_safe(_candidate(), "skeptic")
    assert invoker.calls == 1


def test_dream_evaluator_throttle_is_retried_once_reserved_and_completes(monkeypatch):
    """1071a81 semantics restored: one throttled evaluator call is retried (bounded
    by EVALUATOR_MAX_ATTEMPTS) and the verdict lands; the failed attempt is not
    free — it is reserved at the stage floor (EST_JUDGE_CANDIDATE_USD)."""
    from src.dream_cycle.orchestrator import EST_JUDGE_CANDIDATE_USD

    class ThrottledOnce(FakeDirect):
        def _invoke_provider(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Throttled: Too many requests")
            return {"stopReason": "end_turn"}
    invoker = ThrottledOnce(raw='{"verdict":"ACCEPT","reasoning":"synthetic"}')
    orch = _dream(invoker, cap=5)
    monkeypatch.setattr("src.dream_cycle.orchestrator.time.sleep", lambda *_: None)
    verdict = orch._invoke_evaluator_safe(_candidate(), "skeptic")
    assert verdict.verdict == "ACCEPT"
    assert invoker.calls == 2
    assert orch._spend.reserved_calls == 1
    assert orch._spend.reserved_usd == pytest.approx(EST_JUDGE_CANDIDATE_USD)
    assert orch._spend.total_usd == pytest.approx(EST_JUDGE_CANDIDATE_USD + 0.000605)
    assert orch._spend.blocked_reason is None


def test_dream_transport_retries_are_bounded_and_each_attempt_is_reserved(monkeypatch):
    from src.dream_cycle.orchestrator import EVALUATOR_MAX_ATTEMPTS
    invoker = FakeDirect(error=TimeoutError("synthetic transport timeout"))
    orch = _dream(invoker, cap=5)
    monkeypatch.setattr("src.dream_cycle.orchestrator.time.sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="unrecoverable"):
        orch._invoke_evaluator_safe(_candidate(), "skeptic")
    assert invoker.calls == EVALUATOR_MAX_ATTEMPTS
    assert orch._spend.reserved_calls == EVALUATOR_MAX_ATTEMPTS
    assert orch._spend.total_usd == pytest.approx(EVALUATOR_MAX_ATTEMPTS * 0.70)


def test_dream_transport_reservation_crossing_the_cap_stops_retries(monkeypatch):
    invoker = FakeDirect(error=TimeoutError("synthetic transport timeout"))
    orch = _dream(invoker, cap=1)  # floor 0.70 per attempt: the 2nd reservation crosses
    monkeypatch.setattr("src.dream_cycle.orchestrator.time.sleep", lambda *_: None)
    with pytest.raises(SpendCapExceeded):
        orch._invoke_evaluator_safe(_candidate(), "skeptic")
    assert invoker.calls == 2
    assert orch._spend.total_usd > orch._spend.cap_usd


def test_dream_routing_failure_is_not_retried():
    invoker = FakeDirect(error=CorpusRoutingError("Requested corpus model was not honored"))
    orch = _dream(invoker)
    with pytest.raises(UsageUnavailable):
        orch._invoke_evaluator_safe(_candidate(), "skeptic")
    assert invoker.calls == 1


def test_cli_paid_invalid_output_preserves_usage(monkeypatch):
    import json
    from src.backends.claude_code import ClaudeCodeInvoker
    invoker = ClaudeCodeInvoker(model="global.anthropic.claude-opus-5")
    envelope = {"type": "result", "result": "invalid JSON",
                "usage": {"input_tokens": 1000, "output_tokens": 100}, "total_cost_usd": 0.02}
    monkeypatch.setattr(invoker, "_run_subprocess", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(envelope), stderr=""))
    tracker = SpendTracker(1)
    spec = RoleBackend("thinker", "claude_code", invoker.model, "high")
    with pytest.raises(ValueError) as exc:
        invoke_metered(invoker, spec, tracker, "synthetic", "synthetic")
    assert exc.value.model_response_received
    assert tracker.total_usd == 0.02
    assert tracker.calls == 1


@pytest.mark.parametrize("missing", ["inputTokens", "outputTokens"])
def test_bedrock_partial_usage_is_not_replaced_with_zero(missing):
    from src.backends.bedrock import BedrockInvoker
    client = Mock()
    usage = dict(USAGE)
    usage.pop(missing)
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": '{"ok":true}'}]}},
        "stopReason": "end_turn", "usage": usage,
    }
    invoker = BedrockInvoker(model=MODEL, client=client)
    tracker = SpendTracker(1)
    with pytest.raises(UsageUnavailable):
        invoke(invoker, tracker)
    assert tracker.blocked_reason
    assert client.converse.call_count == 1
