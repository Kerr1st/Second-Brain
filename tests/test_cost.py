"""Tests for src/backends/cost.py — per-run spend accounting and guardrails.

The dream cycle on direct Bedrock is fully metered, so every real ``usage``
block is priced and accumulated; exceeding the per-run cap aborts the run
(hard stop, no fabricated verdicts). A kill switch env var stops a run before
any LLM call.
"""

from __future__ import annotations

import pytest

from src.backends.cost import (
    DEFAULT_RUN_COST_CAP_USD,
    SpendCapExceeded,
    SpendTracker,
    estimate_cost_usd,
    kill_switch_engaged,
    price_for,
)


def _usage(inp, out):
    return {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out}


class TestPriceTable:
    @pytest.mark.parametrize(
        "model, per_m_in, per_m_out",
        [
            ("global.anthropic.claude-fable-5-1", 10.0, 50.0),
            ("us.anthropic.claude-fable-5", 10.0, 50.0),
            ("global.anthropic.claude-opus-5", 5.0, 25.0),
            ("global.anthropic.claude-sonnet-5", 2.0, 10.0),
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1.0, 5.0),
            ("global.anthropic.claude-opus-4-8", 5.0, 25.0),
            ("global.openai.gpt-6-astra", 10.0, 50.0),
        ],
    )
    def test_known_models(self, model, per_m_in, per_m_out):
        price = price_for(model)
        assert price.known is True
        assert (price.input_per_m, price.output_per_m) == (per_m_in, per_m_out)

    def test_unknown_model_uses_conservative_ceiling(self):
        """Never under-count: an unpriced model is billed at the table maximum
        so the cap trips early rather than late."""
        price = price_for("vendor.mystery-model-v9")
        assert price.known is False
        assert price.input_per_m == 10.0 and price.output_per_m == 50.0

    def test_estimate_cost(self):
        assert estimate_cost_usd("global.anthropic.claude-fable-5-1", _usage(1_000_000, 0)) == pytest.approx(10.0)
        assert estimate_cost_usd("global.anthropic.claude-fable-5-1", _usage(0, 1_000_000)) == pytest.approx(50.0)
        # June verified-run Thinker totals at Fable prices ≈ $0.65.
        assert estimate_cost_usd("global.anthropic.claude-fable-5-1", _usage(5_701, 11_883)) == pytest.approx(0.651, abs=0.001)

    def test_estimate_cost_without_usage_is_zero(self):
        assert estimate_cost_usd("global.anthropic.claude-opus-5", None) == 0.0
        assert estimate_cost_usd("global.anthropic.claude-opus-5", {"inputTokens": "n/a"}) == 0.0

    def test_cache_tokens_are_priced(self):
        """Bedrock auto-caches long system prompts: `inputTokens` then reports only
        the uncached remainder and the bulk lands in cacheRead/cacheWrite. Reads
        are discounted, writes cost 1.25x input; neither may be dropped."""
        usage = {"inputTokens": 2, "outputTokens": 0, "cacheReadInputTokens": 1_000_000, "cacheWriteInputTokens": 0}
        assert estimate_cost_usd("global.openai.gpt-6-astra", usage) == pytest.approx(1.0 + 2 * 10 / 1e6)
        assert estimate_cost_usd("global.anthropic.claude-fable-5-1", usage) == pytest.approx(0.25 + 2 * 10 / 1e6)
        assert estimate_cost_usd("global.anthropic.claude-sonnet-5", usage) == pytest.approx(0.20 + 2 * 2 / 1e6)
        write = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 1_000_000}
        assert estimate_cost_usd("global.openai.gpt-6-astra", write) == pytest.approx(12.5)
        assert estimate_cost_usd("global.anthropic.claude-opus-5", write) == pytest.approx(6.25)

    def test_provider_reported_total_cost_wins(self):
        """Claude Code reports `total_cost_usd` for the whole agentic turn
        (including its own context loads); that beats any token estimate."""
        usage = {"input_tokens": 60, "output_tokens": 24_334, "total_cost_usd": 1.8732}
        assert estimate_cost_usd("global.anthropic.claude-opus-5", usage) == pytest.approx(1.8732)

    def test_snake_case_usage_keys_are_understood(self):
        """Agentic-CLI adapters surface the provider envelope's snake_case keys."""
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 1_000_000}
        assert estimate_cost_usd("global.anthropic.claude-opus-5", usage) == pytest.approx(5.0 + 0.5)


class TestSpendTracker:
    def test_accumulates_and_reports(self):
        t = SpendTracker(cap_usd=10.0)
        t.record("global.anthropic.claude-sonnet-5", _usage(1_000_000, 0))  # $2
        t.record("global.openai.gpt-6-astra", _usage(0, 100_000))          # $5
        assert t.total_usd == pytest.approx(7.0)
        assert t.calls == 2

    def test_exceeding_cap_raises_hard_stop(self):
        t = SpendTracker(cap_usd=1.0)
        t.record("global.anthropic.claude-sonnet-5", _usage(100_000, 0))  # $0.20
        with pytest.raises(SpendCapExceeded) as excinfo:
            t.record("global.anthropic.claude-fable-5-1", _usage(0, 100_000))  # +$5.00
        assert "cap" in str(excinfo.value).lower()
        # The offending call is still counted so the abort message is accurate.
        assert t.total_usd == pytest.approx(5.20)

    def test_cap_exception_is_not_a_runtime_error(self):
        """The evaluator retry loop swallows RuntimeError/TimeoutError/ValueError
        and retries (spending more). A cap breach must escape it untouched."""
        assert not issubclass(SpendCapExceeded, RuntimeError)
        assert not issubclass(SpendCapExceeded, ValueError)
        assert not issubclass(SpendCapExceeded, TimeoutError)

    def test_missing_usage_is_a_no_op(self):
        """Kiro reports no real usage (usage=None); nothing to price."""
        t = SpendTracker(cap_usd=0.01)
        t.record("claude-opus-5", None)
        t.record("claude-opus-5", {"inputTokens": None, "outputTokens": None})
        assert t.total_usd == 0.0 and t.calls == 0

    def test_from_env_default_and_override(self, monkeypatch):
        monkeypatch.delenv("SECOND_BRAIN_RUN_COST_CAP_USD", raising=False)
        assert SpendTracker.from_env().cap_usd == DEFAULT_RUN_COST_CAP_USD == 5.0
        monkeypatch.setenv("SECOND_BRAIN_RUN_COST_CAP_USD", "12.5")
        assert SpendTracker.from_env().cap_usd == 12.5

    def test_from_env_rejects_garbage_and_nonpositive(self, monkeypatch):
        for bad in ("abc", "0", "-3"):
            monkeypatch.setenv("SECOND_BRAIN_RUN_COST_CAP_USD", bad)
            assert SpendTracker.from_env().cap_usd == DEFAULT_RUN_COST_CAP_USD


class TestKillSwitch:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_engaged_values(self, monkeypatch, value):
        monkeypatch.setenv("SECOND_BRAIN_KILL_SWITCH", value)
        assert kill_switch_engaged() is True

    @pytest.mark.parametrize("value", [None, "", "0", "false", "off", "no"])
    def test_disengaged_values(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("SECOND_BRAIN_KILL_SWITCH", raising=False)
        else:
            monkeypatch.setenv("SECOND_BRAIN_KILL_SWITCH", value)
        assert kill_switch_engaged() is False
