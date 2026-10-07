# Design Document

## Overview

The dream-cycle pipeline runs on a pluggable model-backend scaffold (`Invoker`
Protocol, `InvocationResult`, `BackendCapabilities` + `BACKEND_CAPABILITIES`,
`assert_backend_supports_role`, the shared `parse_json_output` backstop — all in
`src/backends/base.py`; the profile-driven `Resolver`; orchestrator wiring via
`_invoker_for(role)`). Every adapter shipped so far is an **agentic-CLI** backend
driving a subprocess through the shared `AgenticCliInvoker` base (`KiroInvoker`,
`ClaudeCodeInvoker`, `CodexInvoker`).

This feature adds the scaffold's intended **second family** and its first member:

- **`DirectApiInvoker`** (`src/backends/direct_api.py`, new) — the Direct-API
  analogue of `AgenticCliInvoker`. It owns the mechanics common to every
  tool-less provider call: the `tools=False` structural guard, the per-call
  metrics JSONL writer (with `usage_source`), the shared `parse_json_output`
  backstop, usage normalization, the tolerate-and-warn path, and the
  infrastructure-failure-always-raises cardinal rule. It performs **no**
  subprocess and **no** MCP probe.
- **`BedrockInvoker`** (`src/backends/bedrock.py`, new) — the first concrete
  Direct-API adapter, wrapping the Amazon Bedrock `bedrock-runtime` **Converse**
  API via `boto3`. Registered in `DEFAULT_ADAPTERS` as `bedrock`.

`bedrock` is already declared in `BACKEND_CAPABILITIES`
(`supports_mcp=False, metered=True, structured_output=True, reports_usage=True`),
so no capability or guard change is needed: routing the Explorer to `bedrock`
already fails fast, and that rejection is **preserved**. `KiroInvoker` remains the
reference whose observable behavior (return shape and the exceptions it raises)
the new adapter matches, because the orchestrator's evaluator retry/abort logic
depends on that contract being identical across backends.

The change is **default-preserving**: with the `laptop` profile (all-Kiro), the
existing suite must pass unchanged (Req 10). Verified against the AWS Converse API
reference, the Bedrock retry/backoff guidance, and the inference-profile
prerequisites (see `requirements.md` Introduction).

### Scope decision (carried from the requirements review)

This feature delivers `BedrockInvoker` **registered, contract-conformant, and
verified** (mocked + one live call), with the Explorer guard preserved. It does
**not** add per-role routing or a blessed mixed profile, so running a *whole*
live dream cycle on Bedrock (tool-less roles on Bedrock, the agentic Explorer on
a CLI backend) remains gated on the deferred per-role-routing feature and the
data-governance gate. The resolver already reads per-role specs, so a future
mixed profile is config-only; an illustrative example is given under *Selection &
configuration* but is not part of this feature's scope.

## Architecture

### Before

```
                         src/backends/base.py
        Invoker · InvocationResult · BACKEND_CAPABILITIES · parse_json_output
                                   │
                        AgenticCliInvoker (subprocess base)
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                       ▼
        KiroInvoker         ClaudeCodeInvoker         CodexInvoker
                                   │
   DEFAULT_ADAPTERS = { kiro, claude_code, codex }     (bedrock → NotImplementedError)
```

### After

```
                         src/backends/base.py
   Invoker · InvocationResult · BACKEND_CAPABILITIES · parse_json_output
                   · _write_metrics_line / METRICS_DIR (hoisted, shared)
                                   │
        ┌──────────────────────────┴───────────────────────────┐
        ▼                                                        ▼
  AgenticCliInvoker (subprocess base)                  DirectApiInvoker (API base)   ← new
   tools attach + MCP probe                             tools=False guard, no probe
   ┌───────────┬───────────────┬───────────┐                    │
   ▼           ▼               ▼            │                    ▼
 Kiro     ClaudeCode        Codex          │               BedrockInvoker          ← new
                                           │            boto3 bedrock-runtime.converse
   shared: metrics JSONL (usage_source) · parse_json_output backstop ·
           usage normalize · tolerate-and-warn · "infra failure always raises"
                                   │
   DEFAULT_ADAPTERS = { kiro, claude_code, codex, bedrock }   ← only change in resolver.py
                                   │
            orchestrator._invoker_for(role)  ← unchanged; resolves per active profile
```

