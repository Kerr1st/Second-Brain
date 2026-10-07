# Requirements Document

## Introduction

The Second Brain dream-cycle pipeline runs on a pluggable model-backend scaffold:
a common `Invoker` Protocol, an `InvocationResult` return shape, a
`BackendCapabilities` registry (`BACKEND_CAPABILITIES`), the
`assert_backend_supports_role` guard, and a shared `parse_json_output` backstop
(all in `src/backends/base.py`); a profile-driven `Resolver`
(`src/backends/resolver.py`); orchestrator wiring (`_invoker_for(role)`); and
three **agentic-CLI** adapters that drive a subprocess through the shared
`AgenticCliInvoker` base (`KiroInvoker`, `ClaudeCodeInvoker`, `CodexInvoker`).

Every adapter shipped so far belongs to the **agentic-CLI** family — it shells
out to a coding-assistant CLI that speaks MCP. The scaffold was always intended
to also support a second family: **Direct-API backends** — a single tool-less
provider API call rather than a subprocess. `BACKEND_CAPABILITIES` already
declares one such backend, `bedrock` (`supports_mcp=False`, `metered=True`,
`structured_output=True`, `reports_usage=True`), but no adapter implements it, so
any profile selecting `bedrock` raises `NotImplementedError`.

This feature delivers the Direct-API backend family and its first concrete
adapter:

- **`DirectApiInvoker`** — a shared base (the Direct-API analogue of
  `AgenticCliInvoker`) that owns the mechanics common to every tool-less provider
  call: the per-call metrics JSONL writer (with `usage_source`), the shared
  `parse_json_output` backstop, usage normalization, the tolerate-and-warn path
  for missing telemetry, failure-mode mapping onto the same exceptions
  `KiroInvoker` raises, and the `tools=False` enforcement that is structural to
  the family.
- **`BedrockInvoker`** — the first Direct-API adapter, wrapping the Amazon
  Bedrock `bedrock-runtime` reasoning API (the Converse API as the
  provider-agnostic default) as an `Invoker`.

`BedrockInvoker` is registered in the resolver's `DEFAULT_ADAPTERS` map so a
profile assigning a **tool-less** role (Thinker fed a pre-fetched packet,
evaluators, Express editor) to `bedrock` resolves to a working invoker. Bedrock
is tool-less by design, so the existing `assert_backend_supports_role` guard
continues to reject routing the **Explorer** to it; that rejection is preserved,
not removed.

The authoritative source of truth for the backend-family abstraction, the
capability matrix, the failure-mode-parity requirement, the tool-less /
Explorer-eligibility rules, and the cost/governance posture is
`docs/MODEL-BACKENDS.md`.

The Bedrock-specific requirements below were verified against the current AWS
documentation (consulted via the AWS docs MCP, June 2026): the `bedrock-runtime`
Converse API reference (request/response shape, the `usage` block, the
`bedrock:InvokeModel` permission, native `toolConfig` structured output), the
Bedrock retry/exponential-backoff guidance (which errors are transient vs.
non-retryable), and the cross-region inference-profile prerequisites
(`bedrock:GetInferenceProfile`, data-residency posture).

**Amazon Bedrock AgentCore and the Strands Agents SDK were both evaluated and
deliberately kept out of scope.** See the _Considered Alternatives_ section below
for the rationale and the specific conditions under which we would revisit each.

This feature does NOT build the abstraction (it exists), does NOT implement the
direct OpenAI/Anthropic (non-Bedrock) adapters (a later, additive step), does NOT
add per-role model selection or evaluator-panel model diversity (still
explicitly deferred), does NOT remove the Explorer-needs-tools guard or implement
the B1 Explorer MCP-decoupling, and does NOT touch the embedding path
(`src/embeddings.py` always calls Bedrock Titan directly and is unchanged).

## Glossary

- **Dream_Cycle**: The deterministic Python orchestrator pipeline
  (`src/dream_cycle/orchestrator.py`) that runs the Explorer, Thinker, and
  evaluator stages by calling a resolved `Invoker` per role.
- **Invoker**: The common call contract (Protocol) in `src/backends/base.py`:
  `invoke(system_prompt, user_message, *, tools=False, timeout=300, effort=None,
  stage=None, run_id=None) -> InvocationResult`.
- **InvocationResult**: The normalized return shape (a `TypedDict`):
  `{"output": Any, "raw": str, "usage": dict | None, "usage_source":
  "real" | "estimate"}`.
