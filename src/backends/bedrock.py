"""Amazon Bedrock Direct-API adapter (Converse API).

Wraps the ``bedrock-runtime`` Converse API as an :class:`Invoker`. Registered in
:data:`DEFAULT_ADAPTERS` as ``bedrock``.

The boto3/botocore imports are deferred (not at module level) so the module can
be imported — and tests can inject a fake client — without requiring real AWS
credentials or ``boto3`` on the import path.

Model-specific rules baked in here were confirmed live against Bedrock Runtime
(us-east-1, 2026-09-14):

* ``converse`` serves ``openai.*`` inference profiles (GPT-6 Astra) directly;
  their reasoning depth is ``additionalModelRequestFields.reasoning.effort``.
* Claude 5-series (Fable 5.1, Opus 5, Sonnet 5) use adaptive thinking:
  ``{"thinking": {"type": "adaptive"}, "output_config": {"effort": ...}}``.
  They reject ``thinking.type=enabled`` / ``budget_tokens``.
* Fable 5.1 rejects forced tool choice (``toolChoice.tool`` / ``.any``), so
  structured output uses ``toolChoice.auto`` and validates the reply instead.
* Thinking is billed as output and counts against ``maxTokens``; the default
  budget is therefore large, and a ``max_tokens`` stop is a loud failure.

Skim-stage models (docs/PLAN-skim-stage.md Phase 1, verified against the AWS
docs on 2026-09-20; the Nova request below was confirmed live on the same day
with three calls on a synthetic document — every field accepted, response
``serviceTier.type`` echoed ``flex``, ~2.5k input / ~340 output tokens,
$0.0009 per call at the Flex rate):

* Amazon Nova 2 Lite (``us.amazon.nova-2-lite-v1:0``) is served by Converse
  (model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html).
  Forced tool choice ``toolChoice: {"tool": {"name": ...}}`` is supported for
  Amazon Nova (and Claude 3) only —
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolChoice.html ;
  Nova serves it by constrained decoding, "ideal for structured output" —
  https://docs.aws.amazon.com/nova/latest/nova2-userguide/using-tools.html .
  Extended reasoning is controlled by
  ``additionalModelRequestFields.reasoningConfig`` (``type: enabled|disabled``,
  default disabled; ``maxReasoningEffort: low|medium|high`` when enabled) —
  https://docs.aws.amazon.com/nova/latest/nova2-userguide/extended-thinking.html .
  ``inferenceConfig.temperature`` accepts 0.0–1.0 (default 0.7) —
  https://docs.aws.amazon.com/nova/latest/nova2-userguide/using-converse-api.html .
* The Converse request field for the processing tier is
  ``serviceTier: {"type": "default" | "flex" | "priority" | "reserved"}`` and the
  response echoes the tier actually used —
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ServiceTier.html ,
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html .
* Google Gemma 3 12B IT (``google.gemma-3-12b-it``) supports Bedrock's native
  structured outputs on bedrock-runtime but lists client-side tool calling only
  on the bedrock-mantle endpoint (model card:
  https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-12b-it.html),
  and forced tool choice is Nova/Claude-3-only (above). Gemma therefore gets
  the JSON-schema output format ``outputConfig.textFormat`` —
  https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html
  (not live-verified; Phase 1 only spends on Nova).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import TYPE_CHECKING, Any, Optional

from src.backends.direct_api import DirectApiInvoker

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

#: Default output budget. Adaptive-thinking models spend it on reasoning first;
#: the live probe saw Fable 5.1 consume a 150-token budget entirely on thinking.
DEFAULT_MAX_TOKENS = 32768

# Claude generation from an id like "global.anthropic.claude-fable-5-1" -> 5.
_CLAUDE_GENERATION_RE = re.compile(r"claude-(?:fable|opus|sonnet|haiku)-(\d+)")

_ANTHROPIC_ADAPTIVE_EFFORTS = frozenset({"low", "medium", "high", "max"})
_OPENAI_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_LEGACY_THINKING_BUDGETS = {"medium": 2048, "high": 8192, "max": 32768}

#: Converse ``serviceTier.type`` values (API_runtime_ServiceTier.html).
SERVICE_TIERS = frozenset({"default", "flex", "priority", "reserved"})

#: Name of the single tool offered for schema-constrained output.
STRUCTURED_OUTPUT_TOOL = "structured_output"

# Nova 2 ``reasoningConfig`` per effort level. ``low`` (and no effort) keeps
# extended thinking off — the skim stage's setting; ``max`` has no Nova
# equivalent and maps to ``high``.
_NOVA2_REASONING_EFFORTS = {"medium": "medium", "high": "high", "max": "high"}


#: Plausibility floor for reported input: no tokenizer in use here produces fewer
#: than one token per 12 characters of English/JSON prose (real ratios are 3-5).
#: Below the floor the provider's input count is treated as implausible.
MAX_PLAUSIBLE_CHARS_PER_INPUT_TOKEN = 12
#: Conservative replacement when the count is implausible (over-counts ~1.3-1.7x).
CONSERVATIVE_CHARS_PER_INPUT_TOKEN = 3


def _nonneg_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def reconcile_input_usage(usage: dict, input_chars: int | None, *, model: str = "") -> dict:
    """Make a Converse ``usage`` block safe to bill from. Mutates and returns it.

    Live metrics (2026-10-05): 307 of 309 ``global.openai.gpt-6-astra`` calls
    reported ``inputTokens: 2`` for 16-27k-character prompts — ~$1 of input per
    scheduled run never reached the spend cap. Two corrections, both only ever
    *raising* the billed input:

    1. ``totalTokens - outputTokens`` is the provider's own statement of input
       consumed. When it exceeds ``inputTokens + cacheRead + cacheWrite``, the
       difference is added to ``inputTokens`` (``inputTokensSource="totalTokens"``).
    2. If the effective input (``inputTokens + cacheRead + cacheWrite``) is still
       below ``input_chars / MAX_PLAUSIBLE_CHARS_PER_INPUT_TOKEN`` the count is
       implausible for a prompt of that length: ``inputTokens`` is replaced by
       ``ceil(input_chars / CONSERVATIVE_CHARS_PER_INPUT_TOKEN)`` (cache fields are
       kept and still priced) and the block is flagged ``inputTokensEstimated=True``.
       ``usage_source`` stays ``"real"`` so the charge lands on the cap; the metrics
       line carries ``reportedInputTokens`` and the flag for audit.

    Nothing is changed when the reported figures are internally consistent and
    plausible, so well-behaved models keep their exact usage.
    """
    inp = _nonneg_int(usage.get("inputTokens"))
    out = _nonneg_int(usage.get("outputTokens"))
    if inp is None or out is None:
        return usage  # cost._require_usage rejects incomplete usage; leave it alone
    cache_read = _nonneg_int(usage.get("cacheReadInputTokens")) or 0
    cache_write = _nonneg_int(usage.get("cacheWriteInputTokens")) or 0
    total = _nonneg_int(usage.get("totalTokens"))
    reported = inp

    accounted = inp + cache_read + cache_write
    if total is not None and total > out and total - out > accounted:
        inp += (total - out) - accounted
        usage["inputTokens"] = inp
        usage["inputTokensSource"] = "totalTokens"
        usage["reportedInputTokens"] = reported
        logger.warning("[bedrock] %s reported inputTokens=%d but totalTokens-outputTokens=%d; billing input as %d",
                       model, reported, total - out, inp)
        accounted = inp + cache_read + cache_write

    chars = _nonneg_int(input_chars)
    if chars and accounted < chars / MAX_PLAUSIBLE_CHARS_PER_INPUT_TOKEN:
        conservative = math.ceil(chars / CONSERVATIVE_CHARS_PER_INPUT_TOKEN)
        usage["inputTokens"] = conservative
        usage["inputTokensEstimated"] = True
        usage["inputTokensSource"] = "chars/3"
        usage.setdefault("reportedInputTokens", reported)
        logger.warning("[bedrock] %s reported %d input token(s) (+%d cached) for a %d-char prompt — implausible; "
                       "billing a conservative %d input tokens against the cap",
                       model, reported, cache_read + cache_write, chars, conservative)
    return usage


class BedrockInvoker(DirectApiInvoker):
    """Direct-API adapter for Amazon Bedrock (Converse API).

    Constructed as ``BedrockInvoker(model=<id>)``. A metered backend: blank model
    ids are rejected at construction.

    Parameters:
        model: Bedrock model id (base id or inference-profile id). Required.
        region: AWS region override. Defaults to ``BEDROCK_REGION`` env var,
            then ``us-east-1``.
        client: An injected ``bedrock-runtime`` client (for tests). When
            ``None``, a lazy singleton is built on first call.
        max_tokens: Maximum output tokens for ``inferenceConfig``. Defaults to
            the ``BEDROCK_MAX_TOKENS`` env var, then :data:`DEFAULT_MAX_TOKENS`.
            Thinking tokens count against this budget on adaptive models.
        max_retries: ``botocore`` adaptive retry ``max_attempts``.
        system_prompt_delivery: ``"native"`` (default) delivers the system
            prompt via the Converse ``system`` field; ``"prepend"`` folds it
            into the user message (fallback for models that reject a native
            system field).
        use_invoke_model: When ``True``, ``_invoke_provider`` calls
            ``client.invoke_model(...)`` instead of ``client.converse(...)``,
            and ``_extract_raw`` handles the different response format. This is
            the isolated seam for models the Converse API does not serve.
        temperature: Optional ``inferenceConfig.temperature``. ``None`` (the
            default) sends nothing and keeps the model's own default. The skim
            stage uses ``0.0`` for deterministic extraction.
        service_tier: Optional Converse ``serviceTier.type`` (one of
            :data:`SERVICE_TIERS`). ``None`` sends nothing (= standard tier).
            The skim stage requests ``"flex"``; the tier the request was
            actually served on is carried back in ``usage["serviceTier"]`` so
            :mod:`src.backends.cost` prices what was billed, not what was asked.
    """

    BACKEND_NAME: str = "bedrock"
    DEFAULT_MODEL: str = ""  # metered → must be set explicitly

    def __init__(
        self,
        model: str | None = None,
        *,
        region: str | None = None,
        client: Any | None = None,
        max_tokens: int | None = None,
        max_retries: int = 4,
        system_prompt_delivery: str = "native",
        use_invoke_model: bool = False,
        temperature: float | None = None,
        service_tier: str | None = None,
    ) -> None:
        super().__init__(model=model)
        # Metered backend: reject blank model id.
        if not self.model or not self.model.strip():
            raise ValueError(
                "BedrockInvoker requires an explicit model id (metered backend). "
                "Pass model=<id> at construction."
            )
        self._region = region or os.environ.get("BEDROCK_REGION", "us-east-1")
        self._injected_client = client
        self._lazy_client: Any | None = None
        if max_tokens is None:
            max_tokens = int(os.environ.get("BEDROCK_MAX_TOKENS", DEFAULT_MAX_TOKENS))
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        if system_prompt_delivery not in ("native", "prepend"):
            raise ValueError(
                f"system_prompt_delivery must be 'native' or 'prepend', "
                f"got {system_prompt_delivery!r}"
            )
        self._system_prompt_delivery = system_prompt_delivery
        self._use_invoke_model = use_invoke_model
        if temperature is not None and not (0.0 <= float(temperature) <= 1.0):
            raise ValueError(f"temperature must be within [0.0, 1.0], got {temperature!r}")
        self._temperature = None if temperature is None else float(temperature)
        if service_tier is not None and service_tier not in SERVICE_TIERS:
            raise ValueError(
                f"service_tier must be one of {sorted(SERVICE_TIERS)}, got {service_tier!r}"
            )
        self._service_tier = service_tier

    # ------------------------------------------------------------------ #
    # Model-family detection (drives the per-model request rules)         #
    # ------------------------------------------------------------------ #
    def _is_nova2(self) -> bool:
        """Amazon Nova 2 family (``[us.|global.]amazon.nova-2-*``): has
        ``reasoningConfig`` and supports forced tool choice."""
        return "amazon.nova-2" in self.model.lower()

    def _is_nova(self) -> bool:
        """Any Amazon Nova text model (Nova 1 and 2): forced tool choice supported."""
        return "amazon.nova-" in self.model.lower()

    def _is_gemma(self) -> bool:
        return "google.gemma" in self.model.lower()

    def _structured_output_mode(self) -> str:
        """How this model is asked for schema-shaped JSON.

        ``forced_tool``: Amazon Nova — ``toolChoice.tool`` (constrained decoding).
        ``json_schema``: Google Gemma — Converse ``outputConfig.textFormat``
        (no client-side tool calling on bedrock-runtime; forced tool choice is
        Nova/Claude-3-only per the ToolChoice API reference).
        ``auto_tool``: everything else (Claude 5, GPT) — the offered tool under
        ``toolChoice.auto``; Fable 5.1 rejects ``tool``/``any`` (live-verified
        2026-09-14) and every current model calls the offered tool anyway.
        """
        if self._is_nova():
            return "forced_tool"
        if self._is_gemma():
            return "json_schema"
        return "auto_tool"

    # ------------------------------------------------------------------ #
    # Client construction (lazy singleton, injectable for tests)         #
    # ------------------------------------------------------------------ #
    def _get_client(self, timeout: int) -> Any:
        """Return the ``bedrock-runtime`` boto3 client.

        Builds a lazy singleton on first call (mirroring ``src/embeddings.py``).
        If a ``client`` was injected at construction, returns it directly.
        """
        if self._injected_client is not None:
            return self._injected_client

        if self._lazy_client is None:
            import boto3
            from botocore.config import Config

            config = Config(
                region_name=self._region,
                retries={
                    "max_attempts": self._max_retries,
                    "mode": "adaptive",
                },
                read_timeout=timeout,
                connect_timeout=10,
            )
            self._lazy_client = boto3.client(
                "bedrock-runtime", config=config
            )
        return self._lazy_client

    # ------------------------------------------------------------------ #
    # Provider hook: structured-output shaping                           #
    # ------------------------------------------------------------------ #
    def _shape_structured_output(self, **adapter_opts) -> Optional[dict]:
        """Build the Converse request fragment for native structured output.

        When ``adapter_opts`` contains a ``schema`` key (a JSON Schema dict),
        returns a dict of request kwargs that ``_invoke_provider`` merges into
        the call — the shape depends on :meth:`_structured_output_mode`:

        * ``forced_tool`` (Nova): ``toolConfig`` with one ``ToolSpecification``
          carrying the schema as ``inputSchema`` and
          ``toolChoice: {"tool": {"name": "structured_output"}}``, so the model
          must answer with a schema-valid tool call.
        * ``auto_tool`` (Claude 5 / GPT): the same ``toolConfig`` with
          ``toolChoice: {"auto": {}}``. Claude Fable 5.1 rejects
          ``toolChoice.tool`` / ``.any`` with a ValidationException
          (live-verified), while every current model reliably calls the offered
          tool under ``auto``. If a model answers in text instead,
          ``_extract_raw`` falls back to the text and the shared JSON backstop
          validates it; an unparseable reply raises ``ValueError`` so the caller
          retries or aborts rather than storing garbage.
        * ``json_schema`` (Gemma): ``outputConfig.textFormat`` of type
          ``json_schema`` (Bedrock structured outputs); the reply is a text
          block holding the JSON.
        """
        schema = adapter_opts.get("schema")
        if schema is None:
            return None

        mode = self._structured_output_mode()
        if mode == "json_schema":
            return {
                "outputConfig": {
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {
                            "jsonSchema": {
                                "name": STRUCTURED_OUTPUT_TOOL,
                                "description": "Respond with structured output matching the schema.",
                                "schema": json.dumps(schema),
                            }
                        },
                    }
                }
            }

        tool_choice = (
            {"tool": {"name": STRUCTURED_OUTPUT_TOOL}} if mode == "forced_tool" else {"auto": {}}
        )
        return {
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": STRUCTURED_OUTPUT_TOOL,
                            "description": "Respond with structured output matching the schema.",
                            "inputSchema": {"json": schema},
                        }
                    }
                ],
                "toolChoice": tool_choice,
            }
        }

    # ------------------------------------------------------------------ #
    # Provider hook: Converse request construction                       #
    # ------------------------------------------------------------------ #
    def _invoke_provider(
        self,
        *,
        system_prompt: str,
        user_message: str,
        effort: str | None,
        timeout: int,
        structured_schema: Optional[dict],
    ) -> Any:
        """Build and execute the Converse API call (or InvokeModel fallback).

        Constructs the request per Req 4 (modelId, system, messages,
        inferenceConfig). Keeps user_message distinct from the system prompt
        (Req 5.2). Supports native and prepend system-prompt delivery (Req 5.1,
        5.3). When ``use_invoke_model`` is True, calls ``invoke_model`` instead.
        """
        if self._use_invoke_model:
            return self._invoke_model_fallback(
                system_prompt=system_prompt,
                user_message=user_message,
                effort=effort,
                timeout=timeout,
            )

        client = self._get_client(timeout)

        # --- Build messages ---
        if self._system_prompt_delivery == "prepend":
            # Fallback: fold system prompt into user message.
            combined = f"{system_prompt}\n\n{user_message}"
            messages = [
                {"role": "user", "content": [{"text": combined}]}
            ]
            system_field: list[dict] | None = None
        else:
            # Native delivery (default).
            messages = [
                {"role": "user", "content": [{"text": user_message}]}
            ]
            system_field = [{"text": system_prompt}]

        # --- Build request kwargs ---
        inference_config: dict[str, Any] = {"maxTokens": self._max_tokens}
        if self._temperature is not None:
            inference_config["temperature"] = self._temperature
        kwargs: dict[str, Any] = {
            "modelId": self.model,
            "messages": messages,
            "inferenceConfig": inference_config,
        }
        if system_field is not None:
            kwargs["system"] = system_field

        # --- Structured output (toolConfig or outputConfig, per model) ---
        if structured_schema is not None:
            kwargs.update(structured_schema)

        # --- Service tier (Flex for the skim stage) ---
        if self._service_tier is not None:
            kwargs["serviceTier"] = {"type": self._service_tier}

        # --- Effort / reasoning control (Req 11) ---
        # Where the model exposes a native reasoning control, map effort onto
        # additionalModelRequestFields. Otherwise effort is provenance-only
        # (recorded in the metrics line by the base) and does not fail. Nova 2
        # always receives an explicit reasoningConfig (off unless effort asks).
        additional_fields = self._additional_model_request_fields(effort)
        if additional_fields:
            kwargs["additionalModelRequestFields"] = additional_fields

        return client.converse(**kwargs)

    def _additional_model_request_fields(self, effort: str | None) -> dict[str, Any] | None:
        """``additionalModelRequestFields`` for this call.

        Nova 2 gets its ``reasoningConfig`` even when no effort is given, so
        extended thinking is explicitly off (the documented default, pinned).
        Every other model only receives fields when an effort level maps to a
        native control (:meth:`_map_effort_to_model_fields`).
        """
        if self._is_nova2():
            return self._map_effort_to_model_fields(effort or "low")
        if effort is None:
            return None
        return self._map_effort_to_model_fields(effort)

    # ------------------------------------------------------------------ #
    # Effort → native reasoning control mapping (Req 11)                 #
    # ------------------------------------------------------------------ #
    def _map_effort_to_model_fields(self, effort: str) -> dict[str, Any] | None:
        """Map an effort level onto ``additionalModelRequestFields``.

        The control differs by vendor and model generation (all live-verified
        against Bedrock Runtime on 2026-09-14):

        * OpenAI (``openai.gpt-*``): ``{"reasoning": {"effort": <level>}}``.
          (``reasoning_effort`` is rejected as an unknown parameter.)
        * Claude 5-series (``claude-{fable,opus,sonnet}-5*``): adaptive thinking,
          ``{"thinking": {"type": "adaptive"}, "output_config": {"effort": ...}}``.
          (``thinking.type=enabled`` + ``budget_tokens`` is rejected.)
        * Claude 4.x: extended thinking with a token budget per effort level;
          ``low`` disables thinking.
        * Amazon Nova 2 (``amazon.nova-2-*``): ``{"reasoningConfig": {"type":
          "disabled"}}`` for ``low``; ``{"type": "enabled", "maxReasoningEffort":
          medium|high}`` for ``medium`` / ``high`` (``max`` → ``high``). Nova 1
          models have no reasoning control and stay provenance-only.

        The mapping is best-effort and never raises — an unrecognized model or
        effort level records effort as provenance only (Req 11.2).
        """
        model_lower = self.model.lower()
        effort_lower = effort.lower()

        # --- Amazon Nova 2 (extended thinking via reasoningConfig) ---
        if self._is_nova2():
            if effort_lower == "low":
                return {"reasoningConfig": {"type": "disabled"}}
            nova_effort = _NOVA2_REASONING_EFFORTS.get(effort_lower)
            if nova_effort is None:
                self._unrecognized_effort(effort)
                return {"reasoningConfig": {"type": "disabled"}}
            return {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": nova_effort}}

        # --- OpenAI models served on Bedrock ---
        if "openai." in model_lower or model_lower.startswith("gpt-"):
            if effort_lower in _OPENAI_EFFORTS:
                return {"reasoning": {"effort": effort_lower}}
            return self._unrecognized_effort(effort)

        # --- Anthropic models ---
        generation_match = _CLAUDE_GENERATION_RE.search(model_lower)
        is_anthropic = "anthropic" in model_lower or "claude" in model_lower
        if not is_anthropic:
            return None  # no known reasoning control — provenance only

        generation = int(generation_match.group(1)) if generation_match else 4
        if generation >= 5:
            if effort_lower in _ANTHROPIC_ADAPTIVE_EFFORTS:
                return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort_lower}}
            return self._unrecognized_effort(effort)

        # Claude 4.x: extended thinking budgets (heuristic; no cross-backend
        # equivalence is assumed — Req 11.3).
        if effort_lower == "low":
            return {"thinking": {"type": "disabled"}}
        budget = _LEGACY_THINKING_BUDGETS.get(effort_lower)
        if budget is None:
            return self._unrecognized_effort(effort)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    def _unrecognized_effort(self, effort: str) -> None:
        logger.debug(
            "Unrecognized effort level %r for model %r; recording as provenance only.",
            effort, self.model,
        )
        return None

    # ------------------------------------------------------------------ #
    # InvokeModel fallback seam (Req 4.5)                                #
    # ------------------------------------------------------------------ #
    def _invoke_model_fallback(
        self,
        *,
        system_prompt: str,
        user_message: str,
        effort: str | None,
        timeout: int,
    ) -> Any:
        """Call ``client.invoke_model(...)`` for models Converse does not serve.

        Returns a dict with the JSON-decoded response body under the key
        ``_invoke_model_body`` so that ``_extract_raw`` can distinguish it from
        a Converse response.
        """
        client = self._get_client(timeout)

        # Build a generic request body (Anthropic Messages API format as the
        # most common Bedrock provider; subclasses can override for other
        # providers).
        request_body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        if self._system_prompt_delivery == "native":
            request_body["system"] = system_prompt

        response = client.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body),
        )

        # Read the streaming body and parse as JSON.
        body_bytes = response["body"].read()
        body_json = json.loads(body_bytes)

        # Wrap in a sentinel so _extract_raw can identify InvokeModel responses.
        return {"_invoke_model_body": body_json}

    # ------------------------------------------------------------------ #
    # Provider hook: extract final text from response                     #
    # ------------------------------------------------------------------ #
    def _extract_raw(self, response: Any) -> str:
        """Extract final text from a Converse or InvokeModel response.

        For Converse responses:
        - If structured output was requested and the response contains a
          ``toolUse`` block, prefer its ``input`` payload (a dict serialized to
          JSON string).
        - Otherwise, concatenate ``text`` blocks from ``output.message.content``.

        For InvokeModel responses (``_invoke_model_body`` sentinel):
        - Extract text from the provider-specific body (Anthropic Messages
          format: ``content[].text``).
        """
        # --- InvokeModel fallback response ---
        if isinstance(response, dict) and "_invoke_model_body" in response:
            body = response["_invoke_model_body"]
            # Anthropic Messages API format: {"content": [{"type": "text", "text": "..."}]}
            content_blocks = body.get("content", [])
            text_parts = [
                block.get("text", "")
                for block in content_blocks
                if block.get("type") == "text"
            ]
            return "".join(text_parts)

        # --- Converse response ---
        # A max_tokens stop means the reply is incomplete — on adaptive-thinking
        # models often thinking-only, with no answer at all. Never parse it as
        # if it were whole; surface it so the caller retries/aborts and the
        # operator knows to raise BEDROCK_MAX_TOKENS.
        if isinstance(response, dict) and response.get("stopReason") == "max_tokens":
            usage = response.get("usage") or {}
            raise RuntimeError(
                f"Bedrock response truncated (stopReason=max_tokens) for model {self.model!r} "
                f"after {usage.get('outputTokens', '?')} output tokens; "
                f"maxTokens={self._max_tokens}. Raise BEDROCK_MAX_TOKENS or lower effort."
            )

        content_blocks = (
            response.get("output", {}).get("message", {}).get("content", [])
        )

        # Prefer toolUse.input if present (native structured output). Blocks of
        # other kinds — notably ``reasoningContent`` from adaptive-thinking
        # models — are ignored here.
        for block in content_blocks:
            if "toolUse" in block:
                tool_input = block["toolUse"].get("input")
                if tool_input is not None:
                    # toolUse.input is already a parsed dict; serialize to JSON
                    # string so the parse backstop can process it uniformly.
                    return json.dumps(tool_input)

        # Fall back to concatenating text blocks.
        text_parts = [
            block["text"]
            for block in content_blocks
            if "text" in block
        ]
        return "".join(text_parts)

    # ------------------------------------------------------------------ #
    # Provider hook: usage extraction (Req 6)                            #
    # ------------------------------------------------------------------ #
    def _extract_usage(
        self, *, response: Any, parsed: Any, metrics: dict
    ) -> tuple[Optional[dict], str]:
        """Extract real token usage from the Converse response.

        The Converse API returns ``response["usage"]`` with ``inputTokens``,
        ``outputTokens``, ``totalTokens``. If present, return real usage; if
        absent, tolerate-and-warn (Req 6.2, 6.3).
        """
        # InvokeModel fallback responses may carry usage differently.
        if isinstance(response, dict) and "_invoke_model_body" in response:
            body = response["_invoke_model_body"]
            usage_block = body.get("usage")
            if usage_block and isinstance(usage_block, dict):
                usage = {
                    "inputTokens": usage_block.get("input_tokens"),
                    "outputTokens": usage_block.get("output_tokens"),
                    "totalTokens": usage_block.get("total_tokens"),
                }
                reconcile_input_usage(usage, metrics.get("input_chars"), model=self.model)
                return (usage, "real")
            return self._warn_missing_usage("InvokeModel response missing usage")

        # Standard Converse response path.
        usage_block = response.get("usage") if isinstance(response, dict) else None
        if usage_block and isinstance(usage_block, dict):
            # Bedrock auto-caches long system prompts: inputTokens then covers
            # only the uncached remainder and the bulk is reported as cache
            # reads/writes. Carry them so cost accounting sees the real input.
            usage = {
                "inputTokens": usage_block.get("inputTokens"),
                "outputTokens": usage_block.get("outputTokens"),
                "totalTokens": usage_block.get("totalTokens", 0),
                "cacheReadInputTokens": usage_block.get("cacheReadInputTokens", 0),
                "cacheWriteInputTokens": usage_block.get("cacheWriteInputTokens", 0),
            }
            reconcile_input_usage(usage, metrics.get("input_chars"), model=self.model)
            # The tier the request was actually served on (Converse echoes it);
            # cost.py prices Flex only from this, never from what was requested.
            served_tier = (response.get("serviceTier") or {}).get("type")
            if isinstance(served_tier, str) and served_tier:
                usage["serviceTier"] = served_tier
            return (usage, "real")

        # Tolerate-and-warn: parseable response but no usage block.
        return self._warn_missing_usage(
            "Converse response missing 'usage' block"
        )

    # ------------------------------------------------------------------ #
    # Provider hook: failure-mode mapping (Req 7)                        #
    # ------------------------------------------------------------------ #
    def _map_exception(self, exc: Exception) -> Exception:
        """Map botocore/Bedrock exceptions to standard TimeoutError / RuntimeError.

        Deferred imports match the pattern used in ``_get_client``. The mapping
        ensures failure-mode parity with the CLI adapters: timeout conditions →
        ``TimeoutError``; all other provider errors → ``RuntimeError``. Never
        returns an empty ``InvocationResult`` or a fabricated verdict.
        """
        from botocore.exceptions import (
            ClientError,
            ConnectTimeoutError,
            ReadTimeoutError,
        )

        # --- Client-side read timeout ---
        if isinstance(exc, ReadTimeoutError):
            return TimeoutError(f"Bedrock read timeout: {exc}")

        # --- Client-side connect timeout ---
        if isinstance(exc, ConnectTimeoutError):
            return TimeoutError(f"Bedrock connect timeout: {exc}")

        # --- botocore ClientError (covers all Bedrock service errors) ---
        if isinstance(exc, ClientError):
            error_code = exc.response.get("Error", {}).get("Code", "")
            error_message = exc.response.get("Error", {}).get("Message", str(exc))

            if error_code == "ModelTimeoutException":
                return TimeoutError(
                    f"Bedrock model timeout: {error_message}"
                )
            elif error_code == "ThrottlingException":
                return RuntimeError(f"Throttled: {error_message}")
            elif error_code == "ServiceUnavailableException":
                return RuntimeError(f"Service unavailable: {error_message}")
            elif error_code == "InternalServerException":
                return RuntimeError(f"Internal server error: {error_message}")
            elif error_code == "ValidationException":
                return RuntimeError(f"Validation error: {error_message}")
            elif error_code == "AccessDeniedException":
                return RuntimeError(f"Access denied: {error_message}")
            else:
                return RuntimeError(f"Bedrock error: {error_message}")

        # --- Unrecognized exception → return unchanged (re-raised as-is) ---
        return exc
