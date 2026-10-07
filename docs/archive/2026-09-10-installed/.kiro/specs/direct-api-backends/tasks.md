# Implementation Plan

## Overview

Add the Direct-API backend family and its first adapter on top of the existing
backend scaffold: a shared `DirectApiInvoker` base (`src/backends/direct_api.py`)
and `BedrockInvoker` (`src/backends/bedrock.py`, boto3 `bedrock-runtime` Converse),
registered in the resolver's `DEFAULT_ADAPTERS`. The work is sequenced so the
green baseline is captured first, the backend-agnostic metrics helper is hoisted
behavior-identically (proving the default-preserving guarantee), then the base
and the adapter are built and tested with a mocked/injected client, then
registered, then verified (mocked suite + one live Bedrock call).

Per `docs/MODEL-BACKENDS.md` and the design: Converse is the default request path
(`InvokeModel` is an isolated fallback seam); the boto3 client is injectable so no
live AWS call or credentials are needed for unit tests; the embedding path is
untouched.

## Task Dependency Graph

```
1 (baseline)
└─> 2 (hoist _write_metrics_line + METRICS_DIR to base.py; re-export; suite green)
    └─> 3 (DirectApiInvoker base: tools guard, metrics, parse backstop, tolerate-and-warn)
        └─> 4 (BedrockInvoker)
            ├─> 4.1 (client + Converse request construction + Invoker conformance)
            ├─> 4.2 (final-text extraction + native structured output + parser reuse)
            ├─> 4.3 (real usage capture + tolerate-and-warn + effort provenance)
            ├─> 4.4 (failure-mode mapping / parity)
            └─> 4.5 (mocked-client unit tests + DirectApiInvoker base tests)
                └─> 5 (register bedrock in resolver + registration/guard tests)
                    └─> 6 (live Bedrock verification + docs)
                        └─> 7 (full-suite verify + doc status)
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "rationale": "Capture the green baseline before any change." },
    { "wave": 2, "tasks": ["2"], "rationale": "Hoist the shared metrics helper behavior-identically; the all-Kiro suite must stay green." },
    { "wave": 3, "tasks": ["3"], "rationale": "Build the Direct-API base both the adapter and future direct adapters depend on." },
    { "wave": 4, "tasks": ["4.1"], "rationale": "Client + Converse request construction + Invoker conformance." },
    { "wave": 5, "tasks": ["4.2", "4.3"], "rationale": "Final-text/structured-output extraction and usage capture; independent on top of 4.1." },
    { "wave": 6, "tasks": ["4.4"], "rationale": "Failure-mode mapping / parity across the response and exception paths." },
    { "wave": 7, "tasks": ["4.5"], "rationale": "Mocked-client unit tests for the adapter and base." },
    { "wave": 8, "tasks": ["5"], "rationale": "Register bedrock in DEFAULT_ADAPTERS + resolver/guard tests." },
    { "wave": 9, "tasks": ["6"], "rationale": "Live Bedrock verification + verification doc." },
    { "wave": 10, "tasks": ["7"], "rationale": "Full-suite verification and doc status update." }
  ]
}
```

## Tasks

- [x] 1. Capture the green test baseline
  - Run `.venv/bin/python -m pytest -q` and record the passing count on the
    `laptop` (all-Kiro) profile, so the default-preserving guarantee can be
    verified after the metrics-helper hoist and the new modules.
  - _Requirements: 10.1, 10.2_

- [x] 2. Hoist the backend-agnostic metrics helper to `base.py`
  - Move `_write_metrics_line` and `METRICS_DIR` from
    `src/backends/agentic_cli.py` into `src/backends/base.py` (they are
    backend-agnostic, like `parse_json_output`). Keep `agentic_cli.py` and
    `kiro.py` working unchanged by re-importing / re-exporting them, so the
    existing monkeypatch targets (`src.backends.kiro.METRICS_DIR`, read at call
    time by `KiroInvoker._metrics_dir()`) and the `src/agent_invoker.py` shim are
    unaffected.
  - Re-run the full suite on the `laptop` profile; it MUST pass unchanged.
  - _Requirements: 2.4, 10.1, 10.2, 10.3_

