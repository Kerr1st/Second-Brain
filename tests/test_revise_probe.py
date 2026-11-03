"""Manual revision follows the same routing and run budget as native stages."""
from unittest.mock import Mock

import pytest

from scripts.revise_probe import revise
from src.backends.cost import SpendTracker, SpendCapExceeded
from src.backends.resolver import RoleBackend
from src.models import CandidateInsight


def test_revision_records_spend_and_stops_before_a_second_call():
    candidate = CandidateInsight(title="Synthetic", type="insight", operation="CREATE", content="Synthetic content",
                                 source_memories=[], relationships=[], strategy_that_found_it="synthetic")
    invoker = Mock()
    invoker.invoke.return_value = {"output": {"title": "Revised", "content": "Synthetic revision"},
                                   "usage": {"inputTokens": 1000, "outputTokens": 100}}
    spec = RoleBackend("thinker", "bedrock", "global.anthropic.claude-opus-5", "medium")
    tracker = SpendTracker(0.001)
    with pytest.raises(SpendCapExceeded):
        revise(candidate, "synthetic", [], [], invoker, "medium", spec=spec, tracker=tracker)
    with pytest.raises(SpendCapExceeded):
        revise(candidate, "synthetic", [], [], invoker, "medium", spec=spec, tracker=tracker)
    assert invoker.invoke.call_count == tracker.calls == 1


def test_revision_rejects_forbidden_model_without_sending_sources():
    candidate = CandidateInsight(title="Synthetic", type="insight", operation="CREATE", content="Synthetic content",
                                 source_memories=[], relationships=[], strategy_that_found_it="synthetic")
    invoker = Mock()
    spec = RoleBackend("thinker", "bedrock", "claude-fable-5-1", "medium")
    with pytest.raises(ValueError):
        revise(candidate, "synthetic", [], [], invoker, "medium", spec=spec, tracker=SpendTracker(1))
    invoker.invoke.assert_not_called()
