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

Service tiers (skim stage, 2026-09-20): Bedrock's Flex tier bills the skim
models at half the standard rate. Standard prices live in :data:`_PRICE_TABLE`;
Flex prices in :data:`_FLEX_PRICE_TABLE`. A call is priced at the Flex rate only
when its ``usage`` block says the request was actually served on Flex
(``serviceTier: "flex"``, copied by the Bedrock adapter from the Converse
response's ``serviceTier.type``) — never from what was merely requested.

Transport failures (2026-10-05 policy): a call that fails before any response is
received — throttle, timeout, DNS/connection error, 5xx — has no usage block,
so its true cost is unknown (it may have been zero or it may have been billed).
Rather than blocking the whole run on the first such blip, the tracker charges a
*conservative reservation* for the failed attempt: the worst-case price of the
request's input (``chars / RESERVE_CHARS_PER_TOKEN``) plus the adapter's
``max_tokens`` of output, or the caller's per-stage estimate if larger. The
reservation counts toward the cap exactly like real spend, so bounded retries
stay bounded by money, and crossing the cap raises :class:`SpendCapExceeded`.
Only routing-guarantee failures (:class:`~src.backends.base.CorpusRoutingError`,
model-not-honoured) and unmeterable *successful* responses still block the
tracker permanently.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_RUN_COST_CAP_USD = 5.0

#: Reservation for a failed call: assume ~3 chars per input token (real ratios
#: for English prose and JSON are 3.5-4.5, so this over-counts) …
RESERVE_CHARS_PER_TOKEN = 3
#: … and the adapter's full output budget. Mirrors ``bedrock.DEFAULT_MAX_TOKENS``;
#: used only when the invoker does not expose its own ``max_tokens``.
DEFAULT_RESERVED_OUTPUT_TOKENS = 32768

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
    # Skim-stage models (docs/PLAN-skim-stage.md). Standard tier, us-east-1,
    # read from the Bedrock pricing page's data feed on 2026-09-20
    # (https://aws.amazon.com/bedrock/pricing/ , Amazon Nova tab "Geo
    # Cross-region inference and in-region" and the Google "On-Demand pricing"
    # table for US East/West; price data published 2026-09-17). The `us.`
    # geo profile is what the skim calls, so the geo/in-region rate applies —
    # the `global.` profile is cheaper ($0.30 / $2.50) but leaves the US.
    ("nova-2-lite", 0.33, 2.75),         # us.amazon.nova-2-lite-v1:0
    ("gemma-3-12b", 0.09, 0.29),         # google.gemma-3-12b-it
)

# Flex service tier (https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ServiceTier.html).
# Nova 2 Lite: listed explicitly on the pricing page ("Flex Tier", geo/in-region).
# Gemma 3 12B: the page prints no Flex row and states "Flex tier pricing is at
# 50% discount to Standard tier pricing", so the figure is derived.
_FLEX_PRICE_TABLE: tuple[tuple[str, float, float], ...] = (
    ("nova-2-lite", 0.165, 1.375),
    ("gemma-3-12b", 0.045, 0.145),
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


def price_for(model: str, service_tier: str | None = None) -> ModelPrice:
    """Look up the list price for a model id (inference-profile or bare id).

    ``service_tier`` is the tier the call was *served* on (Converse response
    ``serviceTier.type``). Only ``"flex"`` changes the price, and only for
    models with a Flex entry; anything else falls back to the standard rate.
    """
    m = (model or "").lower()
    tier = (service_tier or "").lower()
    for needle, inp, out in _PRICE_TABLE:
        if needle in m:
            # Fable 5.1 cut cache reads to $0.25/M (2.5% of its $10 input rate).
            # Amazon Nova lists cache reads at 75% less than input (25%).
            frac = 0.025 if "fable-5-1" in m else (0.25 if "nova" in m else 0.10)
            if tier == "flex":
                for fneedle, finp, fout in _FLEX_PRICE_TABLE:
                    if fneedle in m:
                        return ModelPrice(finp, fout, True, frac)
            return ModelPrice(inp, out, True, frac)
    return ModelPrice(_CEILING_IN, _CEILING_OUT, False, 0.10)


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported) and reported >= 0:
        return float(reported)
    inp = _tokens(usage, "inputTokens", "input_tokens")
    out = _tokens(usage, "outputTokens", "output_tokens")
    cache_read = _tokens(usage, "cacheReadInputTokens", "cache_read_input_tokens") or 0
    cache_write = _tokens(usage, "cacheWriteInputTokens", "cache_creation_input_tokens") or 0
    if inp is None and out is None and not cache_read and not cache_write:
        return 0.0
    tier = usage.get("serviceTier", usage.get("service_tier"))
    price = price_for(model, tier if isinstance(tier, str) else None)
    return (
        (inp or 0) * price.input_per_m
        + (out or 0) * price.output_per_m
        + cache_read * price.cache_read_per_m
        + cache_write * price.cache_write_per_m
    ) / 1_000_000


class SpendCapExceeded(Exception):
    """Per-run spend cap breached. Intentionally NOT a RuntimeError subclass."""


