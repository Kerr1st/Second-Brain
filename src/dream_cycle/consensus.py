"""Consensus tally — pure function for verdict aggregation."""
from src.models import EvaluatorVerdict


def tally_consensus(verdicts: list[EvaluatorVerdict]) -> str:
    """Tally consensus: 3/3=ACCEPTED, 2/3=DEFERRED, else=REJECTED.

    Args:
        verdicts: List of exactly 3 EvaluatorVerdict objects.

    Returns:
        "ACCEPTED" if 3/3 ACCEPT, "DEFERRED" if 2/3 ACCEPT, "REJECTED" otherwise.
    """
    accept_count = sum(1 for v in verdicts if v.verdict == "ACCEPT")
    if accept_count == 3:
        return "ACCEPTED"
    if accept_count == 2:
        return "DEFERRED"
    return "REJECTED"
