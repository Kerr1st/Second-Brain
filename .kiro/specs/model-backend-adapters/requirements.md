# Requirements Document

## Introduction

The Second Brain dream-cycle pipeline already runs on a pluggable model-backend
scaffold: a common `Invoker` Protocol, an `InvocationResult` return shape, a
`BackendCapabilities` registry, the `assert_backend_supports_role` guard, a
shared `parse_json_output` backstop (all in `src/backends/base.py`), a reference
`KiroInvoker` adapter (`src/backends/kiro.py`), a profile-driven `Resolver`
(`src/backends/resolver.py`), and orchestrator wiring (`_invoker_for(role)`).
Today only the `kiro` backend has a registered adapter, so any profile selecting
another backend raises `NotImplementedError`.

This feature delivers the two concrete agentic-CLI adapters the scaffold was
built for so the pipeline can run on coding-assistant CLIs other than Kiro:

- **`ClaudeCodeInvoker`** — wraps the `claude -p` CLI.
- **`CodexInvoker`** — wraps the `codex exec` CLI.

Both adapters are registered in the resolver's `DEFAULT_ADAPTERS` map so the
existing `mini` profile (`claude_code`) and a future `codex` profile work
end-to-end. Both backends are agentic/MCP-capable and therefore eligible for all
roles (Explorer, Thinker, evaluators, Express), subject to the existing
capability guard. Both report real token usage and offer native structured
output, in contrast to Kiro's char/4 estimate.

The authoritative source of truth for the exact CLI command surfaces, capability
matrix, failure-mode parity requirement, MCP fail-loud obligations,
system-prompt delivery options, sandbox caveats, and testing strategy is
`docs/MODEL-BACKENDS.md`.

This feature does NOT build the abstraction (it exists), does NOT implement the
direct-API adapters (Bedrock/OpenAI/Anthropic `InvokeModel`, a later separate
feature), does NOT add per-role model selection or evaluator-panel model
diversity (explicitly deferred), and does NOT touch the embedding path
(embeddings always call Bedrock Titan directly and are unchanged).

## Glossary

- **Dream_Cycle**: The deterministic Python orchestrator pipeline
  (`src/dream_cycle/orchestrator.py`) that runs the Explorer, Thinker, and
  evaluator stages by calling a resolved `Invoker` per role.
- **Invoker**: The common call contract (Protocol) defined in
  `src/backends/base.py`: `invoke(system_prompt, user_message, *, tools=False,
  timeout=300, effort=None, stage=None, run_id=None) -> InvocationResult`.
- **InvocationResult**: The normalized return shape (a `TypedDict`):
  `{"output": Any, "raw": str, "usage": dict | None, "usage_source":
  "real" | "estimate"}`.
- **ClaudeCodeInvoker**: The new adapter wrapping the `claude -p` CLI; the
  subject of this feature.
- **CodexInvoker**: The new adapter wrapping the `codex exec` CLI; the subject of
  this feature.
- **KiroInvoker**: The existing reference adapter (`src/backends/kiro.py`) whose
  observable behavior defines the failure-mode and fail-loud parity contract.
- **Resolver**: `src/backends/resolver.py` `Resolver` class; resolves a role to a
  cached `Invoker` per `(backend, model)` from the active profile.
- **DEFAULT_ADAPTERS**: The `backend name -> adapter class` map in
  `src/backends/resolver.py`; currently maps only `kiro`.
- **BackendCapabilities**: The frozen dataclass of capability flags
  (`supports_mcp`, `metered`, `structured_output`, `reports_usage`) with the
  invariant that `metered` requires `reports_usage`.
- **BACKEND_CAPABILITIES**: The capability registry in `src/backends/base.py`;
  already declares `kiro`, `claude_code`, `codex`, and `bedrock`.
- **MCP**: Model Context Protocol; the live tool layer the Second Brain MCP
  server (`python -m src.mcp_server`) exposes for agentic stages.
- **MCP_Startup_Probe**: A single trivial tool call issued during an agentic
  invocation to confirm MCP tools actually attached and are reachable, rather
  than relying on process-attach alone.