class UsageUnavailable(SpendCapExceeded):
    """A metered call cannot be priced; no more calls may run on this budget."""


def cap_from_env(name: str, default: float) -> float:
    """Read a finite positive dollar limit; malformed values use the safe default."""
    if not math.isfinite(default) or default <= 0:
        raise ValueError("Default spend cap must be finite and positive")
    raw = os.environ.get(name)
    if not raw:
        return float(default)
    try:
        value = float(raw)
        if math.isfinite(value) and value > 0:
            return value
    except (TypeError, ValueError):
        pass
    logger.warning("%s must be finite and > 0; using default $%.2f", name, default)
    return float(default)


def _require_usage(usage) -> None:
    if not isinstance(usage, dict):
        raise UsageUnavailable("Metered response has no usable usage; stopping this run")
    reported = usage.get("total_cost_usd")
    if reported is not None:
        if isinstance(reported, bool) or not isinstance(reported, (int, float)) or not math.isfinite(reported) or reported < 0:
            raise UsageUnavailable("Invalid reported model cost; stopping this run")
    names = ("inputTokens", "input_tokens", "outputTokens", "output_tokens",
             "cacheReadInputTokens", "cache_read_input_tokens",
             "cacheWriteInputTokens", "cache_creation_input_tokens")
    for name in names:
        if name in usage and _int_or_none(usage[name]) is None:
            raise UsageUnavailable("Invalid model token usage; stopping this run")
    if reported is None and (
        _tokens(usage, "inputTokens", "input_tokens") is None
        or _tokens(usage, "outputTokens", "output_tokens") is None
    ):
        raise UsageUnavailable("Incomplete model token usage; stopping this run")


def reservation_usd(model: str, input_chars: int, max_output_tokens: int | None = None,
                    floor_usd: float = 0.0) -> float:
    """Worst-case price of a request whose outcome is unknown (no usage returned).

    ``input_chars / RESERVE_CHARS_PER_TOKEN`` input tokens plus ``max_output_tokens``
    (default :data:`DEFAULT_RESERVED_OUTPUT_TOKENS`) of output at the model's
    standard list price, never below ``floor_usd`` (a caller's per-stage estimate).
    """
    out = max_output_tokens if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) \
        and max_output_tokens > 0 else DEFAULT_RESERVED_OUTPUT_TOKENS
    chars = max(int(input_chars or 0), 0)
    inp = math.ceil(chars / RESERVE_CHARS_PER_TOKEN)
    price = price_for(model)
    cost = (inp * price.input_per_m + out * price.output_per_m) / 1_000_000
    floor = float(floor_usd) if isinstance(floor_usd, (int, float)) and not isinstance(floor_usd, bool) \
        and math.isfinite(floor_usd) and floor_usd > 0 else 0.0
    return max(cost, floor)


def _max_output_tokens_for(invoker, kwargs: dict) -> int | None:
    """The output budget a failed request could have consumed, if knowable."""
    for candidate in (kwargs.get("max_tokens"), getattr(invoker, "max_tokens", None),
                      getattr(invoker, "_max_tokens", None)):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    return None


class SpendTracker:
    """Accumulate reported usage; stop after a response crosses the run cap.

    This cannot bound an already in-flight call. Standalone corpus stages use
    record_metered and ensure_available to stop after accounting gaps too.
    ``reserved_usd``/``reserved_calls`` separate conservative reservations for
    failed transport attempts from priced real usage (both are in ``total_usd``).
    """

    def __init__(self, cap_usd: float = DEFAULT_RUN_COST_CAP_USD):
        self.cap_usd = float(cap_usd)
        if isinstance(cap_usd, bool) or not math.isfinite(self.cap_usd) or self.cap_usd <= 0:
            raise ValueError("Spend cap must be finite and positive")
        self.blocked_reason = None
        self.total_usd = 0.0
        self.calls = 0
        self.reserved_usd = 0.0
        self.reserved_calls = 0

    @classmethod
    def from_env(cls) -> "SpendTracker":
        return cls(cap_from_env("SECOND_BRAIN_RUN_COST_CAP_USD", DEFAULT_RUN_COST_CAP_USD))

    def ensure_available(self) -> None:
        """Stop before another call after a cap, kill switch or accounting gap."""
        if kill_switch_engaged():
            raise SpendCapExceeded("Second Brain model kill switch is engaged")
        if self.blocked_reason:
            raise UsageUnavailable(self.blocked_reason)
        if self.total_usd >= self.cap_usd:
            raise SpendCapExceeded("Per-run spend cap is exhausted")

    def record_metered(self, model: str, usage, *, usage_source=None) -> float:
        """Strict accounting for corpus stages. Unknown spend blocks this run."""
        try:
            if usage_source == "estimate":
                raise UsageUnavailable("Estimated usage cannot establish metered spend")
            _require_usage(usage)
        except UsageUnavailable as exc:
            self.blocked_reason = str(exc)
            raise
        cost = self.record(model, usage)
        if cost == 0:
            self.calls += 1
        return cost

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

    def reserve(self, model: str, usd: float, reason: str = "") -> float:
        """Charge a conservative reservation for a call whose spend is unknown.

        Used for transport/throttle/timeout failures that returned no usage. The
        amount counts toward the cap like real spend (so retries are bounded by
        money, not by hope) but does not block the tracker. Raises
        :class:`SpendCapExceeded` when the reservation crosses the cap.
        """
        amount = float(usd)
        if isinstance(usd, bool) or not math.isfinite(amount) or amount < 0:
            raise ValueError("Reservation must be a finite non-negative amount")
        self.calls += 1
        self.reserved_calls += 1
        self.reserved_usd += amount
        self.total_usd += amount
        logger.warning("Reserved $%.4f for a failed %s call with no usage (%s); run total $%.4f of $%.2f",
                       amount, model, reason or "unknown error", self.total_usd, self.cap_usd)
        if self.total_usd > self.cap_usd:
            raise SpendCapExceeded(
                f"Per-run spend cap exceeded: ${self.total_usd:.2f} > ${self.cap_usd:.2f} "
                f"after {self.calls} metered call(s) including {self.reserved_calls} reservation(s) "
                f"totalling ${self.reserved_usd:.2f}; last failed {model} call reserved ${amount:.2f}."
            )
        return amount