### Why a sibling base rather than reusing `AgenticCliInvoker`

`AgenticCliInvoker`'s template is built around a subprocess lifecycle (build
command/config → spawn → timeout/exit mapping → MCP probe → temp-file cleanup).
A Direct-API call shares **none** of that shape: there is no command, no
subprocess, no temp config, no MCP, and timeout is a client read-timeout rather
than a killed process. What the two families genuinely share is narrower — the
metrics JSONL writer, the `parse_json_output` backstop, the usage normalization /
tolerate-and-warn policy, and the "infra failure always raises" rule. Forcing
Bedrock through the CLI template would mean stubbing out most hooks and
misrepresenting the lifecycle. So the shared pieces are hoisted to be
family-agnostic, and each family gets its own thin base.

### Shared-helper hoist (minimal, back-compat-preserving)

`_write_metrics_line` and `METRICS_DIR` currently live in
`src/backends/agentic_cli.py`. They are genuinely backend-agnostic (like
`parse_json_output`, already in `base.py`). They are **hoisted to
`src/backends/base.py`**, and `agentic_cli.py` and `kiro.py` re-import / re-export
them unchanged, so:

- existing monkeypatch targets keep working — the test suite and launchd env
  override `src.backends.kiro.METRICS_DIR`, and `KiroInvoker._metrics_dir()` reads
  that module global at call time (unchanged);
- `src/agent_invoker.py`'s back-compat re-exports are unaffected;
- `DirectApiInvoker` imports the writer from `base.py`, never from a CLI module.

This hoist is behavior-preserving and is validated by the existing full suite
staying green (Req 10).

### Import safety

`src/backends/direct_api.py` imports only from `src/backends/base.py` (+ stdlib).
`src/backends/bedrock.py` imports `direct_api`, `base`, and `boto3`/`botocore`.
`resolver.py` adds an import of `BedrockInvoker`. No import cycle:
`base ← direct_api ← bedrock ← resolver`, the same layering the CLI adapters use.

## Components and Interfaces

### 1. `DirectApiInvoker` (`src/backends/direct_api.py`, new)

A template-method base mirroring `AgenticCliInvoker`'s structure but for one
tool-less API call. Implements the `Invoker` contract via a shared `invoke()`
that orchestrates: tools-guard → metrics init → timed provider call → failure
mapping → raw extraction → parse backstop → usage extraction (tolerate-and-warn)
→ metrics record → return `InvocationResult`.

The base owns:

- **`invoke(...)`** — the template. Its first action is the **tools guard**: if
  `tools` is `True`, raise `ValueError` immediately (the Direct-API family is
  tool-less by definition — Req 3.2). It never constructs an MCP probe (Req 3.3).
- **`_record_metrics(...)`** — the JSONL writer hoisted to `base.py`, extended to
  carry `usage_source` and real token counts when present (same record shape as
  the CLI adapters — Req 2.4).
- **`_finalize(raw, *, usage, usage_source)`** — run `parse_json_output`, raise
  `ValueError` if nothing recoverable (Req 8.3), build the `InvocationResult`.
- **`_warn_missing_usage(...)`** — the tolerate-and-warn helper: log a loud
  warning, return `(None, "estimate")`, never raise (Req 6.2, 6.3).

Hook methods adapters override:

| Hook | Responsibility |
|---|---|
| `_invoke_provider(*, system_prompt, user_message, effort, timeout, structured_schema)` | Build and execute the provider call; return the provider's raw response object. |
| `_extract_raw(response)` | The model's final text (for the parse backstop). |
| `_extract_usage(*, response, parsed, metrics)` | Return `(usage, usage_source)`; tolerate-and-warn when absent. |
| `_map_exception(exc)` | Map a provider/SDK exception onto `TimeoutError` / `RuntimeError` (the failure-mode-parity seam). |
| `_supports_native_structured_output()` / structured shaping | Whether/how to request schema-constrained output. |

