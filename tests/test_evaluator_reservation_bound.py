"""Evaluator seats carry an explicit, bounded output budget (review S3-3).

A transport failure with no usage is reserved at ``input + max_tokens`` of
output (``src/backends/cost.py``). With the adapter default of 32,768 tokens one
throttled Astra seat reserved ≈ $1.70 of the $9 run cap and the three-attempt
evaluator loop ≈ $5 on a single candidate. The committed profiles therefore pin
``max_tokens`` per evaluator seat, sized from production ``llm_metrics``
(``real_output_tokens``, thinking included, through 2026-10-05) so a real answer
is never truncated:

* Astra (``gpt-6-astra``): skeptic / epistemologist / grounding audit max 2,293 → 8,192.
* Sonnet-5: advocate / methodologist max 9,581 → 20,480.

These tests pin (a) the budgets are ≥ 2x the observed maximum and ≥ 8,192, (b) the
Bedrock adapter really sends them as ``inferenceConfig.maxTokens`` and exposes
them to the reservation, and (c) a throttled evaluator call under ``controlled``
reserves < $0.75 for a typical 20k-char prompt.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from src.backends.bedrock import BedrockInvoker
from src.backends.cost import SpendTracker, _max_output_tokens_for, invoke_metered, reservation_usd
from src.backends.resolver import Resolver, load_profiles
from src.dream_cycle.orchestrator import EST_JUDGE_CANDIDATE_USD, GROUNDING_AUDIT_ROLE

EVALUATORS = ("skeptic", "advocate", "epistemologist", "methodologist")
#: Largest real_output_tokens seen per evaluator model in production metrics
#: (logs/llm_metrics, 2026-10-05). Budgets must stay ≥ 2x these.
OBSERVED_MAX_OUTPUT = {"gpt-6-astra": 2_293, "claude-sonnet-5": 9_581}
MIN_BUDGET = 8_192
TYPICAL_PROMPT_CHARS = 20_000
PER_SEAT_RESERVATION_BOUND_USD = 0.75


def _observed_max_for(model: str) -> int:
    for needle, observed in OBSERVED_MAX_OUTPUT.items():
        if needle in model:
            return observed
    raise AssertionError(f"no production output figure for evaluator model {model!r}")


def _bedrock_evaluator_seats():
    profiles, default = load_profiles()
    assert default == "controlled"
    seats = []
    for pname, profile in profiles.items():
        for role in (*EVALUATORS, GROUNDING_AUDIT_ROLE):
            spec = profile[role]
            if spec.backend == "bedrock":
                seats.append(pytest.param(pname, spec, id=f"{pname}-{role}"))
    assert any(p.values[0] == "controlled" for p in seats)
    return seats


class _ThrottlingClient:
    """Records the Converse request and throttles every call (no usage returned)."""

    def __init__(self):
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "synthetic"}}, "Converse")


@pytest.mark.parametrize("profile_name,spec", _bedrock_evaluator_seats())
def test_every_bedrock_evaluator_seat_pins_a_budget_above_observed_output(profile_name, spec):
    budget = (spec.options or {}).get("max_tokens")
    assert isinstance(budget, int) and not isinstance(budget, bool), (profile_name, spec.role)
    assert budget >= MIN_BUDGET
    assert budget >= 2 * _observed_max_for(spec.model)
    assert budget < 32_768, "the adapter default is what S3-3 removes"


def test_grounding_audit_runs_on_a_bounded_evaluator_seat():
    # The audit has its own role, routed exactly like the (bounded) skeptic seat.
    profiles, _ = load_profiles()
    for profile in profiles.values():
        assert profile[GROUNDING_AUDIT_ROLE].options == profile["skeptic"].options


@pytest.mark.parametrize("profile_name,spec", _bedrock_evaluator_seats())
def test_adapter_exposes_the_configured_budget_to_the_reservation(profile_name, spec):
    profiles, _ = load_profiles()
    invoker = Resolver(profiles[profile_name]).invoker_for(spec.role)
    assert isinstance(invoker, BedrockInvoker)
    assert _max_output_tokens_for(invoker, {}) == spec.options["max_tokens"]


def test_controlled_astra_seats_reserve_well_under_a_dollar_for_a_typical_prompt():
    profiles, _ = load_profiles()
    controlled = profiles["controlled"]
    astra_seats = [r for r in EVALUATORS if "gpt-6-astra" in controlled[r].model]
    assert set(astra_seats) == {"skeptic", "epistemologist"}
    for role in astra_seats:
        spec = controlled[role]
        reserved = reservation_usd(spec.model, TYPICAL_PROMPT_CHARS, spec.options["max_tokens"],
                                   EST_JUDGE_CANDIDATE_USD)
        assert reserved < PER_SEAT_RESERVATION_BOUND_USD, (role, reserved)
        # The old inherited default would have reserved more than twice the bound.
        assert reservation_usd(spec.model, TYPICAL_PROMPT_CHARS, 32_768, EST_JUDGE_CANDIDATE_USD) > 1.5


@pytest.mark.parametrize("role", EVALUATORS)
def test_throttled_controlled_evaluator_reserves_a_bounded_amount_end_to_end(role):
    profiles, _ = load_profiles()
    spec = profiles["controlled"][role]
    client = _ThrottlingClient()
    invoker = BedrockInvoker(model=spec.model, client=client, **spec.options)
    tracker = SpendTracker(9.0)
    system, user = "s" * 2_000, "u" * (TYPICAL_PROMPT_CHARS - 2_000)
    with pytest.raises(RuntimeError, match="Throttled"):
        invoke_metered(invoker, spec, tracker, system, user, reserve_floor_usd=EST_JUDGE_CANDIDATE_USD)
    assert client.calls[0]["inferenceConfig"]["maxTokens"] == spec.options["max_tokens"]
    assert tracker.reserved_calls == 1
    assert EST_JUDGE_CANDIDATE_USD <= tracker.reserved_usd < PER_SEAT_RESERVATION_BOUND_USD
    assert tracker.blocked_reason is None
    # Three failed attempts on one candidate (EVALUATOR_MAX_ATTEMPTS) stay far below the cap.
    assert 3 * tracker.reserved_usd < 2.5
