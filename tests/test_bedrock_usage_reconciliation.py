"""Bedrock usage reconciliation — the Astra ``inputTokens: 2`` under-count.

Recorded shape (logs/llm_metrics, 2026-09-18 .. 2026-10-05): 307 of 309
``global.openai.gpt-6-astra`` calls carried ``usage.inputTokens == 2`` for
16-27k-character prompts while ``outputTokens`` was correct (~1.5-2k). The input
side of every panel vote on that model was therefore priced at ~$0.00002 and
never counted toward the run cap. These tests use a fake Converse client with
that response shape; no AWS call is made (the client is injected).
"""

from __future__ import annotations

import json
import math

import pytest

from src.backends.bedrock import (
    BedrockInvoker,
    CONSERVATIVE_CHARS_PER_INPUT_TOKEN,
    MAX_PLAUSIBLE_CHARS_PER_INPUT_TOKEN,
    reconcile_input_usage,
)
from src.backends.cost import SpendTracker, estimate_cost_usd, invoke_metered, price_for
from src.backends.resolver import RoleBackend

ASTRA = "global.openai.gpt-6-astra"
SYSTEM = "S" * 26_849  # recorded system_prompt_chars of a live epistemologist call
USER = "U" * 175       # recorded user_message_chars
INPUT_CHARS = len(SYSTEM) + len(USER)


class _Client:
    def __init__(self, response):
        self.response, self.calls = response, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _astra_response(usage: dict, text='{"verdict":"ACCEPT","reasoning":"ok","score":80}'):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": usage,
    }


@pytest.fixture(autouse=True)
def _metrics_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.backends.direct_api.METRICS_DIR", str(tmp_path / "llm_metrics"))
    return tmp_path / "llm_metrics"


def _invoke(usage, run_id="r"):
    client = _Client(_astra_response(usage))
    inv = BedrockInvoker(model=ASTRA, client=client)
    return inv.invoke(SYSTEM, USER, stage="evaluator:epistemologist", run_id=run_id), client


# --------------------------------------------------------------------------- #
# The recorded shapes
# --------------------------------------------------------------------------- #

