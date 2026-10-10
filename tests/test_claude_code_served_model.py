"""Claude Code served-model check from the stream-json event stream.

Uses the same stream-json fixture shapes as tests/test_claude_code.py (system
init, assistant, terminal result); ``subprocess.run`` is patched, no CLI runs.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.backends.base import CorpusRoutingError
from src.backends.claude_code import ClaudeCodeInvoker, canonical_model_id

REQUESTED = "global.anthropic.claude-sonnet-5"
USAGE = {"input_tokens": 10, "output_tokens": 5}


def _stream(init_model=None, model_usage=None, result='{"ok": 1}'):
    init = {"type": "system", "subtype": "init", "session_id": "s1"}
    if init_model is not None:
        init["model"] = init_model
    terminal = {"type": "result", "subtype": "success", "is_error": False,
                "result": result, "usage": USAGE, "total_cost_usd": 0.01}
    if model_usage is not None:
        terminal["modelUsage"] = {k: {"inputTokens": 1} for k in model_usage}
    events = [init, {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}, terminal]
    return "\n".join(json.dumps(e) for e in events)


def _invoke(stdout, stage="evaluator:advocate", tmp_path=None):
    records = []
    inv = ClaudeCodeInvoker(model=REQUESTED)
    with patch("src.backends.claude_code.subprocess.run",
               return_value=MagicMock(returncode=0, stdout=stdout, stderr="")), \
         patch.object(ClaudeCodeInvoker, "_record_metrics", lambda self, m, run_id: records.append(dict(m))):
        try:
            return inv.invoke("sys", "msg", stage=stage), records[-1], None
        except Exception as exc:  # noqa: BLE001 — surfaced to the assertion
            return None, records[-1], exc


@pytest.mark.parametrize("served", [
    REQUESTED, "us.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5-v1:0",
    "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.anthropic.claude-sonnet-5",
    "claude-sonnet-5", "claude-sonnet-5[1m]",
])
def test_documented_inference_profile_forms_are_honored(served):
    result, rec, exc = _invoke(_stream(init_model=served))
    assert exc is None and result["output"] == {"ok": 1}
    assert rec["model_honored"] is True and rec["served_model"] == served


def test_init_mismatch_on_a_corpus_role_raises_before_the_result_is_used():
    result, rec, exc = _invoke(_stream(init_model="claude-fable-5-1"))
    assert isinstance(exc, CorpusRoutingError) and result is None
    assert rec["model_honored"] is False and rec["error"] == "model_not_honored"
    assert "output_chars" in rec and rec["output_chars"] == 0  # never parsed
    # usage is still attached so invoke_metered bills the response
    assert exc.model_response_received and exc.usage["input_tokens"] == 10


def test_mismatch_on_a_non_corpus_stage_is_recorded_not_raised():
    result, rec, exc = _invoke(_stream(init_model="global.anthropic.claude-opus-5"), stage="adhoc")
    assert exc is None and result["output"] == {"ok": 1}
    assert rec["model_honored"] is False


def test_model_usage_fallback_tolerates_a_helper_model():
    result, rec, exc = _invoke(_stream(model_usage=["claude-haiku-4-5-20251001", "claude-sonnet-5"]))
    assert exc is None and rec["model_honored"] is True
    assert rec["served_model"] == "claude-sonnet-5"
    assert rec["billed_models"] == ["claude-haiku-4-5-20251001", "claude-sonnet-5"]


def test_model_usage_without_the_requested_model_raises_for_corpus():
    _, rec, exc = _invoke(_stream(model_usage=["claude-opus-5"]), stage="explorer")
    assert isinstance(exc, CorpusRoutingError) and rec["model_honored"] is False


def test_no_reported_model_is_unknown_not_fatal():
    result, rec, exc = _invoke(_stream())
    assert exc is None and rec["model_honored"] is None and rec["served_model"] is None


def test_metered_mismatch_blocks_the_tracker_and_bills_usage():
    from src.backends import cost
    from src.backends.resolver import RoleBackend
    spec = RoleBackend("advocate", "claude_code", REQUESTED, "high")
    tracker = cost.SpendTracker(cap_usd=5.0)
    inv = ClaudeCodeInvoker(model=REQUESTED)
    with patch("src.backends.claude_code.subprocess.run",
               return_value=MagicMock(returncode=0, stdout=_stream(init_model="claude-opus-5"), stderr="")), \
         patch.object(ClaudeCodeInvoker, "_record_metrics", lambda *a: None):
        with pytest.raises(CorpusRoutingError):
            cost.invoke_metered(inv, spec, tracker, "sys", "msg", stage="evaluator:advocate")
    assert tracker.blocked_reason and tracker.calls == 1


def test_canonical_ids_keep_families_and_versions_apart():
    assert canonical_model_id("global.anthropic.claude-opus-4-8") == canonical_model_id("claude-opus-4.8")
    assert canonical_model_id("claude-opus-4-8") != canonical_model_id("claude-opus-4-6")
    assert canonical_model_id("claude-sonnet-5") != canonical_model_id("claude-opus-5")