The base is deliberately provider-neutral so a later `OpenAIInvoker` /
`AnthropicInvoker` (direct, non-Bedrock) is added by overriding the same hooks,
without modifying the base (Req 2.3).

### 2. `BedrockInvoker` (`src/backends/bedrock.py`, new)

Constructed as `BedrockInvoker(model=<id>)` (Req 1.2). Same `invoke` signature as
every `Invoker`. A metered backend, so a blank model id is rejected at
construction (mirrors `ClaudeCodeInvoker`/`CodexInvoker`).

```python
class BedrockInvoker(DirectApiInvoker):
    BACKEND_NAME = "bedrock"
    DEFAULT_MODEL = ""  # metered → must be set explicitly

    def __init__(self, model=None, *, region=None, client=None,
                 max_tokens=4096, max_retries=4,
                 system_prompt_delivery="native"):
        ...
```

**Client construction (Req 4.2, 12).** A lazy `boto3.client("bedrock-runtime",
...)` singleton (mirroring `src/embeddings.py`), built with a `botocore` `Config`
that sets the retry mode and the read timeout from the call's `timeout`:

```python
Config(
    region_name=self._region,                       # BEDROCK_REGION, default us-east-1
    retries={"max_attempts": self._max_retries, "mode": "adaptive"},
    read_timeout=timeout, connect_timeout=10,
)
```

`adaptive` mode applies AWS's recommended client-side throttle handling
(exponential backoff + jitter + rate limiting) for the transient classes
(`ThrottlingException`, `ServiceUnavailableException`, `InternalServerException`,
`ModelTimeoutException`) without a hand-rolled decorator (Req 7.2). Credentials
come from the standard `boto3` chain — never from config (Req 12.1). A `client`
may be injected at construction so unit tests pass a fake (no `moto`, no creds —
Req 14.3).

**Request construction — Converse (Req 4).**

```python
client.converse(
    modelId=self.model,                              # base id or inference-profile id
    system=[{"text": system_prompt}],                # native system (Req 5.1)
    messages=[{"role": "user", "content": [{"text": user_message}]}],
    inferenceConfig={"maxTokens": self._max_tokens, ...},
    toolConfig=<single strict tool>,                 # only when structured output requested
    additionalModelRequestFields=<effort/reasoning>, # when the model exposes it
)
```

- The Converse API is the provider-agnostic default (Req 4.1) and needs only the
  `bedrock:InvokeModel` IAM permission; an inference-profile id (`us.` / `global.`
  prefix) is accepted directly as `modelId` (+`bedrock:GetInferenceProfile`).
- **System prompt** is delivered natively via the `system` field (Req 5.1), kept
  distinct from `messages` (Req 5.2). A `prepend` fallback folds it into the user
  message for any model that rejects a system field (Req 5.3), covered by a test.
- **InvokeModel fallback (Req 4.5):** for a model the Converse API does not serve,
  the per-provider request/response body is isolated behind an override of
  `_invoke_provider` / `_extract_raw` / `_extract_usage`; Converse stays the
  default path. Not exercised by the default Bedrock models, but the seam exists.

**Final-text extraction (Req 8).** From the Converse response,
`output.message.content` is a list of blocks; concatenate the `text` blocks for
the final text, then run the shared `parse_json_output` backstop (Req 8.1). When
native structured output was requested, the payload is the `toolUse.input` of the
tool block — preferred directly, with `parse_json_output` as the fallback
(Req 8.2). No recoverable JSON → `ValueError` (Req 8.3).