- [x] 3. Build the `DirectApiInvoker` base
  - Create `src/backends/direct_api.py` with a template-method `invoke()` that:
    raises `ValueError` immediately when `tools=True` (the family is tool-less —
    no provider call, no MCP probe ever constructed); initializes the per-call
    metrics dict and writes it (via the hoisted writer, carrying `usage_source`
    and real token counts when present); times the provider call; runs the shared
    `parse_json_output` backstop and raises `ValueError` when nothing is
    recoverable; and builds the `InvocationResult` (`{output, raw, usage,
    usage_source}`). Add a `_warn_missing_usage(...)` helper (loud warning,
    returns `(None, "estimate")`, never raises).
  - Expose the provider hooks adapters override: `_invoke_provider`,
    `_extract_raw`, `_extract_usage`, `_map_exception`, and the
    structured-output shaping hook. Keep the base provider-neutral so a future
    `OpenAIInvoker`/`AnthropicInvoker` adds by overriding the same hooks.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.2, 3.3, 6.2, 6.3, 8.1, 8.3_

- [ ] 4. Implement `BedrockInvoker`
- [x] 4.1 Client + Converse request construction + Invoker conformance
  - Create `src/backends/bedrock.py` with `BedrockInvoker(model=<id>, *,
    region=None, client=None, max_tokens=4096, max_retries=4,
    system_prompt_delivery="native")` driving `DirectApiInvoker` and implementing
    the `Invoker` signature. Reject a blank model id at construction (metered
    backend). Build a lazy `boto3.client("bedrock-runtime", ...)` singleton
    (mirroring `src/embeddings.py`) with `botocore Config(region_name=<env
    BEDROCK_REGION default us-east-1>, retries={"max_attempts": max_retries,
    "mode": "adaptive"}, read_timeout=<call timeout>, connect_timeout=10)`, and
    accept an injected `client` for tests. Construct the Converse request:
    `modelId=self.model`, native `system=[{"text": system_prompt}]`,
    `messages=[{"role":"user","content":[{"text": user_message}]}]`,
    `inferenceConfig={"maxTokens": ...}`. Keep `user_message` distinct from the
    system prompt; add the `prepend` system-prompt fallback for a model that
    rejects a native system field.
  - _Requirements: 1.1, 1.2, 1.5, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 12.1, 12.3_
- [x] 4.2 Final-text extraction, native structured output, parser reuse
  - Extract the final text by concatenating the `text` blocks of
    `output.message.content`, then parse via the shared `parse_json_output`
    (Req 8.1). When a stage requests schema-constrained output, request it via the
    Converse `toolConfig` (single `ToolSpecification` with the stage schema as
    `inputSchema`, `strict=true`, `toolChoice` pinned), prefer the resulting
    `toolUse.input` payload, and fall back to `parse_json_output`; no recoverable
    JSON → `ValueError`. Provide the isolated InvokeModel fallback seam
    (`_invoke_provider`/`_extract_raw` override) for a model Converse does not
    serve.
  - _Requirements: 1.4, 4.5, 8.1, 8.2, 8.3_
- [x] 4.3 Real usage capture, tolerate-and-warn, effort provenance
  - Populate `usage` from the Converse response `usage`
    (`inputTokens`/`outputTokens`/`totalTokens`) and set `usage_source="real"`;
    fold the real counts into the metrics line (omit `total_cost_usd` — Bedrock
    returns no per-call cost). On a parseable response missing `usage`,
    tolerate-and-warn (keep `output`/`raw`, `usage=None`, `usage_source=
    "estimate"`, loud warning, no raise). Record `effort` as provenance in the
    metrics line; where the model exposes a native reasoning control, map `effort`
    onto `additionalModelRequestFields`; otherwise provenance-only without
    failing.
  - _Requirements: 1.3, 6.1, 6.2, 6.3, 6.4, 11.1, 11.2, 11.3_