- **Fail_Loud**: Raising `TimeoutError` or `RuntimeError` on infrastructure
  failure rather than returning an empty result or a fabricated verdict.
- **Tool_Less_Stage**: A stage invoked with `tools=False` (evaluators, Express)
  that must run with no tool access.
- **Real_Usage**: Token usage reported by the backend itself (recorded with
  `usage_source="real"`), as opposed to Kiro's char/4 `estimate`.
- **Laptop_Profile**: The default `laptop` profile in `config/backends.toml`
  (all-Kiro / Opus 4.8) that reproduces today's behavior exactly.
- **parse_json_output**: The shared, backend-agnostic JSON recovery backstop in
  `src/backends/base.py`.
- **Public_Flag_Surface**: The set of documented, publicly-supported CLI flags
  for a backend, per the official Claude Code CLI reference
  (`code.claude.com/docs/en/cli-reference`). The adapter's command-construction
  contract is defined against this surface so the adapter stays portable to
  machines that do not run an internal build. The local enterprise-managed `claude` is a
  wrapper that forwards to the same native binary, so these flags work on both.
- **enterprise-managed_Build**: The enterprise-managed `claude` build present on this machine
  (`claude` 2.1.177.377). Used to VERIFY the Public_Flag_Surface at build time;
  any enterprise-managed-specific auth/metering divergence is isolated behind configuration,
  not hardcoded into the adapter.

## Requirements

### Requirement 1: ClaudeCodeInvoker conforms to the Invoker contract

**User Story:** As a dream-cycle operator on a machine without Kiro, I want a
Claude Code adapter that satisfies the existing `Invoker` Protocol, so that the
orchestrator can call it through `_invoker_for(role)` with no orchestrator
changes.

#### Acceptance Criteria

1. THE ClaudeCodeInvoker SHALL implement the `Invoker` Protocol method signature
   `invoke(system_prompt, user_message, *, tools=False, timeout=300,
   effort=None, stage=None, run_id=None)` defined in `src/backends/base.py`.
2. THE ClaudeCodeInvoker SHALL accept a `model` argument at construction
   (`ClaudeCodeInvoker(model=<id>)`) consistent with how the Resolver
   instantiates adapters.
3. WHEN an invocation completes successfully, THE ClaudeCodeInvoker SHALL return
   an `InvocationResult` containing the keys `output`, `raw`, `usage`, and
   `usage_source`.
4. THE ClaudeCodeInvoker SHALL place the parsed payload in `output` and the
   backend's final text in `raw`.
5. THE ClaudeCodeInvoker SHALL spawn the `claude -p` CLI as a non-interactive
   subprocess for each invocation.

### Requirement 2: ClaudeCodeInvoker command construction

**User Story:** As a maintainer, I want the Claude Code command line built from
the verified flag surface, so that model selection, effort, and output format
behave as documented.

#### Acceptance Criteria

1. THE ClaudeCodeInvoker SHALL construct the command with `claude -p` and pass
   the configured model via `--model <id>`.
2. WHEN `effort` is provided, THE ClaudeCodeInvoker SHALL pass the effort level
   to the CLI via its effort flag.
3. THE ClaudeCodeInvoker SHALL request JSON output via `--output-format json` and
   extract the final text from the envelope `.result` field.
4. WHERE the target stage requires schema-constrained output, THE
   ClaudeCodeInvoker SHALL request native structured output via `--json-schema`.
5. THE ClaudeCodeInvoker SHALL construct its command using only flags from the
   documented Public_Flag_Surface, so the adapter remains portable to machines
   that do not run the enterprise-managed_Build (e.g. the Mac Mini).
6. THE ClaudeCodeInvoker SHALL use the local enterprise-managed_Build only to verify, at build
   time, that the Public_Flag_Surface flags work, and SHALL isolate any
   enterprise-managed-specific auth or metering divergence behind configuration or environment
   rather than hardcoding an enterprise-managed-only code path.
