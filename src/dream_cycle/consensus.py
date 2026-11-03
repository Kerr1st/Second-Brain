"""Three-of-four judgment policy. This is not a distributed correctness guarantee."""
from src.models import EvaluatorVerdict


def tally_consensus(verdicts: list[EvaluatorVerdict]) -> str:
    """Accept at least three of four independent votes; otherwise reject.

    No individual seat has a veto. Model-family diversity depends on the selected
    profile and is not proof that the candidate is correct or useful.
    """
    if len(verdicts) != 4:
        raise ValueError(f"Expected 4 verdicts, got {len(verdicts)}")
    return "ACCEPTED" if sum(v.verdict == "ACCEPT" for v in verdicts) >= 3 else "REJECTED"