def test_recorded_astra_shape_with_no_cache_and_bogus_total_is_billed_conservatively(_metrics_dir):
    """inputTokens=2, cache fields 0, totalTokens consistent with the bogus 2:
    the only honest number is the prompt length -> chars/3, flagged."""
    result, _ = _invoke({"inputTokens": 2, "outputTokens": 1729, "totalTokens": 1731,
                         "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})
    usage = result["usage"]
    expected = math.ceil(INPUT_CHARS / CONSERVATIVE_CHARS_PER_INPUT_TOKEN)
    assert usage["inputTokens"] == expected == 9008
    assert usage["inputTokensEstimated"] is True
    assert usage["inputTokensSource"] == "chars/3"
    assert usage["reportedInputTokens"] == 2
    assert usage["outputTokens"] == 1729
    assert result["usage_source"] == "real"  # charged against the cap, not fail-closed
    cost = estimate_cost_usd(ASTRA, usage)
    assert cost == pytest.approx((9008 * 10.0 + 1729 * 50.0) / 1e6)
    assert cost > 0.09  # vs $0.0865 with the bogus count: ~$0.09 of input recovered
    rec = json.loads((_metrics_dir / "r.jsonl").read_text().splitlines()[-1])
    assert rec["real_input_tokens"] == 9008
    assert rec["reported_input_tokens"] == 2
    assert rec["input_tokens_estimated"] is True
    assert rec["input_tokens_source"] == "chars/3"
    assert rec["real_cache_read_tokens"] == 0 and rec["real_total_tokens"] == 1731


def test_recorded_astra_shape_with_total_tokens_carrying_the_input():
    """inputTokens=2 but totalTokens tells the truth: bill totalTokens - outputTokens."""
    result, _ = _invoke({"inputTokens": 2, "outputTokens": 1729, "totalTokens": 7900 + 1729,
                         "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})
    usage = result["usage"]
    assert usage["inputTokens"] == 7900
    assert usage["inputTokensSource"] == "totalTokens"
    assert usage["reportedInputTokens"] == 2
    assert "inputTokensEstimated" not in usage  # plausible once reconciled
    assert estimate_cost_usd(ASTRA, usage) == pytest.approx((7900 * 10.0 + 1729 * 50.0) / 1e6)


def test_recorded_astra_shape_with_cache_reads_is_plausible_and_priced_as_cache():
    """inputTokens=2 with ~8k cacheReadInputTokens (the shape test_bedrock.py
    already pins): plausible for the prompt length, so the vendor's cache-read
    rate applies and nothing is replaced."""
    result, _ = _invoke({"inputTokens": 2, "outputTokens": 1729, "totalTokens": 1731,
                         "cacheReadInputTokens": 8100, "cacheWriteInputTokens": 300})
    usage = result["usage"]
    assert usage["inputTokens"] == 2 and "inputTokensEstimated" not in usage
    price = price_for(ASTRA)
    assert estimate_cost_usd(ASTRA, usage) == pytest.approx(
        (2 * 10.0 + 1729 * 50.0 + 8100 * price.cache_read_per_m + 300 * price.cache_write_per_m) / 1e6)


def test_consistent_plausible_usage_is_left_exactly_as_reported():
    result, _ = _invoke({"inputTokens": 7000, "outputTokens": 1729, "totalTokens": 8729,
                         "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})
    assert result["usage"] == {"inputTokens": 7000, "outputTokens": 1729, "totalTokens": 8729,
                               "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}


def test_implausible_input_is_charged_against_the_cap_through_invoke_metered():
    """End to end: the conservative figure lands on the SpendTracker (the cap),
    rather than the bogus ~$0.00002 of input."""
    client = _Client(_astra_response({"inputTokens": 2, "outputTokens": 1729, "totalTokens": 1731}))
    inv = BedrockInvoker(model=ASTRA, client=client)
    spec = RoleBackend("epistemologist", "bedrock", ASTRA, "high")
    tracker = SpendTracker(9.0)
    invoke_metered(inv, spec, tracker, SYSTEM, USER)
    assert tracker.calls == 1 and tracker.blocked_reason is None
    assert tracker.total_usd == pytest.approx((9008 * 10.0 + 1729 * 50.0) / 1e6)
    assert tracker.total_usd > (2 * 10.0 + 1729 * 50.0) / 1e6


def test_implausible_input_still_raises_cap_when_the_corrected_cost_crosses_it():
    from src.backends.cost import SpendCapExceeded
    client = _Client(_astra_response({"inputTokens": 2, "outputTokens": 1729, "totalTokens": 1731}))
    inv = BedrockInvoker(model=ASTRA, client=client)
    spec = RoleBackend("epistemologist", "bedrock", ASTRA, "high")
    tracker = SpendTracker(0.09)  # the bogus count would cost $0.0865 and pass
    with pytest.raises(SpendCapExceeded):
        invoke_metered(inv, spec, tracker, SYSTEM, USER)


# --------------------------------------------------------------------------- #
# Pure function edges
# --------------------------------------------------------------------------- #

def test_reconcile_never_lowers_a_reported_count():
    usage = {"inputTokens": 50_000, "outputTokens": 10, "totalTokens": 20}  # total smaller than input
    assert reconcile_input_usage(dict(usage), 100)["inputTokens"] == 50_000


def test_reconcile_threshold_is_exactly_chars_over_twelve():
    chars = 12_000
    boundary = chars // MAX_PLAUSIBLE_CHARS_PER_INPUT_TOKEN  # 1000 tokens: plausible
    ok = reconcile_input_usage({"inputTokens": boundary, "outputTokens": 1, "totalTokens": boundary + 1}, chars)
    assert ok["inputTokens"] == boundary and "inputTokensEstimated" not in ok
    bad = reconcile_input_usage({"inputTokens": boundary - 1, "outputTokens": 1, "totalTokens": boundary}, chars)
    assert bad["inputTokensEstimated"] is True and bad["inputTokens"] == 4000


def test_cache_fields_count_toward_plausibility():
    usage = {"inputTokens": 2, "outputTokens": 5, "totalTokens": 7, "cacheReadInputTokens": 3000}
    out = reconcile_input_usage(dict(usage), 12_000)
    assert out["inputTokens"] == 2 and "inputTokensEstimated" not in out


def test_incomplete_usage_is_left_for_the_fail_closed_guard():
    """Missing outputTokens must still be rejected by cost._require_usage, not papered over."""
    out = reconcile_input_usage({"inputTokens": 2, "totalTokens": 1731}, 27_000)
    assert out == {"inputTokens": 2, "totalTokens": 1731}


def test_no_input_chars_means_no_plausibility_check_but_total_reconciliation_still_applies():
    out = reconcile_input_usage({"inputTokens": 2, "outputTokens": 10, "totalTokens": 112}, None)
    assert out["inputTokens"] == 102 and out["inputTokensSource"] == "totalTokens"
    out2 = reconcile_input_usage({"inputTokens": 2, "outputTokens": 10, "totalTokens": 12}, None)
    assert out2["inputTokens"] == 2 and "inputTokensEstimated" not in out2
