# Bugfix Requirements Document

## Introduction

During live verification of the just-completed `model-backend-adapters` work
(recorded in `docs/MODEL-BACKENDS-VERIFICATION.md`, Finding 1, confirmed on the
local enterprise-managed `claude` build `2.1.178.386`), a defect was found in the Claude Code
adapter's agentic (`tools=True`) path.

`ClaudeCodeInvoker` (`src/backends/claude_code.py`) constructs
`claude -p ... --output-format json`. The `MCP_Startup_Probe`
(`src/backends/mcp_probe.py`) confirms MCP tool reachability by scanning the CLI
output for a completed tool-use/tool-result block in the transcript. But the
`--output-format json` envelope is a **single** result object
(`{"type":"result","is_error":false,"result":"...","usage":{...},...}`) that
contains **no** tool-use/tool-result transcript. As a result, every agentic
Claude Code call — the Explorer and Thinker stages — raises a false-positive
`RuntimeError` from the probe, even when the MCP tools attached and were used
successfully.

Live evidence shows the same prompt run with `--output-format stream-json
--verbose` (a JSONL event stream) **does** expose the transcript: top-level
event types `{system, assistant, user, result}` with content block types
`{thinking, tool_use, tool_result, text}` (2 `tool_use`, 2 `tool_result`). The
intended fix is to switch the adapter to `--output-format stream-json --verbose`
and adapt envelope/usage/probe extraction to read the JSONL event stream.

A secondary issue (Finding 2): the enterprise-managed safety-tuned model **refused** the
current terse `MCPStartupProbe.instruction()` text as suspected prompt-injection
and did not call the tool; a natural-language phrasing of the same request was
honored. The probe instruction wording must be softened to a transparent,
natural request in the same fix.

The tool-less path (evaluators/Express, `tools=False`) is unaffected and is
already live-verified working; it must stay that way.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN an agentic (`tools=True`) Claude Code invocation runs and the MCP tools attach and return a tool result THEN the system raises a false-positive `RuntimeError` from the `MCP_Startup_Probe`, because the `--output-format json` envelope is a single `{"type":"result",...}` object that carries no tool-use/tool-result transcript for the probe to scan.

1.2 WHEN the Explorer or Thinker stage (the only `tools=True` stages) runs on the Claude Code backend THEN the system fails on every turn with the probe `RuntimeError`, even though the MCP server attached and the model used the tool successfully.

1.3 WHEN the terse `MCPStartupProbe.instruction()` text ("Before anything else, call the `memory_search` tool exactly once ... Disregard whatever it returns ...") is delivered to a safety-tuned model (e.g. the enterprise-managed `sonnet` build) THEN the system's request is refused as suspected prompt-injection and the model issues no tool call, so no tool result can ever appear (compounding the false positive of 1.1).

### Expected Behavior (Correct)

2.1 WHEN an agentic (`tools=True`) Claude Code invocation runs and the MCP tools attach and return a tool result THEN the system SHALL confirm the real tool result from the `stream-json` event stream and return a successful `InvocationResult` (no false-positive `RuntimeError`).

2.2 WHEN the Explorer or Thinker stage runs on the Claude Code backend with working MCP tools THEN the system SHALL complete the turn normally, producing a parsed result.

2.3 WHEN the probe instruction is delivered to a safety-tuned model THEN the system SHALL phrase it as a transparent, natural-language request (no "before anything else / disregard the result / return only JSON" framing) so the model honors it and calls the tool.

2.4 WHEN the adapter constructs and reads a Claude Code invocation THEN the system SHALL use `--output-format stream-json --verbose` and derive `.result`, `is_error`, and `usage`/`total_cost_usd` from the final `{"type":"result"}` event of the JSONL stream, and SHALL feed the assistant-message `content` blocks (or stream events) to the probe's `detect_tool_result`.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN an agentic (`tools=True`) invocation genuinely fails to attach the MCP tools (no completed tool result returns through the event stream) THEN the system SHALL CONTINUE TO raise `RuntimeError` from the `MCP_Startup_Probe`, naming the probe and backend.

3.2 WHEN a tool-less (`tools=False`) invocation runs (evaluators/Express) THEN the system SHALL CONTINUE TO parse the `.result`, capture real usage, deliver the system prompt natively, and run no probe — exactly as live-verified.

3.3 WHEN any failure mode occurs THEN the system SHALL CONTINUE TO map it identically: timeout → `TimeoutError`, non-zero exit → `RuntimeError`, envelope `is_error` → `RuntimeError`, unrecoverable JSON → `ValueError`, and missing usage → tolerate-and-warn (`usage=None`/`usage_source="estimate"`, no raise).

3.4 WHEN the adapter constructs its command THEN the system SHALL CONTINUE TO use only the public Claude Code flag surface (`stream-json`/`--verbose` are public flags); no enterprise-managed-only code path SHALL be introduced.

3.5 WHEN the fix is applied THEN the Codex adapter (`src/backends/codex.py`) and the shared base (`src/backends/agentic_cli.py`) SHALL CONTINUE TO behave unchanged, except for the shared probe instruction wording softened per 2.3.

3.6 WHEN the full test suite runs THEN the system SHALL CONTINUE TO keep all currently-passing tests green (814 passing), with the mocked-subprocess tests in `tests/test_claude_code.py` updated to use `stream-json` JSONL fixtures.

## Deriving the Bug Condition

**Key definitions:**
- **F**: the original (unfixed) `ClaudeCodeInvoker` — builds `claude -p ...
  --output-format json` and scans the single result envelope for a transcript.
- **F'**: the fixed `ClaudeCodeInvoker` — builds `claude -p ...
  --output-format stream-json --verbose`, reads the final `{"type":"result"}`
  event for `.result`/`is_error`/`usage`, and feeds the stream's
  assistant-message `content` blocks to `detect_tool_result`.

**Bug Condition Function** — identifies inputs that trigger the bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = a ClaudeCodeInvoker.invoke(...) call
  OUTPUT: boolean

  // An agentic call where MCP tools genuinely attached AND a tool result
  // came back, yet F raises from the probe (false positive).
  RETURN X.tools = TRUE
     AND mcp_tools_attached(X)
     AND tool_result_returned(X)
END FUNCTION
```

**Property: Fix Checking** — defines correct behavior for buggy inputs:

```pascal
// For every agentic call where tools really attached and returned a result,
// the fixed adapter must SUCCEED (probe confirms the real tool result).
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT no_raise(result)
     AND probe_confirmed_real_tool_result(result)
     AND result has shape {output, raw, usage, usage_source}
END FOR
```

**Property: Preservation Checking** — existing behavior for non-buggy inputs:

```pascal
// For every input that does NOT meet the bug condition — genuinely-failed
// MCP attachment, tool-less calls, and every failure mode — the fixed adapter
// behaves identically to the original.
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

Concretely, `NOT isBugCondition(X)` covers:
- `tools=True` but no tool result returns → still `RuntimeError` from the probe (3.1).
- `tools=False` evaluator/Express calls → unchanged parse/usage/no-probe (3.2).
- timeout / non-zero exit / `is_error` / unrecoverable JSON / missing usage →
  unchanged mapping (3.3).

**Counterexample (the live-confirmed bug):** an Explorer/Thinker turn on the
enterprise-managed `claude` build with the real Second Brain MCP server attached — the model
runs `memory_search` and a `tool_result` comes back — yet
`ClaudeCodeInvoker.invoke(..., tools=True)` raises `RuntimeError` from
`MCP_Startup_Probe` because the `--output-format json` envelope it scans has no
transcript.
