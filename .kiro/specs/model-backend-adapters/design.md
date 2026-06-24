# Design Document

## Overview

The dream-cycle pipeline already runs on a pluggable model-backend scaffold:
the `Invoker` Protocol, `InvocationResult`, `BackendCapabilities` +
`BACKEND_CAPABILITIES`, the `assert_backend_supports_role` guard, and the shared
`parse_json_output` backstop (all in `src/backends/base.py`); the reference
`KiroInvoker` (`src/backends/kiro.py`); the profile-driven `Resolver`
(`src/backends/resolver.py`); and orchestrator wiring (`_invoker_for(role)` in
`src/dream_cycle/orchestrator.py`). Only `kiro` has a registered adapter today,
so any profile selecting another backend raises `NotImplementedError`.

This feature adds the two agentic-CLI adapters the scaffold was built for —
`ClaudeCodeInvoker` (wraps `claude -p`) and `CodexInvoker` (wraps `codex exec`)
— and registers them in `DEFAULT_ADAPTERS`. No abstraction changes, no
orchestrator changes. `KiroInvoker` is the reference whose **observable
behavior** (return shape and the exceptions it raises) the new adapters must
match, because the orchestrator's evaluator retry/abort logic depends on that
contract being identical across backends.

The change is **default-preserving**: with the `laptop` profile (all-Kiro), the
existing suite must pass unchanged (Req 20).

## Architecture

### Before

```
config/backends.toml ── Resolver ── DEFAULT_ADAPTERS = { "kiro": KiroInvoker }
                                          │
orchestrator._invoker_for(role) ─────────┘   (claude_code / codex → NotImplementedError)
                                          ▼
                                    KiroInvoker.invoke()  ── kiro-cli subprocess
```

### After

```
                         src/backends/base.py
        Invoker Protocol · InvocationResult · BACKEND_CAPABILITIES
        assert_backend_supports_role · parse_json_output (shared backstop)
                                   │
              ┌────────────────────┼─────────────────────────────┐
              ▼                    ▼                              ▼
        KiroInvoker         ClaudeCodeInvoker                CodexInvoker
        (unchanged)         (new: claude -p)                 (new: codex exec)
              │                    │                              │
              └──────── AgenticCliInvoker (shared base) ──────────┘
                    spawn · timeout→TimeoutError · lenient decode ·
                    metrics JSONL · cleanup · parse backstop · usage normalize
                                   │
   DEFAULT_ADAPTERS = { kiro, claude_code, codex }   ← only change in resolver.py
                                   │
            orchestrator._invoker_for(role)  ← unchanged; resolves per active profile
```

`config/backends.toml` already ships a `mini` profile selecting `claude_code`;
once registered it resolves to a working invoker. Selecting `codex` requires
only a profile that names it (none ships by default; adding one is config-only).

### Import safety

Both new modules live in `src/backends/` and import only from
`src/backends/base.py` (stdlib otherwise), exactly like `kiro.py`. `resolver.py`
already imports `KiroInvoker`; adding imports of the two new adapters introduces
no cycle.

## Components and Interfaces

### 1. Shared base: `AgenticCliInvoker` (recommended — extract now)

**Decision: extract a thin `AgenticCliInvoker` base in `base.py` (or a new
`src/backends/agentic_cli.py`) now, rather than build two standalone adapters.**

Rationale: `KiroInvoker` already contains the shared agentic-CLI mechanics
inline — subprocess spawn with `capture_output`/`text`/`errors="replace"`,
`subprocess.TimeoutExpired → TimeoutError`, non-zero exit → `RuntimeError`, the
per-call metrics JSONL (`logs/llm_metrics/<run_id>.jsonl`), raw-output debug
dump, temp-config cleanup in a `finally`, and the `parse_json_output` backstop.
Building two more adapters that each re-implement this invites the exact
failure-mode drift Req 8/17 forbid (one adapter silently diverging on, say,
timeout handling). A shared base makes failure-mode parity structural rather
than a per-adapter promise.

The base owns:

