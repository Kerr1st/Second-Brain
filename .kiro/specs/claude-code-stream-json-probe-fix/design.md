# Claude Code stream-json probe fix — Bugfix Design

## Overview

The Claude Code agentic path is broken: every `tools=True` call (Explorer,
Thinker) raises a false-positive `RuntimeError` from the `MCP_Startup_Probe`
even when the MCP server attached and the model used the tool successfully. The
root cause is confirmed live (`docs/MODEL-BACKENDS-VERIFICATION.md`, Finding 1):
the adapter builds `claude -p … --output-format json`, whose envelope is a
**single** `{"type":"result", …}` object that carries **no** tool-use /
tool-result transcript. The probe scans for a completed `tool_result` block,
finds none, and fails loud — a false positive.

A second, compounding defect (Finding 2): the terse, injection-styled
`MCPStartupProbe.instruction()` ("Before anything else … Disregard whatever it
returns …") is refused outright by the safety-tuned enterprise-managed `sonnet` build as a
suspected prompt-injection, so the model never even issues the tool call.

The fix has two coordinated parts:

1. **Switch the output format to `--output-format stream-json --verbose`** (both
   tool-using and tool-less calls, for a single extraction path) and rework
   envelope/`.result`/`is_error`/`usage` extraction to read the JSONL event
   stream, locating the terminal `{"type":"result"}` event. Feed the stream's
   events (which include the `tool_result` content blocks) to the probe's
   `detect_tool_result`.
2. **Soften `MCPStartupProbe.instruction()`** to a transparent, natural-language
   request that drops the injection-flagged framing, while retaining the
   `PROBE_NAME` / `TOOL` / `QUERY` substrings so the request stays self-describing
   and the broad test suite stays green.

The change touches only the **public** Claude Code flag surface
(`stream-json` and `--verbose` are confirmed present on the live build) and
introduces **no enterprise-managed-only code path**. The tool-less behavior, the genuine
fail-loud guard, and every failure-mode mapping are preserved.

## Glossary

- **Bug_Condition (C)**: an agentic (`tools=True`) `ClaudeCodeInvoker.invoke`
  call where the MCP tools genuinely attached **and** a tool result came back,
  yet the original adapter raises a false-positive `RuntimeError` from the probe.
- **Property (P)**: the desired behavior under C — confirm the real tool result
  from the `stream-json` event stream and return a successful
  `InvocationResult` (no false-positive raise).
- **Preservation**: every non-buggy input behaves identically — genuine MCP
  failure still raises, tool-less calls are unchanged, and timeout / non-zero
  exit / `is_error` / unrecoverable JSON / missing usage all map exactly as
  before.
- **F**: the original (unfixed) `ClaudeCodeInvoker` — `--output-format json`,
  scans the single result envelope for a transcript.
- **F'**: the fixed `ClaudeCodeInvoker` — `--output-format stream-json
  --verbose`, reads the terminal `{"type":"result"}` event for
  `.result`/`is_error`/`usage`, and feeds the stream events to
  `detect_tool_result`.
- **stream-json event**: one JSON object per line (JSONL). Top-level event types
  `{system, assistant, user, result}`; assistant/user `message.content` blocks of
  types `{thinking, tool_use, tool_result, text}`. The stream ends with one
  terminal `{"type":"result", "is_error":…, "result":…, "usage":…, …}` event.
- **`_envelope_from`**: helper in `src/backends/claude_code.py` that recovers the
  one "envelope" object the rest of the adapter reasons over. After the fix it
  returns the terminal `{"type":"result"}` event of the JSONL stream.
- **`_extract_raw`**: returns the final assistant text (the terminal result
  event's `.result`) for the shared `parse_json_output` backstop.
- **`MCP_Startup_Probe`**: shared, stateless reachability probe
  (`src/backends/mcp_probe.py`). Confirms a tool *result* actually returned;
  raises `RuntimeError` naming the probe + backend when it cannot.

## Bug Details

### Bug Condition

The bug manifests on any agentic (`tools=True`) Claude Code turn where the MCP
server attached and a `memory_search` tool result genuinely returned. The
adapter reads the `--output-format json` envelope — a single
`{"type":"result", …}` object with no transcript — so the probe's
`detect_tool_result` finds no `tool_result` block and `_run_probe` raises a
false-positive `RuntimeError`. Because Explorer and Thinker are the only
`tools=True` stages, the bug fires on **every** agentic turn.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = a ClaudeCodeInvoker.invoke(...) call
  OUTPUT: boolean

  RETURN X.tools = TRUE
     AND mcp_tools_attached(X)          // server attached
     AND tool_result_returned(X)        // a real tool_result came back
END FUNCTION
```

Under the original `F`, `isBugCondition(X)` ⇒ `F` raises `RuntimeError` from the
probe (the defect). Under `F'`, `isBugCondition(X)` ⇒ success.

### Examples

- **Explorer turn, real MCP server (the live-confirmed counterexample)**: model
  runs `memory_search`, a `tool_result` comes back, the turn completes — yet
  `invoke(..., tools=True)` raises `RuntimeError` from `MCP_Startup_Probe`
  because the `json` envelope has no transcript. *Expected:* success with a
  parsed `output`.
- **Thinker turn, working tools**: same false-positive raise on every turn.
  *Expected:* normal completion.
- **Safety-tuned model + terse instruction (Finding 2)**: the model refuses the
  "Before anything else … Disregard …" instruction as injection, issues no tool
  call, so no `tool_result` can ever appear. *Expected:* a natural-language
  instruction the model honors, producing a real tool call + result.
- **Edge — genuine MCP failure** (NOT the bug condition): server fails to
  attach, no `tool_result` returns. *Expected (unchanged):* `RuntimeError` from
  the probe.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- A genuinely-failed `tools=True` attach (no `tool_result` in the stream) MUST
  continue to raise `RuntimeError` from `MCP_Startup_Probe`, naming the probe and
  backend (Req 3.1).
- A tool-less (`tools=False`) call (evaluators/Express) MUST continue to: parse
  the final `.result`, capture real usage, deliver the system prompt natively,
  and run **no** probe (Req 3.2).
- Every failure mode MUST map identically: timeout → `TimeoutError`, non-zero
  exit → `RuntimeError`, envelope `is_error` → `RuntimeError`, unrecoverable
  JSON → `ValueError`, missing usage → tolerate-and-warn (`usage=None` /
  `usage_source="estimate"`, no raise) (Req 3.3).
- Only the **public** Claude Code flag surface is used; no enterprise-managed-only code path is
  introduced (Req 3.4). The enterprise-managed usage-divergence tolerance (copy the whole
  `usage` dict, fold in `total_cost_usd`, no enterprise-managed-only branch) is preserved.
- The Codex adapter (`src/backends/codex.py`) and the shared base
  (`src/backends/agentic_cli.py`) behave unchanged, except that Codex inherits
  the softened shared probe instruction wording (Req 3.5).
- The full suite stays green (814 passing); only `tests/test_claude_code.py`
  fixtures are reworked to `stream-json` JSONL (Req 3.6).

**Scope:**
All inputs that are NOT the bug condition must be completely unaffected by this
fix:
- `tools=False` evaluator/Express calls (no probe, unchanged extraction).
- Any failure mode (timeout, non-zero exit, `is_error`, unrecoverable JSON,
  missing usage).
- `tools=True` calls where tools genuinely did not attach (still fail loud).

The actual expected correct behavior for the bug condition is defined in the
Correctness Properties section (Property 1).

## Hypothesized Root Cause

Finding 1 (live-confirmed) already isolates the root cause, so this is
verification rather than open hypothesis:

1. **Output format lacks the transcript (primary, confirmed)**: `--output-format
   json` emits a single `{"type":"result", …}` object with no `tool_use` /
   `tool_result` blocks. The probe's `detect_tool_result` has nothing to confirm,
   so it raises. Live evidence: the same prompt + MCP config under
   `--output-format stream-json --verbose` exposes `{system, assistant, user,
   result}` events with `{thinking, tool_use, tool_result, text}` content blocks
   (2 `tool_use`, 2 `tool_result`).

2. **Instruction refused as injection (secondary, confirmed — Finding 2)**: the
   terse `instruction()` framing ("Before anything else … Disregard whatever it
   returns …") trips the safety-tuned model's prompt-injection heuristics; the
   model issues no tool call, guaranteeing no `tool_result` even when tools work.

3. **Extraction reads the wrong object (consequence)**: `_envelope_from`,
   `_extract_raw`, and `_extract_usage` all assume a single parseable envelope on
   stdout. Against a JSONL stream they must instead locate the terminal result
   event; naively running the shared backstop over full JSONL stdout would
   recover the *largest* balanced JSON object (an interior event), not the final
   answer.

The fix addresses (1) by switching the format and reworking extraction, and (2)
by rewording the instruction.

## Correctness Properties

Property 1: Bug Condition — stream-json probe confirms real tool results

_For any_ agentic (`tools=True`) input where the bug condition holds
(`isBugCondition` returns true — MCP tools attached and a `tool_result` came
back), the fixed `ClaudeCodeInvoker` SHALL parse the `--output-format stream-json
--verbose` event stream, confirm the real `tool_result` via the probe's
`detect_tool_result`, and return a successful `InvocationResult`
`{output, raw, usage, usage_source}` with no false-positive `RuntimeError`. The
probe instruction SHALL be a transparent, natural-language request (no "before
anything else / disregard the result / return only JSON" framing) so a
safety-tuned model honors it and issues the tool call.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation — non-buggy inputs behave identically

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns
false), the fixed `ClaudeCodeInvoker` SHALL produce the same observable result
as the original: a genuinely-failed `tools=True` attach still raises
`RuntimeError` from `MCP_Startup_Probe`; a `tools=False` call still parses
`.result`, captures real usage, delivers the system prompt natively, and runs no
probe; and timeout → `TimeoutError`, non-zero exit → `RuntimeError`, `is_error` →
`RuntimeError`, unrecoverable JSON → `ValueError`, and missing usage →
tolerate-and-warn are all preserved, using only public flags with no enterprise-managed-only
path and no behavior change to Codex or the shared base.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming the (live-confirmed) root cause, the fix is contained to two files.

**File**: `src/backends/claude_code.py`

1. **`_build_command` — output format (Req 2.4, 3.4)**: replace
   ```
   "--output-format", "json",
   ```
   with
   ```
   "--output-format", "stream-json", "--verbose",
   ```
   `--verbose` is required for `stream-json` to emit the full event stream (per
   the live repro). Both flags are public and present on build `2.1.178.386`
   (verification Part A: the flag surface was re-confirmed against this build,
   and the `stream-json --verbose` repro ran successfully). Applied to **both**
   `tools=True` and `tools=False` so there is a single extraction path (see
   "Single code path" below).

2. **New helper `_events_from(stdout) -> list | None`**: parse the JSONL stream
   into a list of event dicts, one per line, skipping non-JSON lines (mirrors
   the existing `CodexInvoker._events_from`). Returns `None` when nothing parses.

3. **`_envelope_from(result)` — terminal result event (Req 2.4)**: stop calling
   the backstop on full stdout. Instead parse events via `_events_from` and
   return the **last** event whose `type == "result"`; return `None` when no
   result event exists. This is the one consistent way the rest of the adapter
   recovers `.result` / `is_error` / `usage`.

4. **`_extract_raw(result)` — final text (Req 2.4, 9.3)**: from the terminal
   result event, return `.result` (a `str` as-is; otherwise `json.dumps`). When
   there is **no** result event (or it carries no `.result`), return `""` so the
   shared `parse_json_output` backstop raises `ValueError` — NOT `result.stdout`,
   because running the backstop over full JSONL would wrongly recover an interior
   event as the answer (see "No result event" decision below).

5. **`_extract_usage(...)` — usage from the result event (Req 7, 3.3)**:
   unchanged in spirit — it already calls `_envelope_from(result)`. Because
   `_envelope_from` now returns the terminal result event, `usage` /
   `total_cost_usd` are read from that event. Keep the tolerate-and-warn
   semantics verbatim (real `usage` → copy the **whole** dict + fold in
   `total_cost_usd` → `usage_source="real"`; missing `usage` → `None` /
   `"estimate"` + loud warning, never raise). The whole-dict copy preserves the
   enterprise-managed divergence tolerance with no enterprise-managed-only branch.

6. **`_run_probe(...)` — feed stream events to the probe (Req 2.4, 5, 8.3)**:
   - `is_error` check unchanged: `_envelope_from(result)` → terminal result
     event; if `is_error is True` raise `RuntimeError` (kept unconditional so it
     fires even for `tools=False`, preserving `test_is_error_raises_even_when_tools_false`).
   - Replace `parsed=envelope` with `events=self._events_from(result.stdout)` in
     the `MCPStartupProbe.run(...)` call, and keep `raw=result.stdout` as the
     fallback. Rationale: the `tool_result` blocks live in the assistant/user
     message events, **not** the terminal result event, so passing the result
     event as `parsed` would never confirm. `detect_tool_result` recurses into
     the event list (`_scan`) and finds nested `tool_result` blocks regardless of
     wrapping; `raw` JSONL scanning (`_scan_text`) is the backstop. The terminal
     `result` event in the list is correctly ignored (its type is not a
     `tool_result` marker).

**File**: `src/backends/mcp_probe.py`

7. **`MCPStartupProbe.instruction()` — natural-language rewording (Req 2.3,
   3.5)**: rewrite the text to a transparent request that drops the
   injection-flagged framing ("Before anything else", "Disregard whatever it
   returns", "return only JSON"). Constraints:
   - MUST retain the `TOOL` (`memory_search`) and `QUERY`
     (`__mcp_startup_probe__`) substrings (keeps
     `test_mcp_probe.py::test_instruction_mentions_trivial_tool_call` green).
   - SHOULD retain the `PROBE_NAME` (`MCP_Startup_Probe`) substring as a
     named/parenthetical label for the check (keeps the
     `PROBE_NAME in prompt_input` assertions in `test_claude_code.py` **and**
     `test_codex.py` green, so Codex needs no code or test change — Req 3.5).
   - The wording is generic (no Claude/Codex specifics) since both adapters
     prepend it via their `_prompt_input`.

   Proposed text (illustrative):
   ```
   To confirm your memory tools are connected and working, please begin by
   using the `memory_search` tool once to look up "__mcp_startup_probe__"
   (limit 1). It's fine if it finds nothing — this is just a quick startup
   check (MCP_Startup_Probe). After that, go ahead and complete the task
   normally.
   ```
   This phrasing is a polite, explained request (matching the Finding-2 phrasing
   the live model honored), retains all three tokens, and removes the
   imperative/secrecy framing the safety model flagged.

### Single code path (tool-less also moves to stream-json) — decision

`tools=False` moves to `--output-format stream-json --verbose` together with
`tools=True`, giving one extraction path. Justification:

- `_envelope_from`, `_extract_raw`, and `_extract_usage` receive only `result`
  (no `needs_tools`). Keeping tool-less on `json` would require branching all
  three on the tool flag and threading `needs_tools` through them — strictly more
  complex and more error-prone than one format.
- The terminal `{"type":"result"}` event is present on **every** completed
  `stream-json --verbose` turn, tool-using or not, so `.result` / `is_error` /
  `usage` extraction works identically for a no-tool turn.
- Req 3.2 preserves tool-less *behavior* (parse `.result`, capture real usage,
  native prompt, no probe) — all of which hold under `stream-json`; it does not
  pin the literal `--output-format` value.

Trade-off acknowledged: the tool-less path was live-verified on `json`, not
`stream-json`; the live verification of `stream-json` itself (Finding 1 repro)
gives confidence the terminal result event + usage are present. The mocked tests
assert behavior (parsed `output`, `usage_source`, no probe), which the new
JSONL fixtures reproduce.

### "No result event" edge case — decision

A completed (exit 0) `stream-json --verbose` turn is required by the format to
end with exactly one terminal `{"type":"result"}` event. Its absence is a
malformed/truncated stream. Decision: route it through the existing
**`ValueError`** backstop (do not invent a new exception, keeping the
failure-mode table unchanged per 3.3/3.4): `_envelope_from` returns `None`,
`_extract_raw` returns `""`, and the shared `parse_json_output("")` raises
`ValueError("No valid JSON found …")`. Returning `""` (rather than full JSONL
stdout) is deliberate — it prevents the backstop from recovering an interior
event as a bogus answer. The `is_error` ordering is preserved: a result event
with `is_error=True` and a parseable `.result` flows through extraction to
`_run_probe`, which raises `RuntimeError` (matching the current json-mode tests);
the fixtures provide a parseable `.result` so `RuntimeError`, not `ValueError`,
wins for the `is_error` rows.

## Testing Strategy

### Validation Approach

Two phases. First, surface counterexamples that demonstrate the bug on the
**unfixed** adapter using `stream-json` JSONL fixtures (the original code scans
for a transcript the `json` envelope never had, and would also mis-handle the
JSONL stream). Then verify the fix succeeds for the bug condition and preserves
all non-buggy behavior, keeping the full suite green (814 passing) as the gate.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the false-positive BEFORE the
fix, confirming Finding 1's root cause. If a fixture passes on unfixed code,
re-examine the root cause.

**Test Plan**: Build `stream-json` JSONL fixtures (system → assistant `tool_use`
→ user `tool_result` → terminal `result`) and assert a `tools=True` invoke
succeeds. On the **unfixed** adapter (which builds `--output-format json` and
reads a single envelope) these fixtures fail — either the probe raises (no
transcript recognized in its envelope-shaped scan) or extraction reads the wrong
object — reproducing the live false positive.

**Test Cases**:
1. **Agentic turn with real tool result**: stream with a `tool_result` block +
   terminal result → expect success on F' (fails on F). (will fail on unfixed code)
2. **Probe instruction honored**: instruction text contains no injection-flagged
   phrases yet retains `TOOL`/`QUERY`/`PROBE_NAME`. (asserts the Finding-2 fix)
3. **Terminal result extraction**: `.result` is read from the final result
   event, not from an interior assistant/system event. (will fail on unfixed code)
4. **Edge — no result event**: a stream missing the terminal result event →
   `ValueError`. (new edge case)

**Expected Counterexamples**:
- Unfixed: `tools=True` + real `tool_result` in a `stream-json` fixture → false
  `RuntimeError` from `MCP_Startup_Probe`.
- Possible causes (per Finding 1): output format lacks transcript; extraction
  reads the single-object envelope; instruction refused as injection.

### Fix Checking

**Goal**: For all inputs meeting the bug condition, the fixed adapter produces
the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := ClaudeCodeInvoker_fixed.invoke(X)   // tools=True, tool_result present
  ASSERT no_raise(result)
     AND probe_confirmed_real_tool_result(result)
     AND shape(result) = {output, raw, usage, usage_source}
END FOR
```

### Preservation Checking

**Goal**: For all inputs NOT meeting the bug condition, the fixed adapter behaves
identically to the original.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```
Concretely `NOT isBugCondition(X)` covers: `tools=True` with no `tool_result`
(still `RuntimeError`); `tools=False` (unchanged parse/usage/no-probe); and
timeout / non-zero exit / `is_error` / unrecoverable JSON / missing usage
(unchanged mapping).

**Testing Approach**: Property-based testing suits preservation — generate
varied JSONL streams (random interleavings of `thinking`/`text`/`tool_use`
blocks, with and without a `tool_result`, with and without `usage`) and assert
the success/raise decision and extracted `.result`/`usage` match the documented
contract. It exercises far more interleavings than the hand-written fixtures and
guards the `detect_tool_result` recursion against stream-shape drift.

**Test Plan**: Reproduce the live `stream-json` shapes as fixtures, assert the
preserved behaviors, then add property-based generators over stream shapes for
the success-vs-raise and extraction invariants.

**Test Cases**:
1. **Genuine MCP failure still raises**: `tools=True` stream with `tool_use` but
   no `tool_result` → `RuntimeError` naming probe + backend (Req 3.1).
2. **Tool-less unchanged**: `tools=False` stream (terminal result, no tool
   blocks) → parsed `.result`, real usage when present, no probe, native prompt
   delivery; `--strict-mcp-config` + `--tools ""`, no `--mcp-config` (Req 3.2).
3. **Failure-mode parity**: timeout → `TimeoutError`; non-zero exit →
   `RuntimeError`; `is_error` result event → `RuntimeError` (even `tools=False`);
   unrecoverable `.result` → `ValueError`; missing `usage` → tolerate-and-warn
   (Req 3.3).
4. **Public-flags / no-enterprise-managed**: command contains `--output-format stream-json
   --verbose` and never `--aws-profile` / `--claude-help`; usage dict copied
   whole with no enterprise-managed branch (Req 3.4).
5. **Codex untouched**: `test_codex.py` stays green (PROBE_NAME retained in the
   shared instruction); `detect_tool_result(events=…)`/`raw` path unchanged.

### Unit Tests (`tests/test_claude_code.py` — Req 3.6)

New JSONL fixture helpers replace the single-object envelope helpers:
- `_stream(events)` → `"\n".join(json.dumps(e) for e in events)` on stdout.
- `_result_event(result_text, *, is_error=False, usage=None, total_cost_usd=None)`
  → `{"type":"result","subtype":"success","is_error":…,"result":…[,"usage":…][,"total_cost_usd":…]}`.
- `_system_event()`, `_assistant_text_event(text)`,
  `_assistant_tool_use_event(name="memory_search")`,
  `_user_tool_result_event(is_error=False)` → events with
  `message.content` blocks of the documented types.
- `_stream_ok(result_text)` = system + assistant(text) + result (no tool result).
- `_stream_with_tool_result(result_text)` = system + assistant(tool_use) +
  user(tool_result) + result (probe confirms).
- `_stream_with_usage(result_text, *, usage, total_cost_usd=None)` = the result
  event carrying `usage`.

Existing tests updated:
- `TestCommandConstruction::test_core_flags` → assert
  `--output-format` value is `"stream-json"` **and** `--verbose` present.
- All `_ok(_envelope(...))` / `_envelope_with_tool_result` / `_envelope_with_usage`
  call sites → the corresponding `_stream_*` fixtures.
- `TestResultExtraction` → `raw` equals the terminal result event's `.result`.
- `TestFailLoud` / `TestMcpAttach` → the `tool_result` now lives in a
  user/assistant event in the stream, not an envelope `transcript` key.
- `TestUsageCapture` → `usage` read from the terminal result event.
- `is_error` tests → result event carries `is_error` + a parseable `.result`.
- `test_unparseable_result_raises_valueerror` → result event `.result` is prose →
  `ValueError`; add `test_no_result_event_raises_valueerror`.
- Instruction assertions (`PROBE_NAME in prompt_input`) stay as-is (token
  retained).

`tests/test_mcp_probe.py` and `tests/test_codex.py` need **no** changes given the
token-retention decision; optionally add a `test_mcp_probe.py` assertion that the
instruction omits the injection-flagged phrases ("before anything else",
"disregard", "return only json").

### Property-Based Tests

- Generate random `stream-json` streams (interleaved
  `thinking`/`text`/`tool_use` blocks ± a `tool_result`, ± terminal `usage`) and
  assert: a confirmed `tool_result` ⇒ success on `tools=True`; no `tool_result`
  ⇒ `RuntimeError`; `.result` always taken from the terminal result event.
- Generate streams with/without `usage` and assert the real-vs-estimate
  `usage_source` split and the whole-dict copy (enterprise-managed extra fields ride along).
- Generate `tools=False` streams and assert the probe never runs and extraction
  matches the `tools=True` non-buggy contract.

### Integration Tests

- Full Explorer/Thinker flow against a mocked `stream-json` subprocess: probe
  instruction prepended, tool call + result confirmed, parsed `output` returned.
- Context parity: a `tools=True` success followed by a `tools=True` genuine
  failure in the same cached invoker (probe holds no cross-call state).
- Tool-less evaluator flow: native `--system-prompt-file`, no probe, real usage —
  end-to-end unchanged under the new format.