7. THE ClaudeCodeInvoker SHALL invoke a configurable `claude` binary path
   (e.g. via a `CLAUDE_CLI` environment variable defaulting to `claude`),
   mirroring how `KiroInvoker` resolves `KIRO_CLI`, so it runs against either a
   public Claude Code install or the enterprise-managed wrapper without code changes.
8. THE ClaudeCodeInvoker SHALL NOT depend on the enterprise-managed provider wrapper's
   non-public surface (e.g. `--aws-profile`, Bedrock credential routing, or the
   `--claude-help` subcommand); authentication and provider routing (Claude
   subscription, `ANTHROPIC_API_KEY`, or standard Bedrock environment variables)
   SHALL be supplied out-of-band via environment, not by adapter code.

### Requirement 3: ClaudeCodeInvoker system-prompt delivery preserves role independence

**User Story:** As the owner of the evaluator panel, I want each role's system
prompt delivered natively, so that evaluator independence (which is defined by
their distinct system prompts) is preserved across the backend change.

#### Acceptance Criteria

1. THE ClaudeCodeInvoker SHALL deliver the `system_prompt` natively via
   `--system-prompt-file` or `--append-system-prompt`.
2. THE ClaudeCodeInvoker SHALL pass the `user_message` as the prompt input
   distinct from the system prompt.
3. IF native system-prompt delivery is unavailable, THEN THE ClaudeCodeInvoker
   SHALL prepend the system prompt to the user message as a fallback and the
   fallback path SHALL be covered by a test.

### Requirement 4: ClaudeCodeInvoker MCP attachment and cwd discipline for agentic stages

**User Story:** As an operator burned by the June 8–12 zero-candidate outage, I
want agentic Claude Code stages to spawn the MCP server with the correct working
directory, so that the tools the Explorer and Thinker need are actually
available.

#### Acceptance Criteria

1. WHEN `tools=True`, THE ClaudeCodeInvoker SHALL attach the Second Brain MCP
   server via `--mcp-config` with the server command `python -m src.mcp_server`.
2. WHEN `tools=True`, THE ClaudeCodeInvoker SHALL configure the MCP server entry
   to run with the working directory set to the repository root.
3. WHEN `tools=True`, THE ClaudeCodeInvoker SHALL restrict MCP configuration to
   the explicitly provided server via `--strict-mcp-config`.

### Requirement 5: ClaudeCodeInvoker fails loudly when MCP tools do not attach

**User Story:** As an operator, I want an agentic Claude Code stage to fail
loudly when its tools did not attach or are unreachable, so that a stage never
silently runs tool-less and produces zero candidates.

#### Acceptance Criteria

1. WHEN `tools=True`, THE ClaudeCodeInvoker SHALL perform an MCP_Startup_Probe
   consisting of one trivial tool call to confirm the MCP tools are reachable.
2. IF the MCP_Startup_Probe does not confirm tool reachability, THEN THE
   ClaudeCodeInvoker SHALL raise `RuntimeError`.
3. IF the response envelope reports `is_error`, THEN THE ClaudeCodeInvoker SHALL
   raise `RuntimeError`.
4. THE ClaudeCodeInvoker SHALL treat process-attach alone as insufficient
   evidence that MCP tools are usable.
5. THE ClaudeCodeInvoker SHALL perform the MCP_Startup_Probe only when
   `tools=True`, never on a tool-less stage.
6. THE ClaudeCodeInvoker SHALL NOT cache or reuse MCP-health or probe results
   across invocations; each agentic invocation spawns a fresh subprocess with
   independent MCP startup and SHALL perform its own probe, so a per-process
   attach or sandbox-block failure cannot be masked by a stale healthy result.

### Requirement 6: ClaudeCodeInvoker enforces tool-less execution for evaluator stages

**User Story:** As the owner of the BFT evaluator gate, I want tool-less stages
to run with no tool access on Claude Code, so that evaluators cannot reach live
tools and remain independent single-shot judges.

#### Acceptance Criteria