- `_run_subprocess(cmd, *, timeout, stdin=None)` → completed process, mapping
  `TimeoutExpired → TimeoutError`, decode leniently.
- `_record_metrics(...)` → the existing JSONL writer, extended with
  `usage_source` and real token counts when available.
- `_finalize(raw, *, usage, usage_source)` → run `parse_json_output`, build the
  `InvocationResult`, raise `ValueError` if no JSON recoverable.
- a `_warn_missing_usage(...)` helper implementing the tolerate-and-warn path.

Each adapter overrides only: **command construction**, **system-prompt
delivery**, **MCP config emission**, **final-text extraction**, **the effort
flag**, **usage extraction**, and **failure-mode mapping** (envelope checks).

Constraint: the extraction must be **behavior-identical for the Kiro path** (Req
20). `KiroInvoker` is refactored to call the base helpers but keep its current
command surface (`--no-interactive --agent --model`, `--trust-all-tools
--require-mcp-startup`, the MCP-startup retry, `usage=None`/`usage_source=
"estimate"`). The full suite green on the `laptop` profile is the gate.

If, during implementation, the extraction proves to perturb the Kiro path, the
fallback is to implement the two adapters standalone and share only
`parse_json_output` (already shared) plus a small `_subprocess` helper — but the
base is preferred for parity.

### 2. `ClaudeCodeInvoker` (`src/backends/claude_code.py`, new)

Constructed as `ClaudeCodeInvoker(model=<id>)` (Req 1.2). Implements
`invoke(system_prompt, user_message, *, tools=False, timeout=300, effort=None,
stage=None, run_id=None) -> InvocationResult` (Req 1.1).

**Command construction (public flag surface — Req 2, the contract):**

Common: `claude -p <user_message> --model <id> --output-format json`
`[--effort <level>]` `[--json-schema <file>]`, system prompt delivered via
`--system-prompt-file <file>` (preferred) or `--append-system-prompt` (Req 3.1).
The final text is the envelope `.result` field (Req 2.3); parsed via
`parse_json_output`, preferring a schema-validated payload when `--json-schema`
was used (Req 9).

- **tools=True (Explorer/Thinker):** add `--mcp-config <sb.json>`
  `--strict-mcp-config`, where the server entry is
  `{ command: <python>, args: ["-m","src.mcp_server"], cwd: <repo root> }`
  (Req 4). Permission mode set so tool use is non-interactive.
- **tools=False (evaluators/Express):** pass `--strict-mcp-config` **without**
  `--mcp-config` (no MCP servers load at all) plus `--tools ""` (disable
  built-in tools), and no probe (Req 6). NOTE: `--tools ""` alone is
  insufficient — per the public Claude Code reference it disables only built-in
  tools, not MCP tools; the `--strict-mcp-config`-without-`--mcp-config` form is
  what guarantees no MCP tools load.

The adapter invokes a **configurable `claude` binary path** (`CLAUDE_CLI`,
default `claude`), mirroring `KiroInvoker`'s `KIRO_CLI`, so it runs against a
public Claude Code install or the enterprise-managed wrapper unchanged. It uses **only public
flags** and depends on none of the enterprise-managed provider wrapper's surface
(`--aws-profile`, Bedrock routing, `--claude-help`); auth/provider routing
(Claude subscription, `ANTHROPIC_API_KEY`, or standard Bedrock env vars) is
supplied out-of-band via environment (Req 2.7, 2.8). The local **enterprise-managed build** is
the build-time verification environment only; any enterprise-managed auth/metering divergence
is isolated behind config/env, never an enterprise-managed-only code path (Req 2.5/2.6,
22.3).

**Fail-loud (Req 5):** when `tools=True`, run the **MCP_Startup_Probe** (§5)
before trusting the result; raise `RuntimeError` if the probe doesn't confirm
reachability or if the envelope reports `is_error`. Process-attach alone is
insufficient. The probe runs only on `tools=True` and is **not cached** across
invocations (each `invoke()` is a fresh subprocess).

