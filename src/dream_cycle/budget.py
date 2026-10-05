"""Canonical discovery budgets and candidate limits."""
import os
from src.backends.cost import cap_from_env

EVALUATOR_MAX_ATTEMPTS = 3

EXPLORER_TIMEOUT_S = int(os.environ.get("EXPLORER_TIMEOUT", "1200"))

THINKER_TIMEOUT_S = int(os.environ.get("THINKER_TIMEOUT", "1200"))

GROUNDING_SPEND_SHARE = float(os.environ.get("GROUNDING_SPEND_SHARE", "0.8"))

# R1 decision kept in R3: the grounding audit runs on the skeptic seat (whose
# Bedrock ``max_tokens`` budget R1 sized for it). 74ba02d's dedicated
# ``grounding_auditor`` role is a backends/config change and is not ported here.
GROUNDING_AUDIT_ROLE = "skeptic"

MAX_CANDIDATES_PER_SLICE = 2

EVALUATOR_BACKOFF_S = 2.0

SKIM_SPEND_CAP_USD = 1.00

SKIM_RUN_CAP_SHARE = 0.15

SKIM_DISABLED_ENV = "SECOND_BRAIN_SKIM_DISABLED"

EST_THINKER_CALL_USD = 0.30

EST_JUDGE_CANDIDATE_USD = 0.70

# Floor for the conservative reservation charged when a call fails with no
# response (throttle/timeout/transport): the larger of the worst-case token price
# and the stage estimate above. Explorer and skim have no per-stage estimate
# (their reservation is the computed worst case alone). R1 transport policy.
_RESERVE_FLOOR_USD = {
    "thinker": EST_THINKER_CALL_USD,
    "skeptic": EST_JUDGE_CANDIDATE_USD,
    "advocate": EST_JUDGE_CANDIDATE_USD,
    "epistemologist": EST_JUDGE_CANDIDATE_USD,
    "methodologist": EST_JUDGE_CANDIDATE_USD,
}

def _skim_cap_usd(run_cap_usd: float) -> float:
    """The skim's sub-cap: SECOND_BRAIN_SKIM_CAP_USD (default SKIM_SPEND_CAP_USD),
    bounded to SKIM_RUN_CAP_SHARE of the run cap. A bad env value falls back to the
    default with a warning, like SpendTracker.from_env."""
    cap = SKIM_SPEND_CAP_USD
    cap = cap_from_env("SECOND_BRAIN_SKIM_CAP_USD", cap)
    if run_cap_usd and run_cap_usd > 0:
        cap = min(cap, SKIM_RUN_CAP_SHARE * float(run_cap_usd))
    return cap