1. WHEN `tools=False`, THE ClaudeCodeInvoker SHALL invoke the CLI with no MCP
   tools attached.
2. WHEN `tools=False`, THE ClaudeCodeInvoker SHALL pass `--strict-mcp-config`
   WITHOUT any `--mcp-config`, so that no MCP servers load at all, AND SHALL
   pass `--tools ""` to disable built-in tools. NOTE: `--tools ""` alone is
   insufficient because, per the public Claude Code reference, it disables only
   built-in tools and does not affect MCP tools.
3. WHEN `tools=False`, THE ClaudeCodeInvoker SHALL NOT perform an
   MCP_Startup_Probe.

### Requirement 7: ClaudeCodeInvoker captures real token usage

**User Story:** As a cost-conscious operator, I want Claude Code's real token
usage captured per call, so that the run cost budget compares measurements,
never estimates, against measurements.

#### Acceptance Criteria

1. WHEN an invocation completes successfully AND the response envelope contains
   `usage`, THE ClaudeCodeInvoker SHALL populate `usage` from the envelope's
   `usage` and `total_cost_usd` fields and set `usage_source` to `"real"`.
2. WHEN an invocation completes successfully AND produces a parseable payload but
   the response envelope omits `usage`, THE ClaudeCodeInvoker SHALL treat the
   invocation as successful: it SHALL keep `output` and `raw`, set `usage` to
   `None`, set `usage_source` to `"estimate"` (the char/4 fallback KiroInvoker
   uses), and emit a loud warning that real usage was expected from a metered
   backend but absent.
3. WHEN `usage` is absent, THE ClaudeCodeInvoker SHALL NOT raise, discard the
   result, or route into the failure/retry/abort path, because missing telemetry
   is a metering gap, not an infrastructure failure (preserving the failure-mode
   parity of Requirement 8).
4. THE `metered ⇒ reports_usage` invariant for `claude_code` SHALL be enforced at
   the `BACKEND_CAPABILITIES` capability-registry level, not as a per-call guard.

### Requirement 8: ClaudeCodeInvoker failure-mode parity with KiroInvoker

**User Story:** As the owner of the evaluator retry/abort logic, I want Claude
Code infrastructure failures mapped onto the same exceptions KiroInvoker raises,
so that a crashed evaluator never becomes a fabricated REJECT verdict.

#### Acceptance Criteria

1. WHEN the subprocess exceeds `timeout`, THE ClaudeCodeInvoker SHALL raise
   `TimeoutError`.
2. IF the CLI exits with a non-zero exit code, THEN THE ClaudeCodeInvoker SHALL
   raise `RuntimeError`.
3. IF the response envelope reports `is_error`, THEN THE ClaudeCodeInvoker SHALL
   raise `RuntimeError`.
4. IF an infrastructure failure occurs, THEN THE ClaudeCodeInvoker SHALL raise an
   exception rather than returning an empty `InvocationResult` or a fabricated
   verdict.

### Requirement 9: ClaudeCodeInvoker reuses the shared JSON recovery backstop

**User Story:** As a maintainer, I want Claude Code output parsed through the
shared backstop, so that all adapters share one parser and a round-trip-tested
recovery path.

#### Acceptance Criteria

1. WHEN extracting the parsed payload, THE ClaudeCodeInvoker SHALL apply the
   shared `parse_json_output` backstop from `src/backends/base.py` to the final
   text.
2. WHERE native structured output is requested, THE ClaudeCodeInvoker SHALL
   prefer the schema-validated payload and fall back to `parse_json_output`.
3. IF the final text contains no recoverable JSON, THEN THE ClaudeCodeInvoker
   SHALL raise `ValueError`.

### Requirement 10: CodexInvoker conforms to the Invoker contract

**User Story:** As a dream-cycle operator on an OpenAI/Codex machine, I want a
Codex adapter that satisfies the existing `Invoker` Protocol, so that the
orchestrator can call it through `_invoker_for(role)` with no orchestrator
changes.

#### Acceptance Criteria