**Native structured output (Req 8.2).** Requested via the Converse `toolConfig`
with a single `ToolSpecification` carrying the stage's JSON schema as
`inputSchema` and `strict=true` (forcing schema-valid tool input), plus
`toolChoice` pinned to that tool. This is the AWS-documented mechanism for
schema-constrained output and maps cleanly to the dense Thinker / strict-JSON
evaluator schemas. The `parse_json_output` backstop is retained regardless.

**Real usage (Req 6 — tolerate-and-warn).** The Converse response carries
`usage: {inputTokens, outputTokens, totalTokens}`. On success, populate `usage`
from it and set `usage_source="real"`; fold the real counts into the metrics line
(Req 6.1). Bedrock does not return a per-call USD cost, so `total_cost_usd` is
omitted (cost is derived downstream from pricing, not fabricated here). On a
parseable response missing `usage` (not expected with Converse, but handled for
parity), tolerate-and-warn: keep `output`/`raw`, `usage=None`,
`usage_source="estimate"`, loud warning, no raise (Req 6.2, 6.3).

**Effort (Req 11).** Recorded as provenance in the metrics line regardless
(Req 11.1). Where the target model exposes a native reasoning control (e.g.
Anthropic extended-thinking via `additionalModelRequestFields`), `effort` is
mapped onto it; otherwise it is provenance-only and does not fail (Req 11.2). No
cross-backend equivalence is assumed (Req 11.3).

**Failure-mode mapping (Req 7) — the parity seam.** `_map_exception` translates:

| Bedrock / botocore condition | Mapped to | Retried first? |
|---|---|---|
| `ReadTimeoutError` (client read timeout) | `TimeoutError` | no (botocore connect retries only) |
| `ModelTimeoutException` persisting after retries | `TimeoutError` | yes (adaptive) |
| `ThrottlingException` / `ServiceUnavailableException` / `InternalServerException` after retries | `RuntimeError` | yes (adaptive) |
| `ValidationException` / `AccessDeniedException` | `RuntimeError` | **no** (non-transient) |
| any other `botocore` `ClientError` | `RuntimeError` | per botocore |

Never an empty `InvocationResult` or a fabricated verdict (Req 7.4) — the cardinal
rule the base enforces and `_map_exception` upholds.

### 3. Resolver registration (`src/backends/resolver.py`)

The only resolver change is registration (Req 9):

```python
from src.backends.bedrock import BedrockInvoker

DEFAULT_ADAPTERS = {
    "kiro": KiroInvoker,
    "claude_code": ClaudeCodeInvoker,
    "codex": CodexInvoker,
    "bedrock": BedrockInvoker,
}
```

Everything else already works unchanged:

- per-`(backend, model)` invoker caching (Req 9.2);
- the **eager** Explorer guard at construction + the lazy guard in `invoker_for`
  both reject `explorer → bedrock` because `bedrock` declares
  `supports_mcp=False`, so a whole-instance Bedrock profile fails fast at config
  load (Req 9.3);
- `_validate_profile` already accepts `bedrock` (it is in `BACKEND_CAPABILITIES`).

### Selection & configuration (illustrative — not in scope)

`bedrock` profile entries carry only the model id + effort, never credentials
(Req 12.3). Because Bedrock is tool-less, a *usable* profile must keep the
Explorer (and the tool-using Thinker) on an agentic backend and place only the
tool-less roles on Bedrock — i.e. per-role routing, which is deferred:

```toml
# Illustrative ONLY — a mixed profile is gated on the deferred per-role-routing
# feature and the data-governance gate. Not shipped by this feature.
[profiles.bedrock-evaluators]
explorer       = { backend = "claude_code", model = "global.anthropic.claude-opus-4-8", effort = "high" }
thinker        = { backend = "claude_code", model = "global.anthropic.claude-opus-4-8", effort = "max" }
skeptic        = { backend = "bedrock", model = "us.anthropic.claude-haiku-4-5", effort = "high" }
# ... advocate / epistemologist / methodologist = bedrock
```

## Data Models

No database or schema changes. The data shapes involved are existing:

- **`InvocationResult`** (`{output, raw, usage, usage_source}`) — unchanged shape;
  `BedrockInvoker` populates real `usage` (`inputTokens`/`outputTokens`/
  `totalTokens`) with `usage_source="real"`, or falls back to
  `None`/`"estimate"` (tolerate-and-warn).
- **Per-call metrics JSONL** (`logs/llm_metrics/<run_id>.jsonl`) — same record
  shape as the CLI adapters; Bedrock records `usage_source="real"` and the real
  token counts (`real_input_tokens` / `real_output_tokens`), keeping
  cross-backend cost analysis honest (Req 2.4).
- **Backend provenance** — the resolved `(backend, model, effort)` is already
  recorded per run/candidate (the `feat/backends-provenance` merge); `bedrock`
  flows through that path unchanged, no new field.
- **Converse request/response** (transient, not persisted) — request:
  `modelId`, `system`, `messages`, `inferenceConfig`, optional `toolConfig` /
  `additionalModelRequestFields`; response: `output.message.content[]`,
  `stopReason`, `usage`, `metrics.latencyMs`.

## Correctness Properties

Executable properties the implementation must satisfy (validated by the suite):

### Property 1: Default-preserving
With the `laptop` profile, every role resolves to `KiroInvoker`, and the full
existing suite passes unchanged (including after the metrics-helper hoist).
**Validates: Requirements 10.1, 10.2, 10.3**

### Property 2: Contract conformance
`BedrockInvoker` satisfies the `Invoker` signature and returns an
`InvocationResult` with exactly `{output, raw, usage, usage_source}`.
**Validates: Requirements 1.1, 1.3, 1.4**

### Property 3: Tool-less enforcement
`invoke(tools=True)` raises `ValueError` (or a clear exception) without any
provider call; `tools=False` performs the call; no MCP probe is ever constructed;
the Explorer→`bedrock` guard still fails fast at resolver construction.
**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 9.3**

### Property 4: Real usage + tolerate-and-warn
A successful Converse response with `usage` yields `usage_source="real"` and the
normalized counts; a parseable response missing `usage` yields `usage=None`,
`usage_source="estimate"`, a warning, and **no raise**.
**Validates: Requirements 6.1, 6.2, 6.3**

### Property 5: Failure-mode parity
Read timeout / `ModelTimeoutException` → `TimeoutError`; `ThrottlingException`/
`ServiceUnavailableException`/`InternalServerException` (post-retry),
`ValidationException`, `AccessDeniedException`, other `ClientError` → `RuntimeError`;
non-transient errors are not retried. Never an empty result or fabricated verdict.
**Validates: Requirements 7.1, 7.2, 7.3, 7.4**

### Property 6: System-prompt delivery
The system prompt is sent in the Converse `system` field, distinct from
`messages`; the `prepend` fallback path is covered by a test.
**Validates: Requirements 5.1, 5.2, 5.3**

### Property 7: Parser reuse & structured output
The final text is parsed via the shared `parse_json_output`; when native
structured output is requested the schema-constrained `toolUse.input` is preferred
with `parse_json_output` as fallback; no recoverable JSON → `ValueError`.
**Validates: Requirements 8.1, 8.2, 8.3**

### Property 8: Resolver registration
A profile assigning a tool-less role to `bedrock` resolves to `BedrockInvoker`
(not `NotImplementedError`), cached per `(backend, model)`.
**Validates: Requirements 9.1, 9.2**

### Property 9: Effort is provenance
Effort is recorded in the metrics line; where supported it maps to a native
reasoning control; no cross-backend equivalence assumed; absence of native
support does not fail.
**Validates: Requirements 11.1, 11.2, 11.3**

### Property 10: Direct-API base reusability
The `tools=False` guard, metrics, parse backstop, and tolerate-and-warn live in
`DirectApiInvoker`; `BedrockInvoker` overrides only provider hooks (verified by a
minimal fake `DirectApiInvoker` subclass in tests exercising the base).
**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

## Error Handling

