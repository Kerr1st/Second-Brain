"""Per-run spend accounting and guardrails for metered model backends.

On direct Bedrock (and Claude Code on Bedrock) every dream-cycle call is
metered, so the orchestrator prices each real ``usage`` block and accumulates a
per-run total. Two controls, both env-driven:

* ``SECOND_BRAIN_RUN_COST_CAP_USD`` (default :data:`DEFAULT_RUN_COST_CAP_USD`):
  when the running total exceeds the cap, :class:`SpendCapExceeded` is raised
  and the run aborts. It deliberately does NOT subclass ``RuntimeError`` /
  ``ValueError`` / ``TimeoutError`` so the evaluator retry loop (which retries
  those) cannot swallow it and spend more.
* ``SECOND_BRAIN_KILL_SWITCH``: when set truthy, a run is refused before any
  LLM call is made.

Prices are list prices per 1M tokens (USD), input / output, as published on
2026-09-14 (Anthropic models overview; OpenAI GPT-6 Astra model page — Bedrock
bills OpenAI models at OpenAI-direct rates). Thinking / reasoning tokens are
billed as output by both vendors and are already included in the provider's
``outputTokens``. Cache-read discounts are ignored (conservative). A model that
is not in the table is priced at the table ceiling so the cap trips early
rather than late.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_RUN_COST_CAP_USD = 5.0

# Ordered (substring, input $/M, output $/M). First match wins, so more specific
# substrings must precede more general ones (e.g. "fable-5-1" before "fable-5").
_PRICE_TABLE: tuple[tuple[str, float, float], ...] = (
    # Anthropic (Bedrock inference-profile ids: {us,global}.anthropic.claude-...)
    ("claude-fable-5", 10.0, 50.0),      # Fable 5 and 5.1 share list prices
    ("claude-opus-5", 5.0, 25.0),
    ("claude-sonnet-5", 2.0, 10.0),
    ("claude-haiku-4-5", 1.0, 5.0),
    ("claude-haiku-4.5", 1.0, 5.0),
    ("claude-opus-4", 5.0, 25.0),        # 4.x generation (Kiro/Claude Code ids use dotted forms)
    ("claude-sonnet-4", 3.0, 15.0),
    # OpenAI on Bedrock ({us,global}.openai.gpt-...)
    ("gpt-6-astra", 10.0, 50.0),
    ("gpt-5.6-sol", 4.0, 20.0),          # derived: Astra is 2.5x Sol's rate
)

# Conservative ceiling for unknown models (= the most expensive known tier).
_CEILING_IN, _CEILING_OUT = 10.0, 50.0


@dataclass(frozen=True)
class ModelPrice:
    input_per_m: float
    output_per_m: float
    known: bool

    @property
    def cache_read_per_m(self) -> float:
        """Cached-input read price. OpenAI on Bedrock: 10% of input (Astra $1/M).
        Anthropic 5-series: Fable 5.1 lists $0.25/M reads (2.5% of input); the
        other Claude models are 10%. Apply the vendor rule to the input price."""
        return self.input_per_m * self._cache_read_fraction

    @property
    def cache_write_per_m(self) -> float:
        """Cache-write price: 1.25x the uncached input rate (both vendors)."""
        return self.input_per_m * 1.25

    _cache_read_fraction: float = 0.10


def price_for(model: str) -> ModelPrice:
    """Look up the list price for a model id (inference-profile or bare id)."""
    m = (model or "").lower()
    for needle, inp, out in _PRICE_TABLE:
        if needle in m:
            # Fable 5.1 cut cache reads to $0.25/M (2.5% of its $10 input rate).
            frac = 0.025 if "fable-5-1" in m else 0.10
            return ModelPrice(inp, out, True, frac)
    return ModelPrice(_CEILING_IN, _CEILING_OUT, False, 0.10)


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _tokens(usage: dict, *keys: str) -> int | None:
    """First present integer among alternative key spellings (camel/snake)."""
    for k in keys:
        v = _int_or_none(usage.get(k))
        if v is not None:
            return v
    return None


def estimate_cost_usd(model: str, usage: dict | None) -> float:
    """Price a provider ``usage`` block. Missing/unparseable usage costs 0.

    Accepts Bedrock Converse keys (``inputTokens``, ``cacheReadInputTokens``…)
    and provider-envelope snake_case keys (``input_tokens``, ``cache_read_input_tokens``…).
    If the provider reported a ``total_cost_usd`` for the call (Claude Code does,
    covering every turn of its tool loop), that figure wins over any estimate.
    """
    if not isinstance(usage, dict):
        return 0.0
    reported = usage.get("total_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported >= 0:
        return float(reported)
    inp = _tokens(usage, "inputTokens", "input_tokens")
    out = _tokens(usage, "outputTokens", "output_tokens")
    cache_read = _tokens(usage, "cacheReadInputTokens", "cache_read_input_tokens") or 0
    cache_write = _tokens(usage, "cacheWriteInputTokens", "cache_creation_input_tokens") or 0
    if inp is None and out is None and not cache_read and not cache_write:
        return 0.0
    price = price_for(model)
    return (
        (inp or 0) * price.input_per_m
        + (out or 0) * price.output_per_m
        + cache_read * price.cache_read_per_m
        + cache_write * price.cache_write_per_m
    ) / 1_000_000


class SpendCapExceeded(Exception):
    """Per-run spend cap breached. Intentionally NOT a RuntimeError subclass."""


class SpendTracker:
    """Accumulates estimated spend for one run and enforces a hard cap."""

    def __init__(self, cap_usd: float = DEFAULT_RUN_COST_CAP_USD):
        self.cap_usd = float(cap_usd)
        self.total_usd = 0.0
        self.calls = 0

    @classmethod
    def from_env(cls) -> "SpendTracker":
        raw = os.environ.get("SECOND_BRAIN_RUN_COST_CAP_USD")
        cap = DEFAULT_RUN_COST_CAP_USD
        if raw:
            try:
                parsed = float(raw)
                if parsed > 0:
                    cap = parsed
                else:
                    logger.warning("SECOND_BRAIN_RUN_COST_CAP_USD=%r must be > 0; using default $%.2f", raw, cap)
            except ValueError:
                logger.warning("SECOND_BRAIN_RUN_COST_CAP_USD=%r is not a number; using default $%.2f", raw, cap)
        return cls(cap_usd=cap)

    def record(self, model: str, usage: dict | None) -> float:
        """Price one call, add it to the total, and enforce the cap.

        Returns the cost of this call. Backends that report no real usage
        (Kiro) contribute nothing. Raises :class:`SpendCapExceeded` once the
        running total exceeds the cap — after counting the offending call, so
        the abort message reflects what was actually spent.
        """
        cost = estimate_cost_usd(model, usage)
        if cost == 0.0:
            return 0.0
        self.calls += 1
        self.total_usd += cost
        price = price_for(model)
        if not price.known:
            logger.warning("No list price for model %r; billed at ceiling $%.0f/$%.0f per 1M.",
                           model, price.input_per_m, price.output_per_m)
        if self.total_usd > self.cap_usd:
            raise SpendCapExceeded(
                f"Per-run spend cap exceeded: ${self.total_usd:.2f} > ${self.cap_usd:.2f} "
                f"after {self.calls} metered call(s); last call {model} cost ${cost:.2f}. "
                f"Raise SECOND_BRAIN_RUN_COST_CAP_USD to allow more."
            )
        return cost


def kill_switch_engaged() -> bool:
    """True when ``SECOND_BRAIN_KILL_SWITCH`` is set to a truthy value."""
    return os.environ.get("SECOND_BRAIN_KILL_SWITCH", "").strip().lower() in {"1", "true", "yes", "on"}