1. THE CodexInvoker SHALL implement the `Invoker` Protocol method signature
   `invoke(system_prompt, user_message, *, tools=False, timeout=300,
   effort=None, stage=None, run_id=None)` defined in `src/backends/base.py`.
2. THE CodexInvoker SHALL accept a `model` argument at construction
   (`CodexInvoker(model=<id>)`) consistent with how the Resolver instantiates
   adapters.
3. WHEN an invocation completes successfully, THE CodexInvoker SHALL return an
   `InvocationResult` containing the keys `output`, `raw`, `usage`, and
   `usage_source`.
4. THE CodexInvoker SHALL spawn the `codex exec` CLI as a non-interactive
   subprocess for each invocation.

### Requirement 11: CodexInvoker command construction

**User Story:** As a maintainer, I want the Codex command line built from the
documented `codex exec` surface, so that model selection, effort, and final-text
extraction behave as documented.

#### Acceptance Criteria

1. THE CodexInvoker SHALL construct the command with `codex exec` and pass the
   configured model via `-m <id>`.
2. WHEN `effort` is provided, THE CodexInvoker SHALL pass the reasoning effort
   via `-c model_reasoning_effort=<level>`.
3. THE CodexInvoker SHALL extract the final text via `--output-last-message
   <file>` or the `--json` event stream.
4. WHERE the target stage requires schema-constrained output, THE CodexInvoker
   SHALL request native structured output via `--output-schema`.

### Requirement 12: CodexInvoker system-prompt delivery preserves role independence

**User Story:** As the owner of the evaluator panel, I want each role's system
prompt delivered natively on Codex, so that evaluator independence is preserved
across the backend change.

#### Acceptance Criteria

1. THE CodexInvoker SHALL deliver the `system_prompt` natively via
   `developer_instructions`, `instructions`, or `model_instructions_file`.
2. THE CodexInvoker SHALL pass the `user_message` as the prompt input distinct
   from the system prompt.
3. IF native system-prompt delivery is unavailable, THEN THE CodexInvoker SHALL
   prepend the system prompt to the user message as a fallback and the fallback
   path SHALL be covered by a test.

### Requirement 13: CodexInvoker MCP attachment, cwd discipline, and sandbox for agentic stages

**User Story:** As an operator, I want agentic Codex stages to attach the MCP
server with the correct working directory and a sandbox that allows the server
to reach Postgres and Bedrock, so that the Explorer and Thinker tools work and
are not silently blocked.

#### Acceptance Criteria

1. WHEN `tools=True`, THE CodexInvoker SHALL configure the Second Brain MCP
   server via `mcp_servers.second_brain` with command `python -m src.mcp_server`.
2. WHEN `tools=True`, THE CodexInvoker SHALL set the MCP server entry's `cwd` to
   the repository root.
3. WHEN `tools=True`, THE CodexInvoker SHALL mark the MCP server entry
   `required=true` so Codex fails when the server cannot start.
4. WHEN `tools=True`, THE CodexInvoker SHALL run with `--sandbox workspace-write`
   and `sandbox_workspace_write.network_access=true`.

### Requirement 14: CodexInvoker fails loudly when MCP tools do not attach

**User Story:** As an operator, I want an agentic Codex stage to fail loudly when
its tools did not attach or are unreachable, so that a sandbox-blocked MCP server
never causes a silent tool-less run.

#### Acceptance Criteria

1. WHEN `tools=True`, THE CodexInvoker SHALL perform an MCP_Startup_Probe
   consisting of one trivial tool call to confirm the MCP tools are reachable.
2. IF the MCP_Startup_Probe does not confirm tool reachability, THEN THE
   CodexInvoker SHALL raise `RuntimeError`.
3. THE CodexInvoker SHALL treat process-attach alone as insufficient evidence
   that MCP tools are usable, because Codex sandboxing can block the server's
   network and database access.
4. THE CodexInvoker SHALL treat the Codex `mcp_servers.<name>.required=true`
   startup guarantee as the PRIMARY guard (it hard-fails at startup, before any
   model call), and the MCP_Startup_Probe as the SECONDARY check for the case
   `required=true` cannot catch — the server starts but the sandbox blocks its
   network or database access — so the probe's trivial tool call exercises
   reachability to Postgres/Bedrock.
