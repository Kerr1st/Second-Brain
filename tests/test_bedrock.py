"""Tests for the direct Bedrock adapter (src/backends/bedrock.py).

All provider calls go through an injected fake ``bedrock-runtime`` client, so
nothing here needs AWS credentials or network. The request/response shapes and
the model-specific parameter rules below were confirmed against Bedrock Runtime
in us-east-1 on 2026-09-14:

* ``converse`` serves ``openai.*`` inference profiles (GPT-6 Astra) directly.
* Claude 5-series (Fable 5.1 / Opus 5 / Sonnet 5) accept
  ``{"thinking": {"type": "adaptive"}, "output_config": {"effort": ...}}`` and
  REJECT ``thinking.type=enabled`` + ``budget_tokens``.
* Fable 5.1 rejects forced tool choice (``toolChoice.tool`` / ``any``).
* OpenAI models accept ``{"reasoning": {"effort": ...}}`` and reject
  ``reasoning_effort``.
* Both vendors may return a ``reasoningContent`` block before the answer.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from src.backends.bedrock import BedrockInvoker


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeBedrockClient:
    """Records ``converse`` kwargs and returns a canned response (or raises)."""

    def __init__(self, response=None, error=None):
        self.calls: list[dict] = []
        self._response = response
        self._error = error

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _converse_response(*blocks, stop_reason="end_turn", usage=(20, 7)):
    return {
        "output": {"message": {"role": "assistant", "content": list(blocks)}},
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": usage[0],
            "outputTokens": usage[1],
            "totalTokens": usage[0] + usage[1],
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        },
    }


def _reasoning_block(text="thinking..."):
    return {"reasoningContent": {"reasoningText": {"text": text}}}


@pytest.fixture(autouse=True)
def _isolated_metrics_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.backends.direct_api.METRICS_DIR", str(tmp_path / "llm_metrics"))
    return tmp_path / "llm_metrics"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_blank_model_rejected_metered_backend(self):
        with pytest.raises(ValueError):
            BedrockInvoker(model="")
        with pytest.raises(ValueError):
            BedrockInvoker()

    def test_region_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
        inv = BedrockInvoker(model="global.anthropic.claude-opus-5")
        assert inv._region == "us-west-2"

    def test_default_max_tokens_leaves_room_for_adaptive_thinking(self, monkeypatch):
        """Fable 5.1 / Astra bill thinking as output and count it against
        maxTokens; a 150-token budget was consumed entirely by thinking in the
        live probe. The default must be far above the old 4096."""
        monkeypatch.delenv("BEDROCK_MAX_TOKENS", raising=False)
        inv = BedrockInvoker(model="global.anthropic.claude-fable-5-1")
        assert inv._max_tokens >= 16384

    def test_max_tokens_env_override(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "2048")
        inv = BedrockInvoker(model="global.anthropic.claude-fable-5-1")
        assert inv._max_tokens == 2048


# ---------------------------------------------------------------------------
# Structured output shaping
# ---------------------------------------------------------------------------

class TestStructuredOutputShape:
    def test_tool_choice_is_auto_never_forced_for_claude(self):
        """Fable 5.1 returns ValidationException for toolChoice.tool / .any."""
        inv = BedrockInvoker(model="global.anthropic.claude-fable-5-1")
        cfg = inv._shape_structured_output(schema={"type": "object"})["toolConfig"]
        assert cfg["toolChoice"] == {"auto": {}}
        assert cfg["tools"][0]["toolSpec"]["inputSchema"] == {"json": {"type": "object"}}

    def test_no_schema_means_no_tool_config(self):
        inv = BedrockInvoker(model="global.anthropic.claude-fable-5-1")
        assert inv._shape_structured_output() is None

    @pytest.mark.parametrize("model", ["us.amazon.nova-2-lite-v1:0", "amazon.nova-2-lite-v1:0",
                                       "global.amazon.nova-2-lite-v1:0", "amazon.nova-pro-v1:0"])
    def test_nova_forces_the_structured_output_tool(self, model):
        """toolChoice.tool is supported for Amazon Nova (and Claude 3) only —
        https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolChoice.html ;
        Nova serves it with constrained decoding, so the reply is schema-valid."""
        inv = BedrockInvoker(model=model)
        cfg = inv._shape_structured_output(schema={"type": "object"})["toolConfig"]
        assert cfg["toolChoice"] == {"tool": {"name": "structured_output"}}
        assert cfg["tools"][0]["toolSpec"]["name"] == "structured_output"
        assert cfg["tools"][0]["toolSpec"]["inputSchema"] == {"json": {"type": "object"}}

    def test_gemma_uses_json_schema_output_format(self):
        """Gemma 3 12B: structured outputs yes, client-side tool calling only on
        bedrock-mantle → Converse outputConfig.textFormat (structured-output.html)."""
        inv = BedrockInvoker(model="google.gemma-3-12b-it")
        shape = inv._shape_structured_output(schema={"type": "object", "properties": {"a": {"type": "string"}}})
        assert "toolConfig" not in shape
        fmt = shape["outputConfig"]["textFormat"]
        assert fmt["type"] == "json_schema"
        js = fmt["structure"]["jsonSchema"]
        assert js["name"] == "structured_output"
        assert json.loads(js["schema"]) == {"type": "object", "properties": {"a": {"type": "string"}}}

    def test_openai_keeps_auto_tool_choice(self):
        inv = BedrockInvoker(model="global.openai.gpt-6-astra")
        cfg = inv._shape_structured_output(schema={"type": "object"})["toolConfig"]
        assert cfg["toolChoice"] == {"auto": {}}


# ---------------------------------------------------------------------------
# Construction options: temperature and service tier
# ---------------------------------------------------------------------------

class TestRequestOptions:
    def test_temperature_and_service_tier_validated(self):
        with pytest.raises(ValueError, match="temperature"):
            BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", temperature=1.5)
        with pytest.raises(ValueError, match="service_tier"):
            BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", service_tier="cheap")
        inv = BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", temperature=0, service_tier="flex")
        assert inv._temperature == 0.0 and inv._service_tier == "flex"

    def test_defaults_send_neither(self):
        client = _FakeBedrockClient(response=_converse_response({"text": "{}"}))
        inv = BedrockInvoker(model="global.anthropic.claude-sonnet-5", client=client)
        inv.invoke("sys", "user")
        call = client.calls[0]
        assert call["inferenceConfig"] == {"maxTokens": inv._max_tokens}
        assert "serviceTier" not in call


# ---------------------------------------------------------------------------
# Exact request kwargs per model family (the skim-stage contract)
# ---------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


class TestPerModelRequestShape:
    def test_nova_2_lite_full_skim_request(self):
        """(i) Nova 2 Lite: forced tool, reasoning explicitly off, temperature 0,
        Flex tier — the exact kwargs the skim stage sends (live-verified 2026-09-20)."""
        payload = {"summary": "s"}
        client = _FakeBedrockClient(response=_converse_response(
            {"toolUse": {"toolUseId": "t1", "name": "structured_output", "input": payload}},
            stop_reason="tool_use", usage=(3000, 120)))
        inv = BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", client=client, max_tokens=4096,
                             temperature=0.0, service_tier="flex")
        result = inv.invoke("sys", "doc", schema=_SCHEMA, stage="skim", run_id="r1")

        assert result["output"] == payload
        assert client.calls == [{
            "modelId": "us.amazon.nova-2-lite-v1:0",
            "messages": [{"role": "user", "content": [{"text": "doc"}]}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.0},
            "system": [{"text": "sys"}],
            "toolConfig": {
                "tools": [{"toolSpec": {
                    "name": "structured_output",
                    "description": "Respond with structured output matching the schema.",
                    "inputSchema": {"json": _SCHEMA},
                }}],
                "toolChoice": {"tool": {"name": "structured_output"}},
            },
            "serviceTier": {"type": "flex"},
            "additionalModelRequestFields": {"reasoningConfig": {"type": "disabled"}},
        }]

    def test_nova_2_reasoning_off_even_without_effort_and_without_schema(self):
        client = _FakeBedrockClient(response=_converse_response({"text": "{}"}))
        inv = BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", client=client)
        inv.invoke("sys", "user")
        call = client.calls[0]
        assert call["additionalModelRequestFields"] == {"reasoningConfig": {"type": "disabled"}}
        assert "toolConfig" not in call and "serviceTier" not in call
        assert call["inferenceConfig"] == {"maxTokens": inv._max_tokens}

    def test_fable_request_unchanged(self):
        """(ii) Fable 5.1: auto tool choice, adaptive thinking, no tier, no
        temperature, no reasoningConfig — byte-for-byte what shipped before."""
        client = _FakeBedrockClient(response=_converse_response(
            _reasoning_block(), {"text": json.dumps({"summary": "s"})}))
        inv = BedrockInvoker(model="global.anthropic.claude-fable-5-1", client=client, max_tokens=32768)
        inv.invoke("sys", "user", effort="high", schema=_SCHEMA)
        assert client.calls == [{
            "modelId": "global.anthropic.claude-fable-5-1",
            "messages": [{"role": "user", "content": [{"text": "user"}]}],
            "inferenceConfig": {"maxTokens": 32768},
            "system": [{"text": "sys"}],
            "toolConfig": {
                "tools": [{"toolSpec": {
                    "name": "structured_output",
                    "description": "Respond with structured output matching the schema.",
                    "inputSchema": {"json": _SCHEMA},
                }}],
                "toolChoice": {"auto": {}},
            },
            "additionalModelRequestFields": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},
        }]

    def test_openai_request_unchanged(self):
        """(iii) GPT-6 Astra: auto tool choice and reasoning.effort only."""
        client = _FakeBedrockClient(response=_converse_response({"text": json.dumps({"summary": "s"})}))
        inv = BedrockInvoker(model="global.openai.gpt-6-astra", client=client, max_tokens=32768)
        inv.invoke("sys", "user", effort="high", schema=_SCHEMA)
        assert client.calls == [{
            "modelId": "global.openai.gpt-6-astra",
            "messages": [{"role": "user", "content": [{"text": "user"}]}],
            "inferenceConfig": {"maxTokens": 32768},
            "system": [{"text": "sys"}],
            "toolConfig": {
                "tools": [{"toolSpec": {
                    "name": "structured_output",
                    "description": "Respond with structured output matching the schema.",
                    "inputSchema": {"json": _SCHEMA},
                }}],
                "toolChoice": {"auto": {}},
            },
            "additionalModelRequestFields": {"reasoning": {"effort": "high"}},
        }]

    def test_gemma_request_uses_output_config(self):
        """Gemma 3 12B: JSON-schema output format, no toolConfig, no reasoning
        fields (doc-verified only; Phase 1 spends on Nova alone)."""
        client = _FakeBedrockClient(response=_converse_response({"text": json.dumps({"summary": "s"})}))
        inv = BedrockInvoker(model="google.gemma-3-12b-it", client=client, max_tokens=2048,
                             temperature=0.0, service_tier="flex")
        result = inv.invoke("sys", "doc", schema=_SCHEMA)
        assert result["output"] == {"summary": "s"}
        call = client.calls[0]
        assert "toolConfig" not in call and "additionalModelRequestFields" not in call
        assert call["outputConfig"]["textFormat"]["type"] == "json_schema"
        assert json.loads(call["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"]) == _SCHEMA
        assert call["serviceTier"] == {"type": "flex"}
        assert call["inferenceConfig"] == {"maxTokens": 2048, "temperature": 0.0}

    def test_served_service_tier_is_carried_in_usage(self):
        """Converse echoes serviceTier; cost.py prices Flex from this echo only."""
        resp = _converse_response(
            {"toolUse": {"toolUseId": "t1", "name": "structured_output", "input": {"summary": "s"}}},
            stop_reason="tool_use", usage=(1000, 50))
        resp["serviceTier"] = {"type": "flex"}
        client = _FakeBedrockClient(response=resp)
        inv = BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", client=client, service_tier="flex")
        result = inv.invoke("sys", "doc", schema=_SCHEMA)
        assert result["usage"]["serviceTier"] == "flex"
        from src.backends.cost import estimate_cost_usd
        assert estimate_cost_usd("us.amazon.nova-2-lite-v1:0", result["usage"]) == pytest.approx(
            1000 * 0.165 / 1e6 + 50 * 1.375 / 1e6)

    def test_no_service_tier_echo_means_no_key(self):
        client = _FakeBedrockClient(response=_converse_response({"text": "{}"}))
        inv = BedrockInvoker(model="us.amazon.nova-2-lite-v1:0", client=client)
        result = inv.invoke("sys", "user")
        assert "serviceTier" not in result["usage"]


# ---------------------------------------------------------------------------
# invoke(): request construction and response handling
# ---------------------------------------------------------------------------

class TestInvoke:
    def _invoker(self, response, model="global.anthropic.claude-fable-5-1", **kw):
        client = _FakeBedrockClient(response=response)
        return BedrockInvoker(model=model, client=client, **kw), client

    def test_text_json_with_leading_reasoning_block(self, _isolated_metrics_dir):
        payload = {"verdict": "ACCEPT", "reasoning": "grounded"}
        inv, client = self._invoker(
            _converse_response(_reasoning_block(), {"text": json.dumps(payload)}, usage=(479, 150))
        )
        result = inv.invoke("sys", "user", effort="high", stage="evaluator:skeptic", run_id="r1")

        assert result["output"] == payload
        assert result["raw"] == json.dumps(payload)
        assert result["usage"] == {
            "inputTokens": 479, "outputTokens": 150, "totalTokens": 629,
            "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0,
        }
        assert result["usage_source"] == "real"

        # Request shape: native system prompt, single user turn, big output budget.
        call = client.calls[0]
        assert call["modelId"] == "global.anthropic.claude-fable-5-1"
        assert call["system"] == [{"text": "sys"}]
        assert call["messages"] == [{"role": "user", "content": [{"text": "user"}]}]
        assert call["inferenceConfig"]["maxTokens"] == inv._max_tokens

        # Metrics line carries real tokens.
        lines = (_isolated_metrics_dir / "r1.jsonl").read_text().splitlines()
        rec = json.loads(lines[-1])
        assert rec["backend"] == "bedrock" and rec["success"] is True
        assert rec["real_input_tokens"] == 479 and rec["real_output_tokens"] == 150

    def test_cache_tokens_survive_usage_extraction(self):
        """Live runs showed inputTokens=2 with ~8K tokens in cacheReadInputTokens;
        dropping them under-reports the panel's input cost entirely."""
        resp = _converse_response({"text": "{}"}, usage=(2, 700))
        resp["usage"].update({"cacheReadInputTokens": 8100, "cacheWriteInputTokens": 300})
        inv, _client = self._invoker(resp, model="global.openai.gpt-6-astra")
        result = inv.invoke("sys", "user")
        assert result["usage"]["cacheReadInputTokens"] == 8100
        assert result["usage"]["cacheWriteInputTokens"] == 300

    def test_tool_use_block_preferred_over_text(self):
        payload = {"verdict": "REJECT", "reasoning": "two notes are not a pattern"}
        inv, _ = self._invoker(
            _converse_response(
                _reasoning_block(),
                {"text": "Calling the tool now."},
                {"toolUse": {"toolUseId": "t1", "name": "structured_output", "input": payload}},
                stop_reason="tool_use",
            ),
            model="global.openai.gpt-6-astra",
        )
        result = inv.invoke("sys", "user", schema={"type": "object"})
        assert result["output"] == payload

    def test_max_tokens_truncation_fails_loudly(self):
        """A response cut off at maxTokens is not parsed as if complete."""
        inv, _ = self._invoker(
            _converse_response(_reasoning_block(), stop_reason="max_tokens", usage=(479, 150))
        )
        with pytest.raises(RuntimeError, match="max_tokens"):
            inv.invoke("sys", "user")

    def test_tools_true_rejected_before_any_provider_call(self):
        inv, client = self._invoker(_converse_response({"text": "{}"}))
        with pytest.raises(ValueError):
            inv.invoke("sys", "user", tools=True)
        assert client.calls == []

    def test_no_effort_means_no_additional_fields(self):
        inv, client = self._invoker(_converse_response({"text": "[]"}))
        inv.invoke("sys", "user")
        assert "additionalModelRequestFields" not in client.calls[0]


