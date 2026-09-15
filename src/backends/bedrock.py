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
"""

from __future__ import annotations

import json
import logging
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
        """Build the Converse ``toolConfig`` for native structured output.

        When ``adapter_opts`` contains a ``schema`` key (a JSON Schema dict),
        returns a ``toolConfig`` with a single ``ToolSpecification`` carrying the
        schema as ``inputSchema`` and ``toolChoice`` set to ``auto``.

        ``auto`` rather than a forced tool: Claude Fable 5.1 rejects
        ``toolChoice.tool`` / ``toolChoice.any`` with a ValidationException
        (live-verified), while every current model reliably calls the offered
        tool under ``auto``. If a model answers in text instead, ``_extract_raw``
        falls back to the text and the shared JSON backstop validates it; an
        unparseable reply raises ``ValueError`` so the caller retries or aborts
        rather than storing garbage.
        """
        schema = adapter_opts.get("schema")
        if schema is None:
            return None

        tool_name = "structured_output"
        return {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool_name,
                        "description": "Respond with structured output matching the schema.",
                        "inputSchema": {"json": schema},
                    }
                }
            ],
            "toolChoice": {"auto": {}},
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
        kwargs: dict[str, Any] = {
            "modelId": self.model,
            "messages": messages,
            "inferenceConfig": {"maxTokens": self._max_tokens},
        }
        if system_field is not None:
            kwargs["system"] = system_field

        # --- Structured output (toolConfig) ---
        if structured_schema is not None:
            kwargs["toolConfig"] = structured_schema

        # --- Effort / reasoning control (Req 11) ---
        # Where the model exposes a native reasoning control, map effort onto
        # additionalModelRequestFields. Otherwise effort is provenance-only
        # (recorded in the metrics line by the base) and does not fail.
        if effort is not None:
            additional_fields = self._map_effort_to_model_fields(effort)
            if additional_fields:
                kwargs["additionalModelRequestFields"] = additional_fields

        return client.converse(**kwargs)

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

        The mapping is best-effort and never raises — an unrecognized model or
        effort level records effort as provenance only (Req 11.2).
        """
        model_lower = self.model.lower()
        effort_lower = effort.lower()

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
                return (
                    {
                        "inputTokens": usage_block.get("input_tokens", 0),
                        "outputTokens": usage_block.get("output_tokens", 0),
                        "totalTokens": (
                            usage_block.get("input_tokens", 0)
                            + usage_block.get("output_tokens", 0)
                        ),
                    },
                    "real",
                )
            return self._warn_missing_usage("InvokeModel response missing usage")

        # Standard Converse response path.
        usage_block = response.get("usage") if isinstance(response, dict) else None
        if usage_block and isinstance(usage_block, dict):
            # Bedrock auto-caches long system prompts: inputTokens then covers
            # only the uncached remainder and the bulk is reported as cache
            # reads/writes. Carry them so cost accounting sees the real input.
            return (
                {
                    "inputTokens": usage_block.get("inputTokens", 0),
                    "outputTokens": usage_block.get("outputTokens", 0),
                    "totalTokens": usage_block.get("totalTokens", 0),
                    "cacheReadInputTokens": usage_block.get("cacheReadInputTokens", 0),
                    "cacheWriteInputTokens": usage_block.get("cacheWriteInputTokens", 0),
                },
                "real",
            )

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