5. THE CodexInvoker SHALL perform the MCP_Startup_Probe only when `tools=True`,
   never on a tool-less stage, and SHALL NOT cache or reuse MCP-health or probe
   results across invocations; each agentic invocation performs its own probe.

### Requirement 15: CodexInvoker enforces tool-less execution for evaluator stages

**User Story:** As the owner of the BFT evaluator gate, I want tool-less stages
to run with no tool access on Codex, so that evaluators remain independent
single-shot judges.

#### Acceptance Criteria

1. WHEN `tools=False`, THE CodexInvoker SHALL invoke the CLI with no MCP servers
   configured.
2. WHEN `tools=False`, THE CodexInvoker SHALL run with `--sandbox read-only`.
3. WHEN `tools=False`, THE CodexInvoker SHALL NOT perform an MCP_Startup_Probe.

### Requirement 16: CodexInvoker captures real token usage

**User Story:** As a cost-conscious operator, I want Codex's real token usage
captured per call, so that the run cost budget compares measurements against
measurements.

#### Acceptance Criteria

1. WHEN an invocation completes successfully AND the `--json` event stream
   reports usage events, THE CodexInvoker SHALL populate `usage` from those usage
   events and set `usage_source` to `"real"`.
2. WHEN an invocation completes successfully AND produces a parseable payload but
   no usage events are reported, THE CodexInvoker SHALL treat the invocation as
   successful: it SHALL keep `output` and `raw`, set `usage` to `None`, set
   `usage_source` to `"estimate"` (the char/4 fallback KiroInvoker uses), and
   emit a loud warning that real usage was expected from a metered backend but
   absent.
3. WHEN usage events are absent, THE CodexInvoker SHALL NOT raise, discard the
   result, or route into the failure/retry/abort path, because missing telemetry
   is a metering gap, not an infrastructure failure (preserving the failure-mode
   parity of Requirement 17).
4. THE `metered ⇒ reports_usage` invariant for `codex` SHALL be enforced at the
   `BACKEND_CAPABILITIES` capability-registry level, not as a per-call guard.

### Requirement 17: CodexInvoker failure-mode parity with KiroInvoker

**User Story:** As the owner of the evaluator retry/abort logic, I want Codex
infrastructure failures mapped onto the same exceptions KiroInvoker raises, so
that a crashed evaluator never becomes a fabricated REJECT verdict.

#### Acceptance Criteria

1. WHEN the subprocess exceeds `timeout`, THE CodexInvoker SHALL raise
   `TimeoutError`.
2. IF the CLI exits with a non-zero exit code, THEN THE CodexInvoker SHALL raise
   `RuntimeError`.
3. IF an infrastructure failure occurs, THEN THE CodexInvoker SHALL raise an
   exception rather than returning an empty `InvocationResult` or a fabricated
   verdict.

### Requirement 18: CodexInvoker reuses the shared JSON recovery backstop

**User Story:** As a maintainer, I want Codex output parsed through the shared
backstop, so that all adapters share one parser.

#### Acceptance Criteria

1. WHEN extracting the parsed payload, THE CodexInvoker SHALL apply the shared
   `parse_json_output` backstop from `src/backends/base.py` to the final text.
2. WHERE native structured output is requested, THE CodexInvoker SHALL prefer the
   schema-validated payload and fall back to `parse_json_output`.
3. IF the final text contains no recoverable JSON, THEN THE CodexInvoker SHALL
   raise `ValueError`.

### Requirement 19: Resolver registers both new adapters

**User Story:** As an operator, I want both adapters registered in the resolver,
so that a profile selecting `claude_code` or `codex` resolves to a working
invoker instead of raising `NotImplementedError`.

#### Acceptance Criteria

1. THE Resolver SHALL map `claude_code` to `ClaudeCodeInvoker` in
   `DEFAULT_ADAPTERS`.