| Failure | BedrockInvoker | Shared mapping |
|---|---|---|
| `tools=True` requested | raise `ValueError` before any call | base tools guard (Req 3.2) |
| Client read timeout (`ReadTimeoutError`) | `TimeoutError` | `_map_exception` (Req 7.1) |
| `ModelTimeoutException` after retries | `TimeoutError` | `_map_exception` |
| `ThrottlingException` / `ServiceUnavailableException` / `InternalServerException` after retries | `RuntimeError` | adaptive retry, then `_map_exception` (Req 7.2) |
| `ValidationException` / `AccessDeniedException` | `RuntimeError`, no retry | `_map_exception` (Req 7.3) |
| Any other `botocore` `ClientError` | `RuntimeError` | `_map_exception` (Req 7.4) |
| Response missing `usage` (parseable) | tolerate: `usage=None`/`"estimate"`/warn | `_warn_missing_usage` (NOT an error, Req 6.2) |
| No recoverable JSON in final text | `ValueError` | `parse_json_output` (Req 8.3) |
| Empty/blank model id | construction-time `ValueError` | adapter validates `model` |

The cardinal rule (identical to the CLI adapters): an **infrastructure** failure
always raises; a **telemetry** gap (missing usage) never does. This preserves the
evaluator retry/abort integrity — a crashed evaluator must never become a
fabricated REJECT.

## Testing Strategy

1. **Baseline / default-preserving** — run the full suite on the `laptop` profile
   before and after the metrics-helper hoist; it must pass unchanged (Property 1).
2. **Mocked-client unit tests (`tests/test_bedrock.py`)** — inject a fake
   `bedrock-runtime` client (no `boto3` call, no creds, no `moto`). Cover: Converse
   request construction (modelId, native `system`, `messages`, `inferenceConfig`);
   the `prepend` system-prompt fallback; `tools=True` rejection; final-text
   extraction from `output.message.content`; usage capture from `usage` and the
   tolerate-and-warn fallback; native structured output via `toolConfig`+`strict`
   and the `parse_json_output` fallback; and every failure-mode row (each botocore
   exception → the mapped Python exception, with non-transient errors not retried).
3. **Direct-API base tests** — a tiny fake `DirectApiInvoker` subclass exercises
   the base: the `tools=True` guard, metrics emission, the parse backstop, and
   tolerate-and-warn, independent of Bedrock (Property 10).
4. **Shared-parser tests** — feed a representative Bedrock final text
   (prose-wrapped, fenced, schema `toolUse.input`) through `parse_json_output`;
   reuse the existing property tests.
5. **Resolver/guard registration tests** — a profile selecting `bedrock` for a
   tool-less role resolves to `BedrockInvoker` and caches per `(backend, model)`;
   `explorer → bedrock` still fails fast at construction.
6. **Live verification — real Bedrock** — one tool-less, evaluator-style Converse
   call against real Bedrock (credentials already present for the embedding path),
   confirming real `usage` capture and `parse_json_output` of a real `.content`
   payload. Recorded as **Part C** of `docs/MODEL-BACKENDS-VERIFICATION.md`,
   alongside the Claude Code / Codex parts.

Negative check: force each botocore exception (via the injected client) and
confirm the mapped Python exception propagates (never a verdict), then confirm a
`usage`-less-but-parseable response does **not** raise.

## Scope Boundaries (carried from requirements)

- **In:** `DirectApiInvoker` base + `BedrockInvoker` (Converse default,
  InvokeModel-fallback seam), registered in `DEFAULT_ADAPTERS`, mocked + one live
  verification, Explorer guard preserved, default-preserving, metrics-helper
  hoist.
- **Out / deferred:** direct `OpenAIInvoker` / `AnthropicInvoker` (the base is
  designed for them, but they are not built here); per-role routing and a blessed
  mixed profile (so a *whole* live dream-cycle run on Bedrock is gated on that +
  the data-governance gate); evaluator-panel model diversity; the embedding path
  (`src/embeddings.py`, unchanged); AgentCore and Strands (see *Considered
  Alternatives* in `requirements.md`).