def kill_switch_engaged() -> bool:
    """True when ``SECOND_BRAIN_KILL_SWITCH`` is set to a truthy value."""
    return os.environ.get("SECOND_BRAIN_KILL_SWITCH", "").strip().lower() in {"1", "true", "yes", "on"}


def invoke_metered(invoker, spec, tracker: SpendTracker, system_prompt: str,
                   user_message: str, *, reserve_floor_usd: float = 0.0, **kwargs):
    """Invoke one corpus stage, recording returned usage exactly once.

    A response with malformed output is still billed before its original error
    propagates. A transport/throttle/timeout error (no response, no usage) is
    charged a conservative reservation (:func:`reservation_usd`, never below
    ``reserve_floor_usd``) and re-raised unchanged so the caller's existing,
    bounded retry/skip logic applies; it does not block the tracker. Routing
    guarantee failures (``CorpusRoutingError``) block the tracker permanently.
    A successful response without finite, complete usage also blocks it.

    This is a stop-after-call cap, not a guaranteed monetary ceiling: a single
    in-flight request can cross it. Legacy Dream Cycle callers retain their
    existing independent accounting.
    """
    from src.backends.base import CORPUS_ROLES, CorpusRoutingError, assert_corpus_route, assert_backend_supports_role
    if spec.role not in CORPUS_ROLES:
        raise ValueError("Unknown corpus stage")
    assert_corpus_route(spec.backend, spec.model, spec.role)
    assert_backend_supports_role(spec.backend, spec.role)
    actual_model = getattr(invoker, "model", None)
    actual_backend = getattr(invoker, "BACKEND_NAME", None)
    if isinstance(actual_model, str) and actual_model != spec.model:
        raise ValueError("Invoker model does not match resolved corpus role")
    if isinstance(actual_backend, str) and actual_backend != spec.backend:
        raise ValueError("Invoker backend does not match resolved corpus role")
    tracker.ensure_available()
    kwargs.setdefault("tools", False)
    kwargs.setdefault("effort", spec.effort)
    kwargs.setdefault("stage", spec.role)
    try:
        result = invoker.invoke(system_prompt, user_message, **kwargs)
    except Exception as exc:
        if getattr(exc, "model_response_received", False):
            # Response-bearing failure (refusal, bad JSON, truncation, model not
            # honoured): bill the real usage, then let the original error propagate.
            try:
                tracker.record_metered(spec.model, getattr(exc, "usage", None),
                                       usage_source=getattr(exc, "usage_source", None))
            finally:
                if isinstance(exc, CorpusRoutingError):
                    tracker.blocked_reason = "Corpus model guarantee failed; no further calls permitted"
            raise
        if isinstance(exc, CorpusRoutingError):
            tracker.blocked_reason = "Corpus model guarantee failed; no further calls permitted"
            raise
        # No response, no usage: the request may or may not have reached the
        # provider. Charge the worst case for this attempt instead of claiming
        # zero dollars, keep the original error type so bounded retry loops
        # (evaluator) and skip paths (Explorer, Thinker, skim) behave as before.
        reserved = reservation_usd(
            spec.model, len(system_prompt) + len(user_message),
            _max_output_tokens_for(invoker, kwargs), reserve_floor_usd,
        )
        exc.spend_reserved_usd = reserved
        try:
            tracker.reserve(spec.model, reserved, reason=f"{type(exc).__name__}: {exc}")
        except SpendCapExceeded as cap:
            cap.transport_error = exc
            raise cap from exc
        raise
    if not isinstance(result, dict):
        tracker.blocked_reason = "Invalid metered response; spend is unknown"
        raise UsageUnavailable(tracker.blocked_reason)
    try:
        tracker.record_metered(spec.model, result.get("usage"), usage_source=result.get("usage_source"))
    except SpendCapExceeded as exc:
        exc.invocation_result = result
        raise
    return result