2. THE Resolver SHALL map `codex` to `CodexInvoker` in `DEFAULT_ADAPTERS`.
3. WHEN a profile assigns a role to `claude_code` or `codex`, THE Resolver SHALL
   return a cached invoker per `(backend, model)` for that role.
4. WHEN the active profile assigns the Explorer to `claude_code` or `codex`, THE
   Resolver SHALL pass the existing `assert_backend_supports_role` guard because
   both backends declare `supports_mcp=True`.

### Requirement 20: Default behavior is preserved

**User Story:** As an operator relying on the laptop today, I want zero behavior
change until a profile selects a new backend, so that the existing pipeline and
test suite remain green.

#### Acceptance Criteria

1. WHILE the Laptop_Profile is active, THE Dream_Cycle SHALL exhibit the same
   behavior as before this feature, with all roles resolving to KiroInvoker.
2. WHEN the existing full test suite is run with the Laptop_Profile active, THE
   Dream_Cycle SHALL pass all approximately 685 existing tests unchanged.
3. THE feature SHALL NOT modify the embedding path, which always calls Bedrock
   Titan directly.

### Requirement 21: Effort is recorded as provenance, not assumed cross-backend equivalent

**User Story:** As an analyst comparing runs across backends, I want the effort
level recorded as provenance per backend, so that I never assume "high" on
Claude Code equals "high" on Kiro or Codex.

#### Acceptance Criteria

1. WHEN `effort` is provided, THE ClaudeCodeInvoker SHALL pass it to the CLI and
   record it as provenance for the call.
2. WHEN `effort` is provided, THE CodexInvoker SHALL pass it to the CLI and
   record it as provenance for the call.
3. THE adapters SHALL NOT assume that identical effort level names produce
   equivalent reasoning effort across backends.

### Requirement 22: Verification approach per adapter

**User Story:** As a maintainer building on a laptop that has Claude Code but not
Codex, I want a verification approach matched to what each machine has, so that
ClaudeCodeInvoker is verified live while CodexInvoker is verified by
mocked-subprocess tests plus a manual smoke checklist.

#### Acceptance Criteria

1. THE ClaudeCodeInvoker SHALL be covered by mocked-subprocess unit tests for
   command construction, system-prompt delivery, MCP-config emission, tool-less
   enforcement, final-text extraction, usage capture, and failure-mode parity.
2. THE ClaudeCodeInvoker SHALL be covered by live end-to-end verification on the
   local `claude` build confirming tool attachment, fail-loud behavior, JSON
   parsing, and real usage capture.
3. WHERE the local `claude` build is the enterprise-managed_Build, THE live verification SHALL
   record any auth or metering divergence from public Claude Code as a noted
   caveat in the smoke-test checklist and handle it tolerantly (consistent with
   Requirement 7's tolerate-and-warn), rather than baking an enterprise-managed-only code path
   into the adapter.
4. THE CodexInvoker SHALL be covered by mocked-subprocess unit tests for command
   construction, system-prompt delivery, MCP-config emission, tool-less
   enforcement, sandbox flags, final-text extraction, usage capture, and
   failure-mode parity.
5. THE CodexInvoker SHALL be accompanied by a manual smoke-test checklist to run
   on a machine where Codex is installed.
6. WHERE adapter unit tests run, THE tests SHALL mock the subprocess so that no
   live CLI is required.

### Requirement 23: Explorer behavioral equivalence on a new CLI is not assumed

**User Story:** As the owner of the crown-jewel Explorer stage, I want it
acknowledged that MCP-capability does not imply behavioral equivalence, so that
porting the Explorer to a new CLI remains a validation-gated concern rather than
a silent drop-in.

#### Acceptance Criteria

1. WHERE the Explorer is assigned to `claude_code` or `codex`, THE feature SHALL
   treat behavioral equivalence as unproven and gated on slice-quality
   validation.
2. THE feature SHALL satisfy only the capability-eligibility guard for the
   Explorer on a new backend, and SHALL NOT claim slice-quality equivalence.
