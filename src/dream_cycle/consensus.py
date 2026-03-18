"""Consensus tally — pure function for verdict aggregation.

Implements Lamport's 3f+1 bound with f=1: the BFT quorum is
2f+1 = 3 out of 4 evaluators. Binary output — no DEFERRED state.
"""
from src.models import EvaluatorVerdict


def tally_consensus(verdicts: list[EvaluatorVerdict]) -> str:
    """Tally binary BFT consensus from 4 evaluator verdicts.

    Implements Lamport's 3f+1 bound with f=1: the BFT quorum is
    2f+1 = 3 out of 4 evaluators. Binary output — no DEFERRED state.

    Args:
        verdicts: List of exactly 4 EvaluatorVerdict objects.

    Returns:
        "ACCEPTED" if ≥3/4 ACCEPT, "REJECTED" otherwise.

    Raises:
        ValueError: If len(verdicts) != 4.
    """
    if len(verdicts) != 4:
        raise ValueError(f"Expected 4 verdicts, got {len(verdicts)}")
    accept_count = sum(1 for v in verdicts if v.verdict == "ACCEPT")
    return "ACCEPTED" if accept_count >= 3 else "REJECTED"