- **Direct_API_Backend**: A backend family that performs one **tool-less,
  single-shot** provider API call (no subprocess, no MCP). Declared in
  `BACKEND_CAPABILITIES` with `supports_mcp=False`. `bedrock` is the first member.
- **DirectApiInvoker**: The new shared base for the Direct-API family — the
  analogue of `AgenticCliInvoker` — owning the mechanics common to every
  tool-less provider call. The subject of this feature.
- **BedrockInvoker**: The new adapter wrapping the Amazon Bedrock
  `bedrock-runtime` reasoning API; the first concrete `DirectApiInvoker`. The
  subject of this feature.
- **Converse_API**: The `bedrock-runtime` `converse` operation, which exposes a
  single provider-agnostic request/response shape (native `system` prompt,
  `messages`, `inferenceConfig`, and a `usage` block with `inputTokens` /
  `outputTokens` / `totalTokens`) across Bedrock model providers (Anthropic,
  Amazon Nova, Meta, etc.).
- **InvokeModel_API**: The lower-level `bedrock-runtime` `invoke_model`
  operation, which takes and returns a per-provider request/response body. Used
  by the embedding path today; a fallback for models the Converse API does not
  serve.
- **Inference_Profile**: A Bedrock cross-region inference-profile identifier
  (e.g. `us.anthropic.claude-...`, `global.anthropic.claude-opus-4-8`) passed as
  the `modelId`. Invoking through an inference profile additionally requires the
  `bedrock:GetInferenceProfile` IAM permission.
- **KiroInvoker**: The existing reference adapter (`src/backends/kiro.py`) whose
  observable behavior (return shape and the exceptions it raises) defines the
  failure-mode-parity contract every adapter must match.
- **Resolver**: `src/backends/resolver.py` `Resolver` class; resolves a role to a
  cached `Invoker` per `(backend, model)` from the active profile.
- **DEFAULT_ADAPTERS**: The `backend name -> adapter class` map in
  `src/backends/resolver.py`; currently maps `kiro`, `claude_code`, `codex`.
- **BackendCapabilities / BACKEND_CAPABILITIES**: The frozen capability-flag
  dataclass and registry in `src/backends/base.py`; already declares `bedrock`,
  with the invariant that `metered` requires `reports_usage`.
- **assert_backend_supports_role**: The guard in `src/backends/base.py` that
  rejects routing the Explorer to a non-MCP-capable (`supports_mcp=False`)
  backend.
- **Tool_Less_Stage**: A stage invoked with `tools=False` (Thinker fed a
  pre-fetched packet, the four evaluators, the Express editor) that runs with no
  tool access.
- **Real_Usage**: Token usage reported by the backend itself (recorded with
  `usage_source="real"`), as opposed to Kiro's char/4 `estimate`.
- **Fail_Loud**: Raising `TimeoutError` or `RuntimeError` on infrastructure
  failure rather than returning an empty result or a fabricated verdict.
- **Tolerate_And_Warn**: On a successful, parseable response that is missing only
  usage telemetry, preserve the result, set `usage=None` /
  `usage_source="estimate"`, emit a loud warning, and never raise — because a
  metering gap is not an infrastructure failure.
- **parse_json_output**: The shared, backend-agnostic JSON recovery backstop in
  `src/backends/base.py`.
- **Laptop_Profile**: The default `laptop` profile in `config/backends.toml`
  (all-Kiro / Opus 4.8) that reproduces today's behavior exactly.

## Considered Alternatives

Two AWS-adjacent frameworks were evaluated for this work because they overlap
parts of the system (model invocation and multi-agent coordination). Both were
deliberately **not** adopted. They are recorded here with the reasons against
them now and the conditions that would justify revisiting them — so a future
maintainer inherits the decision, not just the outcome.

### Amazon Bedrock AgentCore

**What it is.** A managed platform for *hosting and operating* agents: a
serverless Runtime (containerized agent hosting, session isolation, long
executions), a managed agent loop (Harness), managed Memory, a Gateway that
converts APIs into MCP tools, Identity, and Code Interpreter. It is framework-
and model-agnostic. It is **not** a model-inference API.

**Why not now.**

- **Wrong layer for the task.** This feature is a tool-less, single-shot model
  call for the evaluators / Thinker-with-packet. The correct surface for that is
  `bedrock-runtime` Converse. AgentCore solves agent *deployment/operation*, a
  problem this feature does not have.
