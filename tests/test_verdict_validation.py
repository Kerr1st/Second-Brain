"""Invalid model answers cannot silently become panel votes."""
import pytest
from src.models import EvaluatorVerdict


@pytest.mark.parametrize("verdict", ["ACCEPTED", "REJECTED", "accept", "DEFER", "", None, [], True])
def test_invalid_verdict_is_a_contract_failure(verdict):
    with pytest.raises(ValueError, match="ACCEPT or REJECT"):
        EvaluatorVerdict(role="skeptic", verdict=verdict)


@pytest.mark.parametrize("verdict", ["ACCEPT", "REJECT"])
def test_binary_verdict_retains_graded_evidence(verdict):
    result = EvaluatorVerdict(role="skeptic", verdict=verdict, score=4, advisory_issues=["Timing uncertain"])
    assert result.verdict == verdict
    assert result.score == 4
    assert result.advisory_issues == ["Timing uncertain"]