**Usage (Req 7 — tolerate-and-warn):** on success with envelope `usage` present,
populate `usage` from `usage`/`total_cost_usd` and set `usage_source="real"`. On
success with a parseable payload but **no** `usage`, keep `output`/`raw`, set
`usage=None`, `usage_source="estimate"`, and log a loud warning — never raise or
route into the abort path.

**Failure-mode mapping (Req 8):** timeout → `TimeoutError`; non-zero exit →
`RuntimeError`; envelope `is_error` → `RuntimeError`; no recoverable JSON →
`ValueError`. Never an empty result or fabricated verdict.

### 3. `CodexInvoker` (`src/backends/codex.py`, new)

Constructed as `CodexInvoker(model=<id>)` (Req 10.2). Same `invoke` signature.
Designed against the **documented** `codex exec` surface (Codex is not installed
locally), so it is verified by mocked-subprocess tests + a manual smoke checklist
(Req 22.4/22.5).

**Command construction (Req 11):** `codex exec <user_message> -m <id>`
`[-c model_reasoning_effort=<level>]`, final text via
`--output-last-message <file>` (preferred) or the `--json` event stream;
`--output-schema <file>` where schema-constrained output is required; system
prompt via `developer_instructions` / `instructions` /
`model_instructions_file` (Req 12).

- **tools=True:** config `[mcp_servers.second_brain] command=<python>
  args=["-m","src.mcp_server"] cwd=<repo root> required=true`, plus
  `--sandbox workspace-write` and `sandbox_workspace_write.network_access=true`
  so the MCP server can reach Postgres/Bedrock (Req 13).
- **tools=False:** no `mcp_servers` config, `--sandbox read-only`, no probe
  (Req 15).