- **Conflicts with a locked tenet.** The dream cycle is a deterministic Python
  orchestrator with a star topology and explicitly *no agentic controller*.
  AgentCore Runtime/Harness is precisely the managed agent loop that tenet
  rejects.
- **Duplicates existing substrate.** The project already owns its memory
  (PostgreSQL + pgvector) and its own MCP server. AgentCore Memory and Gateway
  would be redundant and competing.
- **Cost / maturity / lock-in.** It is a newer managed service that adds
  infrastructure, spend, and coupling for zero benefit to a stateless inference
  call.

**What would make us revisit.** AgentCore becomes relevant only as a *hosting/
deployment* decision, not a backend-adapter decision. Concretely, we would
reconsider it if:

- we need to run the dream cycle as an always-on **hosted** service (e.g. the
  Mac Mini migration outgrows a local launchd job, or we want a cloud-hosted,
  session-isolated, scale-to-zero runtime), or
- we want managed session isolation / long-running (up to 8h) executions, or a
  managed identity story for unattended credentials, that is painful to operate
  ourselves.

Even then it would be scoped as "where the orchestrator runs," kept compatible
with the deterministic-orchestrator tenet, and weighed against simply running our
own process on the target host. It would be its own spec.

### Strands Agents SDK

**What it is.** A model-driven agent SDK (Python/TypeScript) offering an agent
loop, a unified model-provider layer (`BedrockModel` etc. with
`structured_output()` and native usage metrics), and multi-agent patterns —
Graph (deterministic developer-defined routing over model-driven agents), Swarm
(autonomous handoff), Workflow (chained/parallel agents) — plus a
`GoalLoop`/`JudgeAgent` judge plugin.

**Why not now — for the Byzantine consensus panel.**

- **No consensus/BFT primitive.** Graph and Swarm coordinate routing and handoff,
  not independent voting with a ≥3/4 tally. The tally would still be ours — and it
  already exists, pure and tested, in `src/dream_cycle/consensus.py`. Fanning out
  four independent calls is trivial `asyncio`; Graph would wrap the easy part and
  leave the part that matters untouched.
- **Evaluators are single-shot and tool-less by design.** They have no agent
  loop, no tools, no multi-turn reasoning. Strands' core value (the model-driven
  loop) is exactly what an evaluator must not have.
- **Semantic conflict with evaluator integrity.** Strands' budget limits
  terminate "gracefully... no exception is raised" (soft caps). The evaluator
  integrity contract requires the opposite: an infrastructure failure must fail
  loud (`TimeoutError`/`RuntimeError`) and never become a silent or fabricated
  REJECT verdict.

**Why not now — as the model/backend layer.**

- **It would compete with the layer this spec builds.** `BedrockModel +
  structured_output + usage` is essentially an alternative to `BedrockInvoker`.
  Adopting it means either two competing model abstractions or removing the
  hand-built `Invoker` layer we deliberately built with hard-won requirements
  (MCP fail-loud startup probe, failure-mode parity, `usage_source`, `tools` as a
  bool).
- **It cannot run the default path.** Strands wraps model *APIs*, not
  coding-assistant CLIs. Our default `laptop` profile is Kiro CLI → Amazon Q
  ($0); Strands has no adapter for `kiro-cli` and could not reproduce today's
  behavior.

**What would make us revisit.** The one place a model-driven loop is genuinely
wanted is the **Explorer** (the sole agentic, tool-using, multi-turn stage). We
would reconsider Strands if:

- we decided to replace the CLI-based Explorer with a **first-party Python agent
  loop**, where Strands' agent loop + MCP tool support + multi-provider model
  layer would be a strong, idiomatic fit; and/or
- we later pursue **evaluator-panel model diversity** across providers and want a
  single typed `structured_output` interface spanning Bedrock/OpenAI/Anthropic —
  provided its soft-cap/limit semantics are reconciled with our fail-loud
  requirement first.

Both are gated by the existing Explorer-port risk and the deferred per-role-
routing work; neither is part of this Bedrock adapter.

## Requirements

### Requirement 1: BedrockInvoker conforms to the Invoker contract

**User Story:** As a dream-cycle operator on an AWS-only machine, I want a Bedrock
adapter that satisfies the existing `Invoker` Protocol, so that the orchestrator
can call it through `_invoker_for(role)` with no orchestrator changes.

#### Acceptance Criteria

