"""Bedrock Converse adapter with native system prompts and metered structured output.

Nova uses forced tool output, other current models use auto tool output.
Only supported current reasoning controls are sent. No InvokeModel fallback.
Token usage is captured before output parsing so failed responses are billable."""

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

#: Converse ``serviceTier.type`` values (API_runtime_ServiceTier.html).
SERVICE_TIERS = frozenset({"default", "flex", "priority", "reserved"})

#: Name of the single tool offered for schema-constrained output.
STRUCTURED_OUTPUT_TOOL = "structured_output"

# Nova 2 ``reasoningConfig`` per effort level. ``low`` (and no effort) keeps
# extended thinking off — the skim stage's setting; ``max`` has no Nova
# equivalent and maps to ``high``.
_NOVA2_REASONING_EFFORTS = {"medium": "medium", "high": "high", "max": "high"}


class BedrockInvoker(DirectApiInvoker):
    """Direct Bedrock Converse execution. All models require an explicit ID."""

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


    def _structured_output_mode(self) -> str:
        """Nova supports forced tool choice; current other models use automatic choice."""
        if self._is_nova():
            return "forced_tool"
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
        """Offer the requested schema as a forced Nova tool or an automatic tool."""
        schema = adapter_opts.get("schema")
        if schema is None:
            return None

        mode = self._structured_output_mode()
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
        """Build and execute one Converse request with distinct system and user messages."""
        client = self._get_client(timeout)
        messages = [{"role": "user", "content": [{"text": user_message}]}]
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
        """Use current Nova, OpenAI and Claude 5 reasoning controls. Older models retain effort as provenance only."""
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

        return None  # Older generations retain effort as provenance only.

    def _unrecognized_effort(self, effort: str) -> None:
        logger.debug(
            "Unrecognized effort level %r for model %r; recording as provenance only.",
            effort, self.model,
        )
        return None

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Provider hook: extract final text from response                     #
    # ------------------------------------------------------------------ #
    def _extract_raw(self, response: Any) -> str:
        """Extract native tool output or text; reject truncated completions."""
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