- [-] 4.4 Failure-mode mapping / parity
  - Implement `_map_exception`: client `ReadTimeoutError` and a persistent
    `ModelTimeoutException` → `TimeoutError`; `ThrottlingException` /
    `ServiceUnavailableException` / `InternalServerException` (after the adaptive
    retries) / `ValidationException` / `AccessDeniedException` / any other
    `botocore` `ClientError` → `RuntimeError`, with the non-transient
    (`ValidationException`/`AccessDeniedException`) errors not retried. Never
    return an empty `InvocationResult` or a fabricated verdict.
  - _Requirements: 3.1, 7.1, 7.2, 7.3, 7.4_
- [~] 4.5 Mocked-client unit tests for BedrockInvoker and the base
  - Create `tests/test_bedrock.py`. Inject a fake `bedrock-runtime` client (no
    `boto3`/`moto`/creds). Cover: Converse request construction (modelId, native
    `system`, `messages`, `inferenceConfig`); the `prepend` system-prompt
    fallback; `tools=True` rejection; final-text extraction from
    `output.message.content`; native structured output via `toolConfig`+`strict`
    and the `parse_json_output` fallback; usage capture and the tolerate-and-warn
    fallback; effort provenance; and every failure-mode row (each botocore
    exception → the mapped Python exception, non-transient not retried). Add a
    tiny fake `DirectApiInvoker` subclass test exercising the base's tools guard,
    metrics emission, parse backstop, and tolerate-and-warn independently of
    Bedrock.
  - _Requirements: 14.1, 14.3, 2.1, 2.2_

- [~] 5. Register the Bedrock adapter in the resolver
  - In `src/backends/resolver.py`, import `BedrockInvoker` and add it to
    `DEFAULT_ADAPTERS` (`"bedrock"`). Add tests asserting a profile selecting
    `bedrock` for a tool-less role resolves to `BedrockInvoker` (not
    `NotImplementedError`) and caches per `(backend, model)`, and that a profile
    assigning the Explorer to `bedrock` still fails fast at resolver construction
    (the existing eager `assert_backend_supports_role` guard, unchanged).
  - _Requirements: 9.1, 9.2, 9.3_

- [~] 6. Live Bedrock verification + documentation
  - Run one tool-less, evaluator-style Converse call against real Bedrock
    (credentials already present for the embedding path) confirming real `usage`
    capture and `parse_json_output` of a real `output.message.content` payload.
    Record it as **Part C** of `docs/MODEL-BACKENDS-VERIFICATION.md` (alongside
    the Claude Code / Codex parts), and document the required IAM permissions
    (`bedrock:InvokeModel`, plus `bedrock:GetInferenceProfile` for inference
    profiles), the region/profile env vars, and the data-governance note (Bedrock
    is metered, sends content under AWS governance and does not store it,
    cross-region data-residency caveat).
  - _Requirements: 12.2, 13.1, 13.2, 14.2_

- [~] 7. Final verification and documentation
  - Run the full suite (`.venv/bin/python -m pytest`) on the `laptop` profile
    (zero behavior change vs the Task 1 baseline) and confirm the new
    `bedrock`/`direct_api`/resolver tests pass. Update `docs/MODEL-BACKENDS.md`
    status (Bedrock Direct-API adapter implemented; the Direct-API family base
    exists; OpenAI/Anthropic direct and per-role routing still deferred). No
    embedding-path changes.
  - _Requirements: 10.1, 10.2, 10.3_

## Notes

- **Behavior preservation** (Tasks 1, 2, 7) brackets the metrics-helper hoist to
  prove the all-Kiro path is unchanged.
- **No live AWS in CI:** adapter unit tests inject a fake `bedrock-runtime`
  client; only Task 6 makes a real call, and it is a manual/recorded verification.
- **Explorer guard preserved:** no change to `assert_backend_supports_role` or
  `BACKEND_CAPABILITIES` (`bedrock` is already declared `supports_mcp=False`).
- **Out of scope (deferred):** direct `OpenAIInvoker`/`AnthropicInvoker` (the base
  is designed for them), per-role routing and a blessed mixed profile (so a whole
  live dream-cycle run on Bedrock is gated on that + the data-governance gate),
  evaluator-panel model diversity, and any embedding-path change. AgentCore and
  Strands are recorded as Considered Alternatives in `requirements.md`.