**Fail-loud (Req 14):** `required=true` is the **primary** guard (Codex
hard-fails at startup, before any model call, if the server can't start). The
**MCP_Startup_Probe** is the **secondary** check for the case `required=true`
can't catch — the server starts but the sandbox blocks its network/DB access —
so the probe's trivial tool call exercises actual Postgres/Bedrock reachability.
Probe only on `tools=True`, not cached.

**Usage (Req 16):** from `--json` usage events; same tolerate-and-warn fallback
to `usage=None`/`usage_source="estimate"`/warn when absent.

**Failure-mode mapping (Req 17):** timeout → `TimeoutError`; non-zero exit →
`RuntimeError`; no recoverable JSON → `ValueError`.

### 4. The MCP_Startup_Probe (§ shared)

A lightweight, deterministic reachability check used only on agentic
(`tools=True`) invocations.

- **What it calls:** one trivial, read-only tool that exercises the real path to
  the database — a minimal `memory_search` (e.g. a fixed nonsense query,
  `limit=1`) or `memory_list(limit=1)`. The call must reach Postgres, so a
  sandbox that blocks network/DB fails the probe (this is the Codex case
  `required=true` cannot detect).
- **How attachment is confirmed:** the probe is satisfied only by a successful
  tool *result* coming back through the CLI envelope/event stream — not by the
  server process merely being spawned (Req 5.4, 14.3). For Claude Code this means
  inspecting the JSON envelope for a completed tool-use with a result and no
  `is_error`; for Codex, a completed tool call in the `--json` events.
- **Failure → `RuntimeError`** with a message naming the probe and backend, so
  the orchestrator treats it as an infrastructure failure (consistent with
  KiroInvoker's `--require-mcp-startup` exit-3 behavior), never a verdict.
- **Not cached:** each `invoke()` spawns a fresh subprocess with independent MCP
  startup; a cached "healthy" flag would mask exactly the per-process
  attach/sandbox-block failures this guards against (Req 5.6, 14.5).
- **Cost-bounded:** probes run only on the low-volume Explorer/Thinker path,
  never on the ~30 tool-less evaluator calls per run.

Implementation note: the cheapest sound form folds the probe into the agentic
turn by instructing the stage to begin with the trivial tool call and verifying
that tool-use appears in the transcript; a dedicated pre-call probe is the
fallback where transcript inspection is unreliable. Either way the *signal* is "a
tool result actually returned," not "a process attached."

### 5. Resolver registration (`src/backends/resolver.py`)

The only change to the resolver is registration:

```python
from src.backends.claude_code import ClaudeCodeInvoker
from src.backends.codex import CodexInvoker

DEFAULT_ADAPTERS: dict[str, type] = {
    "kiro": KiroInvoker,
    "claude_code": ClaudeCodeInvoker,
    "codex": CodexInvoker,
}
```

Everything else already works for the new backends with no logic change (Req 19):
- per-`(backend, model)` invoker caching (`Resolver._cache`),
- the eager Explorer guard at construction and the lazy guard in `invoker_for`
  (both pass because `claude_code`/`codex` declare `supports_mcp=True`),
- profile validation (`_validate_profile`) — backends are already in
  `BACKEND_CAPABILITIES`.

## Data Models

No database or schema changes. The only data shapes involved are existing:

- **`InvocationResult`** (`{output, raw, usage, usage_source}`) — unchanged
  shape; the new adapters populate `usage` (real) or fall back to
  `None`/`"estimate"`.
- **Per-call metrics JSONL** (`logs/llm_metrics/<run_id>.jsonl`) — the new
  adapters write the same record shape as KiroInvoker, populating
  `usage_source` and real token counts when the backend reports them. This keeps
  cross-backend cost analysis honest (never mixing estimate and real silently).
- **Temp config artifacts** — Claude Code MCP JSON and Codex config TOML written
  to a temp path and cleaned up in `finally`, mirroring KiroInvoker's agent-JSON
  lifecycle.

## Correctness Properties

Executable properties the implementation must satisfy (validated by the test
suite):

### Property 1: Default-preserving Kiro path
With the `laptop` profile, every role resolves to `KiroInvoker` and the full
existing suite passes unchanged.

**Validates: Requirements 20.1, 20.2**

### Property 2: Contract conformance
Both adapters satisfy the `Invoker` Protocol signature and return an
`InvocationResult` with exactly `{output, raw, usage, usage_source}`.

**Validates: Requirements 1.1, 1.3, 10.1, 10.3**

### Property 3: Tool-gating
`tools=False` ⇒ no MCP config emitted, and tool access disabled the documented
way — Claude `--strict-mcp-config` without `--mcp-config` (no MCP servers load)
plus `--tools ""`; Codex no `mcp_servers` + `--sandbox read-only` — and **no
probe**. `tools=True` ⇒ MCP config emitted with `cwd=repo` and a probe is
performed.

**Validates: Requirements 4.1, 6.1, 6.2, 13.1, 15.1, 5.5, 14.5**

### Property 4: Tolerate-and-warn on missing usage
A successful, parseable response with no usage telemetry yields a preserved
result with `usage=None`, `usage_source="estimate"`, a warning, and **no raise**.

**Validates: Requirements 7.2, 7.3, 16.2, 16.3**

### Property 5: Failure-mode parity
For each backend, timeout → `TimeoutError`; non-zero exit → `RuntimeError`;
Claude `is_error` / failed probe → `RuntimeError`; unrecoverable JSON →
`ValueError`. Never an empty result or fabricated verdict.

**Validates: Requirements 8.1, 8.2, 8.4, 17.1, 17.2, 17.3, 5.2, 14.2**

### Property 6: Public-flag contract
ClaudeCodeInvoker's constructed command uses only flags documented in the public
Claude Code CLI reference; no enterprise-managed-only flag (`--aws-profile`, the Cecelia
wrapper surface) is required for correctness, and the binary path is
configurable (`CLAUDE_CLI`).

**Validates: Requirements 2.5, 2.6, 2.7, 2.8**

### Property 7: Probe is not cached
Two sequential agentic invocations each perform their own probe; a forced
probe-failure on the second raises even if the first succeeded.

**Validates: Requirements 5.6, 14.5**

### Property 8: Resolver registration
A profile assigning a role to `claude_code`/`codex` resolves to the correct
adapter (not `NotImplementedError`), cached per `(backend, model)`; Explorer on
either passes the guard.

**Validates: Requirements 19.1, 19.2, 19.3, 19.4**

### Property 9: Effort is provenance
Effort is passed to the CLI and recorded; no cross-backend equivalence assumed.

**Validates: Requirements 21.1, 21.2, 21.3**

## Error Handling

| Failure | ClaudeCodeInvoker | CodexInvoker | Shared mapping |
|---|---|---|---|
| Subprocess exceeds `timeout` | `TimeoutError` | `TimeoutError` | base `_run_subprocess` maps `TimeoutExpired` |
| Non-zero CLI exit | `RuntimeError` | `RuntimeError` | base checks returncode |
| Envelope `is_error` true | `RuntimeError` | n/a (no envelope flag) | adapter envelope check |
| MCP probe: no tool result | `RuntimeError` | `RuntimeError` (secondary) | probe helper |
| Codex `required=true` startup fail | n/a | `RuntimeError` (primary, via non-zero exit) | returncode |
| No recoverable JSON | `ValueError` | `ValueError` | `parse_json_output` |
| Missing usage on success | tolerate: `usage=None`/`estimate`/warn | same | `_warn_missing_usage` (NOT an error) |
| Empty/blank model id | construction-time error | construction-time error | adapter validates `model` |

The cardinal rule: an **infrastructure** failure always raises; a **telemetry**
gap (missing usage) never does. This preserves the evaluator retry/abort
integrity (a crashed evaluator must never become a fabricated REJECT).

## Testing Strategy

1. **Baseline:** run the full suite on the `laptop` profile before and after the
   KiroInvoker refactor to prove Property 1 (behavior preservation).
2. **Mocked-subprocess unit tests (both adapters)** — mock `subprocess.run` so no
   live CLI is needed. Cover: command construction (public flags / `codex exec`
   surface), tools=True vs tools=False command shapes, system-prompt delivery
   (native + the prepend fallback), MCP-config emission with `cwd=repo`, Codex
   sandbox flags, `--strict-mcp-config` / `required=true`, final-text extraction
   (`.result` / `--output-last-message` / `--json` events), usage capture and the
   tolerate-and-warn fallback, failure-mode parity for every row of the table
   above, and probe behavior (performed on tools=True, skipped on tools=False,
   not cached).
3. **Shared parser tests per backend envelope** — feed each backend's
   representative envelope (prose-wrapped, fenced, tool-transcript) through
   `parse_json_output`; reuse the existing property tests.
4. **Resolver/guard registration tests** — `claude_code`/`codex` resolve to the
   right adapter and cache per `(backend, model)`; Explorer on either passes the
   guard; a tool-less Direct-API profile still fails fast (unchanged).
5. **Live verification — Claude Code (local enterprise-managed build)** — confirm tool
   attachment via the probe, fail-loud on a forced MCP failure, JSON parsing of a
   real `.result`, and real usage capture. Record any enterprise-managed auth/metering
   divergence as a noted caveat in the smoke checklist and handle it tolerantly;
   do not bake an enterprise-managed-only path (Req 22.3).
6. **Manual smoke-test checklist — Codex** — to run on a Codex-equipped box:
   verify `required=true` startup-fail behavior, the sandbox `network_access`
   path reaches Postgres/Bedrock, `--output-last-message` extraction, and usage
   events.
7. **DB-touching probe tests** may use the existing `test_db`/`clean_tables`/
   `mock_embedding` fixtures from `tests/conftest.py`.

Negative check: temporarily force a probe failure / a non-zero exit / an
`is_error` envelope and confirm the mapped exception propagates (not a verdict),
then revert.

## Scope Boundaries (carried from requirements)

- Out: direct-API adapters (Bedrock/OpenAI/Anthropic `InvokeModel`), per-role
  model selection, evaluator-panel model diversity, the embedding path.
- Explorer behavioral equivalence on a new CLI is **capability-eligible only**,
  not slice-quality-validated here (Req 23); slice-quality validation is a gated
  follow-on (the replay harness in docs/MODEL-BACKENDS.md).
