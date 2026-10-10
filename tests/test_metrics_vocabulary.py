"""One per-call latency key (``latency_s``) across backends; readers accept ``duration_s``."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.backends.base import METRICS_LATENCY_KEY, call_latency_s
from src.backends.bedrock import BedrockInvoker
from src.backends.claude_code import ClaudeCodeInvoker
from tests.test_bedrock import _FakeBedrockClient, _converse_response


def _capture(cls):
    records = []
    return records, patch.object(cls, "_record_metrics", lambda self, m, run_id: records.append(dict(m)))


def test_direct_api_writes_latency_s_only():
    records, p = _capture(BedrockInvoker)
    client = _FakeBedrockClient(response=_converse_response({"text": "{}"}))
    with p:
        BedrockInvoker(model="global.anthropic.claude-sonnet-5", client=client).invoke("sys", "user")
    rec = records[-1]
    assert METRICS_LATENCY_KEY == "latency_s"
    assert isinstance(rec["latency_s"], float) and "duration_s" not in rec


def test_agentic_cli_writes_latency_s_only():
    records, p = _capture(ClaudeCodeInvoker)
    stdout = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init"},
        {"type": "result", "is_error": False, "result": "{}", "usage": {"input_tokens": 1, "output_tokens": 1}}])
    with p, patch("src.backends.claude_code.subprocess.run",
                  return_value=MagicMock(returncode=0, stdout=stdout, stderr="")):
        ClaudeCodeInvoker(model="global.anthropic.claude-sonnet-5").invoke("sys", "user")
    assert isinstance(records[-1]["latency_s"], float) and "duration_s" not in records[-1]


def test_reader_tolerates_the_legacy_key():
    assert call_latency_s({"latency_s": 1.5}) == 1.5
    assert call_latency_s({"duration_s": 2.0}) == 2.0          # pre-rename direct-API lines
    assert call_latency_s({"latency_s": 1.0, "duration_s": 9.0}) == 1.0
    assert call_latency_s({"duration_s": None}) is None and call_latency_s({}) is None
