"""Shared base for direct-API backends (tool-less, single-shot provider calls).

Every direct-API backend (Bedrock, and later direct OpenAI/Anthropic) performs a
single tool-less provider API call — no subprocess, no MCP probe. The family is
structurally tool-less: ``tools=True`` is always rejected with a ``ValueError``
before any provider call is attempted.

:class:`DirectApiInvoker` owns the mechanics common to every tool-less provider
call:

* the ``tools=True`` structural guard (Req 3.2, 3.3)
* per-call metrics JSONL writer (with ``usage_source`` and real token counts
  when present — Req 2.4)
* timing the provider call
* failure-mode mapping via ``_map_exception`` (Req 7)
* the shared :func:`parse_json_output` backstop (Req 8.1, 8.3)
* usage normalization and the tolerate-and-warn path (Req 6.2, 6.3)
* building the :class:`InvocationResult` (``{output, raw, usage, usage_source}``)

Each concrete adapter (:class:`BedrockInvoker`, and later
:class:`OpenAIInvoker`/:class:`AnthropicInvoker`) overrides only the hooks that
genuinely differ per provider:

* :meth:`_invoke_provider` — call the actual provider API
* :meth:`_extract_raw` — extract the raw text from the provider response
* :meth:`_extract_usage` — extract token usage from the provider response
* :meth:`_map_exception` — map provider exceptions to standard Python exceptions
* :meth:`_shape_structured_output` — prepare structured-output request params

The base is provider-neutral: a future ``OpenAIInvoker`` or ``AnthropicInvoker``
adds by overriding the same hooks, without modifying the base (Req 2.3).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.backends.base import (
    InvocationBase,
    ModelRefusalError, assert_corpus_route, corpus_role_for_stage,
    InvocationResult,
    _write_metrics_line,
    parse_json_output,
)

logger = logging.getLogger(__name__)


class DirectApiInvoker(InvocationBase):
    """Template-method base for direct-API (tool-less) backends.

    Implements the :class:`src.backends.base.Invoker` contract via a shared
    :meth:`invoke` that orchestrates: tools-guard → metrics init → timed
    provider call → failure mapping → raw extraction → parse backstop → usage
    extraction (tolerate-and-warn) → metrics record → return
    :class:`InvocationResult`.

    Subclasses set :attr:`BACKEND_NAME` / :attr:`DEFAULT_MODEL` and override the
    hook methods that differ per provider. This class is NOT an ABC — adapters
    are concrete subclasses that override the hooks.
    """

    #: Short backend label, used in metrics and log lines.
    BACKEND_NAME: str = "direct_api"
    #: Default model id when constructed without an explicit ``model``.
    DEFAULT_MODEL: str = ""

    def __init__(self, model: str | None = None):
        self.model = model or self.DEFAULT_MODEL

    # The JSON recovery backstop is backend-agnostic and shared from base.py.
    parse_json_output = staticmethod(parse_json_output)

    # ------------------------------------------------------------------ #
    # Template method                                                    #
    # ------------------------------------------------------------------ #
    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        *,
        tools: bool = False,
        timeout: int = 300,
        effort: str | None = None,
        stage: str | None = None,
        run_id: str | None = None,
        **adapter_opts,
    ) -> InvocationResult:
        """Run one provider call and return a normalized :class:`InvocationResult`.

        The Direct-API family is tool-less by definition: ``tools=True`` raises
        ``ValueError`` immediately — no provider call, no MCP probe ever
        constructed.

        Raises:
            ValueError: If ``tools=True`` (the family cannot attach MCP tools).
            ValueError: If no JSON is recoverable from the final text.
            TimeoutError: If the provider call exceeds ``timeout``.
            RuntimeError: If the provider call fails (transport error, auth
                failure, throttling after retries, etc.).
        """
        corpus_role = corpus_role_for_stage(stage)
        if corpus_role:
            assert_corpus_route(self.BACKEND_NAME, self.model, corpus_role)

        # --- Tools guard (Req 3.2, 3.3) ---
        if tools:
            raise ValueError(
                f"Direct-API backend {self.BACKEND_NAME!r} is tool-less "
                f"(supports_mcp=False). Cannot invoke with tools=True."
            )

        # --- Metrics init ---
        metrics = self._init_metrics(
            system_prompt=system_prompt,
            user_message=user_message,
            timeout=timeout,
            effort=effort,
            stage=stage,
            run_id=run_id,
        )

        t0 = time.monotonic()
        try:
            # --- Structured-output shaping ---
            structured_schema = self._shape_structured_output(**adapter_opts)

            # --- Timed provider call ---
            try:
                response = self._invoke_provider(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    effort=effort,
                    timeout=timeout,
                    structured_schema=structured_schema,
                )
            except Exception as exc:
                mapped = self._map_exception(exc)
                if mapped is not exc:
                    raise mapped from exc
                raise

            # Usage belongs to the provider response, including refusals,
            # truncated output and malformed JSON. Extract before parsing.
            usage, usage_source = self._extract_usage(
                response=response, parsed=None, metrics=metrics
            )
            metrics["usage_source"] = usage_source
            if usage is not None:
                metrics["real_input_tokens"] = usage.get("inputTokens")
                metrics["real_output_tokens"] = usage.get("outputTokens")
            try:
                stop_reason = response.get("stopReason") if isinstance(response, dict) else None
                if stop_reason in {"content_filtered", "guardrail_intervened", "refusal"}:
                    raise ModelRefusalError(f"Provider refused completion ({stop_reason})")
                raw = self._extract_raw(response)
                metrics["output_chars"] = len(raw)
                parsed = self._parse(raw, metrics)
            except Exception as exc:
                # Keep the original exception type for existing callers. Stage
                # consumers can meter and classify returned contract failures.
                metrics["_usage"] = usage
                exc.model_response_received = True
                exc.usage = usage
                exc.usage_source = usage_source
                metrics["error"] = metrics.get("error") or type(exc).__name__
                raise
            metrics["_usage"] = usage
            metrics["success"] = True

            return InvocationResult(
                output=parsed,
                raw=raw,
                usage=usage,
                usage_source=usage_source,
            )

        finally:
            metrics["latency_s"] = round(time.monotonic() - t0, 3)
            self._record_metrics(metrics, run_id)

    # ------------------------------------------------------------------ #
    # Shared mechanics (the base owns these)                             #
    # ------------------------------------------------------------------ #





    # ------------------------------------------------------------------ #
    # Hooks adapters override                                            #
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
        """Build and execute the provider API call; return the raw response object.

        Adapters MUST override this. The base raises ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override _invoke_provider"
        )

    def _extract_raw(self, response: Any) -> str:
        """Extract the model's final text from the provider response.

        Adapters MUST override this. The base raises ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override _extract_raw"
        )

    def _extract_usage(
        self, *, response: Any, parsed: Any, metrics: dict
    ) -> tuple[Optional[dict], str]:
        """Return ``(usage, usage_source)`` from the provider response.

        Adapters override this to extract real token counts. The default
        implementation invokes :meth:`_warn_missing_usage` and returns
        ``(None, "estimate")``.
        """
        return self._warn_missing_usage("default _extract_usage (no override)")

    def _map_exception(self, exc: Exception) -> Exception:
        """Map a provider/SDK exception onto ``TimeoutError`` / ``RuntimeError``.

        Adapters override this to translate provider-specific exceptions into
        the standard failure-mode exceptions. The default returns the exception
        unchanged (re-raised as-is).
        """
        return exc

    def _shape_structured_output(self, **adapter_opts) -> Optional[dict]:
        """Prepare structured-output request parameters.

        Adapters override this when they support native schema-constrained output.
        Return a schema dict that ``_invoke_provider`` will use to shape the
        request, or ``None`` for no structured output.

        The default returns ``None`` (no structured output).
        """
        return None