# ---------------------------------------------------------------------------
# Effort -> vendor-specific reasoning control
# ---------------------------------------------------------------------------

class TestEffortMapping:
    @pytest.mark.parametrize(
        "model, effort, expected",
        [
            # Claude 5-series: adaptive thinking + output_config.effort (live-verified).
            ("global.anthropic.claude-fable-5-1", "high",
             {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
            ("global.anthropic.claude-fable-5-1", "max",
             {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}),
            ("us.anthropic.claude-opus-5", "medium",
             {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}),
            ("global.anthropic.claude-sonnet-5", "low",
             {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
            # Claude 4.x keeps the extended-thinking budget mapping.
            ("global.anthropic.claude-opus-4-8", "high",
             {"thinking": {"type": "enabled", "budget_tokens": 8192}}),
            ("global.anthropic.claude-opus-4-8", "low",
             {"thinking": {"type": "disabled"}}),
            # OpenAI on Bedrock: reasoning.effort (live-verified; reasoning_effort rejected).
            ("global.openai.gpt-6-astra", "high", {"reasoning": {"effort": "high"}}),
            ("us.openai.gpt-5.6-sol", "xhigh", {"reasoning": {"effort": "xhigh"}}),
            # Unknown vendor: provenance-only.
            ("amazon.nova-pro-v1:0", "high", None),
            # Nova 2: reasoningConfig (extended-thinking.html). low = off (skim setting).
            ("us.amazon.nova-2-lite-v1:0", "low", {"reasoningConfig": {"type": "disabled"}}),
            ("us.amazon.nova-2-lite-v1:0", "medium",
             {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "medium"}}),
            ("us.amazon.nova-2-lite-v1:0", "high",
             {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "high"}}),
            ("us.amazon.nova-2-lite-v1:0", "max",
             {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "high"}}),
            ("us.amazon.nova-2-lite-v1:0", "turbo", {"reasoningConfig": {"type": "disabled"}}),
            # Unknown effort level: provenance-only, never fail.
            ("global.anthropic.claude-fable-5-1", "turbo", None),
        ],
    )
    def test_mapping(self, model, effort, expected):
        inv = BedrockInvoker(model=model, client=_FakeBedrockClient())
        assert inv._map_effort_to_model_fields(effort) == expected

    def test_claude_5_series_never_sends_budget_tokens(self):
        """Fable 5.1 returns ValidationException for thinking.type=enabled."""
        for model in ("global.anthropic.claude-fable-5-1", "global.anthropic.claude-opus-5",
                      "global.anthropic.claude-sonnet-5"):
            inv = BedrockInvoker(model=model, client=_FakeBedrockClient())
            for effort in ("low", "medium", "high", "max"):
                fields = inv._map_effort_to_model_fields(effort)
                assert "budget_tokens" not in json.dumps(fields), (model, effort)
                assert fields["thinking"]["type"] == "adaptive"

    def test_effort_reaches_request(self):
        client = _FakeBedrockClient(response=_converse_response({"text": "[]"}))
        inv = BedrockInvoker(model="global.openai.gpt-6-astra", client=client)
        inv.invoke("sys", "user", effort="high")
        assert client.calls[0]["additionalModelRequestFields"] == {"reasoning": {"effort": "high"}}


# ---------------------------------------------------------------------------
# Failure-mode mapping
# ---------------------------------------------------------------------------

class TestExceptionMapping:
    def _client_error(self, code, message="boom"):
        return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")

    def test_validation_exception_becomes_runtime_error(self):
        inv = BedrockInvoker(
            model="global.anthropic.claude-fable-5-1",
            client=_FakeBedrockClient(error=self._client_error(
                "ValidationException", 'tool_choice: type "tool" and "any" are not supported for this model.'
            )),
        )
        with pytest.raises(RuntimeError, match="Validation error"):
            inv.invoke("sys", "user")

    def test_read_timeout_becomes_timeout_error(self):
        inv = BedrockInvoker(
            model="global.anthropic.claude-fable-5-1",
            client=_FakeBedrockClient(error=ReadTimeoutError(endpoint_url="https://bedrock")),
        )
        with pytest.raises(TimeoutError):
            inv.invoke("sys", "user")

    def test_throttling_becomes_runtime_error(self):
        inv = BedrockInvoker(
            model="global.openai.gpt-6-astra",
            client=_FakeBedrockClient(error=self._client_error("ThrottlingException")),
        )
        with pytest.raises(RuntimeError, match="Throttled"):
            inv.invoke("sys", "user")
