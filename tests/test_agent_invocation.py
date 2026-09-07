"""Actual Dream Cycle invocation contracts; only the external agent is scripted."""

import json

import pytest

from src.backends.resolver import Resolver, RoleBackend
from src.dream_cycle.orchestrator import DreamCycleOrchestrator
from src.models import MemorySlice


class RecordingAgent:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def invoke(self, **request):
        self.calls.append(request)
        if isinstance(self.output, Exception):
            raise self.output
        return {"output": self.output}


def orchestrator_with_agent(agent):
    orchestrator = DreamCycleOrchestrator()
    orchestrator.resolver = Resolver(
        {role: RoleBackend(role, "codex", "fixture-model", "high")
         for role in ("explorer", "thinker")},
        adapters={"codex": lambda model: agent},
    )
    return orchestrator


@pytest.mark.parametrize("memory_ids,titles", [
    (["source-a"], ['Decision: preserve "exact" provenance — café']),
    ([], []),
])
def test_thinker_receives_complete_slice_and_returns_supported_candidate(memory_ids, titles):
    agent = RecordingAgent([{
        "title": "Preserve source ownership",
        "content": "Unknown ownership remains unknown.",
        "source_memories": memory_ids,
    }])
    orchestrator = orchestrator_with_agent(agent)
    result = orchestrator.invoke_thinker(MemorySlice(
        name="Ownership", strategy="contradiction_hunting",
        memory_ids=memory_ids, memory_titles=titles, hypothesis="Check newer evidence",
    ))

    request, = agent.calls
    assert json.loads(request["user_message"]) == {"memory_slice": {
        "name": "Ownership", "strategy": "contradiction_hunting",
        "memory_ids": memory_ids, "memory_titles": titles,
        "hypothesis": "Check newer evidence",
    }}
    assert request["tools"] is True
    assert request["stage"] == "thinker"
    assert request["effort"] == "high"
    assert request["system_prompt"]
    candidate, = result
    assert candidate.title == "Preserve source ownership"
    assert candidate.source_memories == memory_ids
    assert candidate.strategy_that_found_it == "contradiction_hunting"


def test_explorer_receives_feedback_and_returns_source_slice(test_db, clean_tables):
    agent = RecordingAgent([{
        "name": "Ownership", "strategy": "contradiction_hunting",
        "memory_ids": ["source-a"], "memory_titles": ["Source ownership"],
        "hypothesis": "Check newer evidence",
    }])
    result = orchestrator_with_agent(agent).invoke_explorer(
        feedback="Do not infer human ownership from workspace location.",
        run_type="scheduled",
    )
    request, = agent.calls
    assert "Do not infer human ownership from workspace location." in request["system_prompt"]
    assert "Memory count: 0" in request["user_message"]
    assert request["tools"] is True
    assert request["stage"] == "explorer"
    assert request["effort"] == "high"
    assert result == [MemorySlice(
        name="Ownership", strategy="contradiction_hunting",
        memory_ids=["source-a"], memory_titles=["Source ownership"],
        hypothesis="Check newer evidence",
    )]


@pytest.mark.parametrize("stage", ["explorer", "thinker"])
@pytest.mark.parametrize("response", [[], TimeoutError("agent unavailable")], ids=["empty", "unavailable"])
def test_invocation_distinguishes_no_candidates_from_provider_failure(test_db, clean_tables, stage, response):
    orchestrator = orchestrator_with_agent(RecordingAgent(response))
    if stage == "explorer":
        invoke = lambda: orchestrator.invoke_explorer(feedback="", run_type="scheduled")
    else:
        invoke = lambda: orchestrator.invoke_thinker(MemorySlice(
            name="Empty", strategy="pattern_emergence", memory_ids=[],
            memory_titles=[], hypothesis="No evidence",
        ))
    if isinstance(response, Exception):
        with pytest.raises(TimeoutError, match="agent unavailable"):
            invoke()
    else:
        assert invoke() == []