1. THE BedrockInvoker SHALL implement the `Invoker` Protocol method signature
   `invoke(system_prompt, user_message, *, tools=False, timeout=300, effort=None,
   stage=None, run_id=None)` defined in `src/backends/base.py`.
2. THE BedrockInvoker SHALL accept a `model` argument at construction
   (`BedrockInvoker(model=<id>)`) consistent with how the Resolver instantiates
   adapters.
3. WHEN an invocation completes successfully, THE BedrockInvoker SHALL return an
   `InvocationResult` containing the keys `output`, `raw`, `usage`, and
   `usage_source`.
4. THE BedrockInvoker SHALL place the parsed payload in `output` and the model's
   final text in `raw`.
5. THE BedrockInvoker SHALL perform a single tool-less provider API call per
   invocation (no subprocess and no MCP), distinguishing it from the agentic-CLI
   adapters.

### Requirement 2: Direct-API family base owns the shared mechanics

**User Story:** As a maintainer who will add direct OpenAI/Anthropic adapters
later, I want the Direct-API mechanics extracted into one shared base, so that
every Direct-API adapter inherits identical failure-mode handling, metrics, and
parsing instead of re-implementing them and drifting.

#### Acceptance Criteria

1. THE feature SHALL provide a shared `DirectApiInvoker` base (the Direct-API
   analogue of `AgenticCliInvoker`) that owns: the per-call metrics JSONL writer
   (carrying `usage_source` and real token counts when available), the shared
   `parse_json_output` backstop, usage normalization, the Tolerate_And_Warn path,
   failure-mode mapping, and `tools=False` enforcement.
2. THE BedrockInvoker SHALL drive the `DirectApiInvoker` base and override only
   provider-specific hooks: client construction, request shaping, response and
   usage extraction, native structured-output shaping, and provider-exception
   mapping.
3. THE `DirectApiInvoker` base SHALL be designed so a future `OpenAIInvoker` or
   `AnthropicInvoker` (direct, non-Bedrock) can be added by overriding the same
   hooks, without modifying the base.
4. THE per-call metrics record written by a Direct-API adapter SHALL use the same
   JSONL shape as the agentic-CLI adapters (`logs/llm_metrics/<run_id>.jsonl`),
   so cross-backend cost analysis never silently mixes estimate and real counts.

### Requirement 3: BedrockInvoker enforces tool-less execution

**User Story:** As the owner of the Explorer-needs-tools guard, I want the Bedrock
adapter to refuse tool-using invocations, so that a tool-less Direct-API backend
can never be silently asked to run an agentic stage.

#### Acceptance Criteria

1. WHEN `invoke` is called with `tools=False`, THE BedrockInvoker SHALL perform
   the tool-less provider call normally.
2. IF `invoke` is called with `tools=True`, THEN THE BedrockInvoker SHALL raise
   an exception rather than attempt the call, because the Direct-API family
   cannot attach MCP tools (`supports_mcp=False`).
3. THE BedrockInvoker SHALL NEVER perform an MCP startup probe (it has no tools to
   probe).
4. THE existing `assert_backend_supports_role` guard SHALL continue to reject
   assigning the Explorer to `bedrock`, and this feature SHALL NOT weaken or
   remove that guard.

### Requirement 4: BedrockInvoker request construction

**User Story:** As a maintainer, I want the Bedrock request built from a
provider-agnostic API surface, so that model selection, the system prompt, and
generation limits behave consistently across Bedrock model providers.

#### Acceptance Criteria

1. THE BedrockInvoker SHALL invoke the `bedrock-runtime` Converse_API as the
   default request path, passing the configured `model` as the `modelId`.
2. THE BedrockInvoker SHALL resolve the AWS region from configuration (an
   environment variable, defaulting consistently with the embedding path) and
   SHALL NOT hardcode the region.
3. THE BedrockInvoker SHALL pass the `user_message` as the conversation message
   content, distinct from the system prompt.
4. THE BedrockInvoker SHALL pass generation bounds (e.g. max output tokens) via
   the request's `inferenceConfig`, with a configurable default.
5. WHERE a target model is not served by the Converse_API, THE BedrockInvoker
   SHALL be able to fall back to the InvokeModel_API with the per-provider
   request/response body isolated behind a provider-specific hook.

### Requirement 5: BedrockInvoker system-prompt delivery preserves role independence

**User Story:** As the owner of the evaluator panel, I want each role's system
prompt delivered natively to Bedrock, so that evaluator independence (defined by
their distinct system prompts) is preserved across the backend change.

#### Acceptance Criteria

1. THE BedrockInvoker SHALL deliver the `system_prompt` natively via the
   Converse_API `system` field.
2. THE BedrockInvoker SHALL keep the `user_message` distinct from the system
   prompt in the request.
3. IF a target model does not accept a native system field, THEN THE
   BedrockInvoker SHALL prepend the system prompt to the user message as a
   fallback, and the fallback path SHALL be covered by a test.

### Requirement 6: BedrockInvoker captures real token usage

**User Story:** As a cost-conscious operator, I want Bedrock's real token usage
captured per call, so that the run cost budget compares measurements against
measurements.

#### Acceptance Criteria

1. WHEN an invocation completes successfully AND the response contains a `usage`
   block, THE BedrockInvoker SHALL populate `usage` from the response's token
   counts (`inputTokens` / `outputTokens` / `totalTokens`) and set `usage_source`
   to `"real"`.
2. WHEN an invocation completes successfully AND produces a parseable payload but
   the response omits usage, THE BedrockInvoker SHALL Tolerate_And_Warn: keep
   `output` and `raw`, set `usage` to `None`, set `usage_source` to `"estimate"`
   (the char/4 fallback KiroInvoker uses), and emit a loud warning that real
   usage was expected from a metered backend but absent.
3. WHEN usage is absent, THE BedrockInvoker SHALL NOT raise, discard the result,
   or route into the failure path, because missing telemetry is a metering gap,
   not an infrastructure failure (preserving the failure-mode parity of
   Requirement 7).
4. THE `metered ⇒ reports_usage` invariant for `bedrock` SHALL remain enforced at
   the `BACKEND_CAPABILITIES` capability-registry level, not as a per-call guard.

### Requirement 7: BedrockInvoker failure-mode parity with KiroInvoker

**User Story:** As the owner of the evaluator retry/abort logic, I want Bedrock
infrastructure failures mapped onto the same exceptions KiroInvoker raises, so
that a crashed evaluator never becomes a fabricated REJECT verdict.

#### Acceptance Criteria

1. WHEN the Bedrock call exceeds `timeout` (a client read timeout, or a
   `ModelTimeoutException` that persists after retries), THE BedrockInvoker SHALL
   raise `TimeoutError`.
2. WHERE the failure is a transient error (`ThrottlingException`,
   `ModelTimeoutException`, `ServiceUnavailableException`,
   `InternalServerException`, or a network error), THE BedrockInvoker SHALL apply
   a bounded retry with exponential backoff and jitter before giving up, using
   the `botocore` retry configuration (`standard` or `adaptive` mode) and/or
   explicit retry handling, so a single transient failure does not abort a run.
3. IF the failure is a non-transient client error (`ValidationException` or
   `AccessDeniedException`), THEN THE BedrockInvoker SHALL NOT retry and SHALL
   raise `RuntimeError` immediately.
4. IF any Bedrock or `botocore` error remains after the retry policy is exhausted,
   THEN THE BedrockInvoker SHALL raise `RuntimeError` (for non-timeout failures)
   rather than returning an empty `InvocationResult` or a fabricated verdict.

### Requirement 8: BedrockInvoker reuses the shared JSON recovery backstop

**User Story:** As a maintainer, I want Bedrock output parsed through the shared
backstop, so that all adapters share one parser and a round-trip-tested recovery
path.

#### Acceptance Criteria

1. WHEN extracting the parsed payload, THE BedrockInvoker SHALL apply the shared
   `parse_json_output` backstop from `src/backends/base.py` to the model's final
   text.
2. WHERE native structured output is requested for a stage AND the target model
   supports it, THE BedrockInvoker SHALL prefer the schema-constrained payload —
   via the Converse_API `toolConfig` `ToolSpecification` (with the `strict` flag)
   or an equivalent model-native mechanism — and fall back to `parse_json_output`.
3. IF the final text contains no recoverable JSON, THEN THE BedrockInvoker SHALL
   raise `ValueError`.

### Requirement 9: Resolver registers the Bedrock adapter

**User Story:** As an operator, I want `bedrock` registered in the resolver, so
that a profile assigning a tool-less role to `bedrock` resolves to a working
invoker instead of raising `NotImplementedError`.

#### Acceptance Criteria

1. THE Resolver SHALL map `bedrock` to `BedrockInvoker` in `DEFAULT_ADAPTERS`.
2. WHEN a profile assigns a tool-less role to `bedrock`, THE Resolver SHALL return
   a cached invoker per `(backend, model)` for that role.
3. WHEN a profile assigns the Explorer to `bedrock`, THE Resolver SHALL fail fast
   (at construction, via the eager guard) because `bedrock` declares
   `supports_mcp=False` — a whole-instance Bedrock profile is therefore correctly
   rejected, consistent with the MVP "one model per instance" policy not applying
   to a tool-less backend.

### Requirement 10: Default behavior is preserved

**User Story:** As an operator relying on the laptop today, I want zero behavior
change until a profile selects `bedrock`, so that the existing pipeline and test
suite remain green.

#### Acceptance Criteria

1. WHILE the Laptop_Profile is active, THE Dream_Cycle SHALL exhibit the same
   behavior as before this feature, with all roles resolving to KiroInvoker.
2. WHEN the existing full test suite is run with the Laptop_Profile active, THE
   Dream_Cycle SHALL pass all existing tests unchanged.
3. THE feature SHALL NOT modify the embedding path, which always calls Bedrock
   Titan directly via `invoke_model`.

### Requirement 11: Effort is recorded as provenance, not assumed cross-backend equivalent

**User Story:** As an analyst comparing runs across backends, I want the effort
level recorded as provenance, so that I never assume "high" on Bedrock equals
"high" on Kiro or a CLI backend.

#### Acceptance Criteria

1. WHEN `effort` is provided, THE BedrockInvoker SHALL record it as provenance for
   the call in the metrics line.
2. WHERE the target model exposes a native reasoning-effort control, THE
   BedrockInvoker SHALL map `effort` onto it; otherwise it SHALL record `effort`
   as provenance only without failing.
3. THE BedrockInvoker SHALL NOT assume identical effort level names produce
   equivalent reasoning effort across backends.

### Requirement 12: Credentials, region, and IAM are supplied out-of-band

**User Story:** As an operator provisioning a headless box, I want Bedrock
credentials and region supplied through standard AWS configuration, so that no
secret ever lives in the repo or the profile config.

#### Acceptance Criteria

1. THE BedrockInvoker SHALL obtain AWS credentials through the standard
   `boto3`/`botocore` credential chain (environment, shared config, or instance
   role), and SHALL NOT read credentials from `config/backends.toml` or hardcode
   them.
2. THE feature SHALL document the required IAM permissions
   (`bedrock:InvokeModel`, plus `bedrock:GetInferenceProfile` when invoking
   through an Inference_Profile) and the region/profile environment variables.
3. THE `config/backends.toml` profile entries for `bedrock` SHALL contain only
   the model id (and effort), never credentials.

### Requirement 13: Data-governance gate before real memory content flows to a metered backend

**User Story:** As the owner of data governance, I want the Bedrock backend's
metered, content-sending nature acknowledged, so that real memory content is not
silently routed off the current path without a deliberate decision.

#### Acceptance Criteria

1. THE feature SHALL document that `bedrock` is a metered backend that sends
   memory content to Amazon Bedrock under AWS governance (Amazon Bedrock does not
   store the request content it receives), and that selecting it for a role is a
   deliberate per-machine decision. WHERE a cross-region Inference_Profile is
   used, the documentation SHALL note the data-residency implication (requests may
   be processed in other Regions within the profile's geography).
2. THE feature SHALL NOT change the default routing (Laptop_Profile stays
   all-Kiro), so no real memory content reaches Bedrock until an operator selects
   a `bedrock`-using profile.

### Requirement 14: Verification approach

**User Story:** As a maintainer building on a laptop that already has Bedrock
credentials (the embedding path uses them), I want Bedrock verified both by
mocked-client unit tests and a real end-to-end call, so that the adapter is
proven without requiring a live call in CI.

#### Acceptance Criteria

1. THE BedrockInvoker SHALL be covered by mocked-client unit tests (mocking the
   `boto3` `bedrock-runtime` client) for request construction, system-prompt
   delivery, `tools=True` rejection, usage capture, the Tolerate_And_Warn
   fallback, structured-output shaping, parser reuse, and every failure-mode row.
2. THE BedrockInvoker SHALL be covered by a live end-to-end verification against
   real Bedrock for a tool-less evaluator-style call, confirming real usage
   capture and JSON parsing of the final text, recorded in a verification doc.
3. WHERE adapter unit tests run, THE tests SHALL mock the Bedrock client so that
   no live AWS call or credentials are required in CI.
