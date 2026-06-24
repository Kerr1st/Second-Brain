# Implementation Plan

## Overview

Fix the false-positive `MCP_Startup_Probe` `RuntimeError` on every agentic
(`tools=True`) Claude Code turn. The root cause is live-confirmed (Finding 1):
`ClaudeCodeInvoker` builds `claude -p … --output-format json`, whose single
`{"type":"result", …}` envelope carries no `tool_use`/`tool_result` transcript,
so the probe never sees the tool result that actually returned and fails loud.
The fix switches the adapter to `--output-format stream-json --verbose` (a JSONL
event stream that exposes the transcript), reworks envelope/`.result`/`usage`
extraction to read the terminal `{"type":"result"}` event, and feeds the stream
events to `detect_tool_result`. A second, compounding defect (Finding 2) — the
injection-styled probe instruction the safety-tuned model refuses — is fixed by
rewording `MCPStartupProbe.instruction()` to a transparent, natural-language
request that retains the `PROBE_NAME`/`TOOL`/`QUERY` tokens.

The work follows the exploratory bugfix workflow: a bug-condition exploration
test that FAILS on the unfixed code is written first (proving the bug exists),
then preservation property tests that PASS on the unfixed code (capturing
baseline behavior), then the fix, then fix/preservation re-checks, and finally a
full-suite + docs gate.

## Task Dependency Graph

```
1 (bug-condition exploration test — FAILS on unfixed code)
2 (preservation property tests — PASS on unfixed code)
        │
        └─> 3 (apply the fix)
            ├─> 3.1 command-format switch (stream-json --verbose)
            ├─> 3.2 _events_from + _envelope_from terminal-result extraction
            ├─> 3.3 _extract_raw + _extract_usage from terminal result event
            ├─> 3.4 _run_probe feeds stream events to detect_tool_result
            ├─> 3.5 MCPStartupProbe.instruction() rewording (Finding 2)
            ├─> 3.6 rework test_claude_code.py fixtures to stream-json JSONL
            ├─> 3.7 [fix-check] verify exploration test now PASSES
            └─> 3.8 [preservation-check] verify preservation tests still PASS
                └─> 4 (checkpoint: full suite green + docs)
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"], "rationale": "Write exploration test (fails on unfixed) and preservation tests (pass on unfixed) BEFORE any code change; both observe the current code." },
    { "wave": 2, "tasks": ["3.1", "3.5"], "rationale": "Independent source edits: the command-format switch in claude_code.py and the instruction rewording in mcp_probe.py touch different files." },
    { "wave": 3, "tasks": ["3.2"], "rationale": "Add _events_from + terminal-result _envelope_from; depends on the stream-json format being emitted (3.1)." },
    { "wave": 4, "tasks": ["3.3", "3.4"], "rationale": "_extract_raw/_extract_usage and _run_probe both consume the terminal result event / event list from 3.2." },
    { "wave": 5, "tasks": ["3.6"], "rationale": "Rework test fixtures to stream-json JSONL once the adapter reads the new format." },
    { "wave": 6, "tasks": ["3.7", "3.8"], "rationale": "Fix-check (exploration test passes) and preservation-check (preservation tests still pass) after the fix + fixtures land." },
    { "wave": 7, "tasks": ["4"], "rationale": "Full-suite green gate (814 baseline) + docs marked fixed/verified." }
  ]
}
```

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - stream-json agentic turn with a real tool result
  - **CRITICAL**: This test MUST FAIL on the unfixed code — failure confirms the bug exists. **DO NOT attempt to fix the test or the code when it fails.**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation (re-run in task 3.7).
  - **GOAL**: Surface the live-confirmed counterexample — an agentic (`tools=True`) turn where the MCP tools attached and a `tool_result` genuinely returned, yet the adapter raises a false-positive `RuntimeError` from `MCP_Startup_Probe`.
  - **Scoped PBT Approach**: This bug is deterministic, so scope the property to the concrete failing shape: build a `stream-json` JSONL fixture (`system` → assistant `tool_use` → user `tool_result` → terminal `{"type":"result"}` event) and assert `ClaudeCodeInvoker(model="m").invoke("sys", "msg", tools=True)` returns a successful result `{output, raw, usage, usage_source}` with no raise. Generalize over the `.result` payload / interleaved `thinking`/`text` blocks so it reads as a property over "any stream that contains a confirmed tool_result".
  - Add a `_stream(events)` JSONL helper and a `_stream_with_tool_result(result_text)` fixture (system + assistant(tool_use) + user(tool_result) + terminal result) per the design's Unit Tests section; mock `src.backends.claude_code.subprocess.run` to return that stdout.
  - Bug Condition (from design): `isBugCondition(X) = X.tools == True AND mcp_tools_attached(X) AND tool_result_returned(X)`.
  - Expected Behavior (the assertion): the fixed adapter parses the `stream-json --verbose` stream, confirms the real `tool_result` via `detect_tool_result`, and returns success — no false-positive `RuntimeError`.
  - Run test on UNFIXED code. **EXPECTED OUTCOME**: Test FAILS — the unfixed adapter builds `--output-format json` and scans a single-object envelope, so the probe finds no transcript in the JSONL stream and raises `RuntimeError` from `MCP_Startup_Probe`.
  - Document the counterexample found (e.g. "tools=True + real tool_result in a stream-json fixture → `RuntimeError: MCP_Startup_Probe failed …` instead of a parsed `output`").
  - Mark this task complete when the test is written, run, and the failure is documented.
  - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - non-buggy inputs behave identically
  - **IMPORTANT**: Follow the observation-first methodology — run the UNFIXED adapter for each non-bug-condition case, record the actual behavior, then write property-based tests that assert it across the input domain.
  - **Methodology**: Generate varied inputs where `isBugCondition(X)` is false and assert the success/raise decision and extracted `.result`/`usage` match the documented contract. Property-based testing fits because preservation is a universal property ("for all non-buggy inputs, `F(X) == F'(X)`") and exercises far more interleavings than hand-written fixtures.
  - Cover (from design Preservation Requirements / Test Cases):
    - **Genuine MCP failure still raises** (Req 3.1): `tools=True` with a `tool_use` but NO `tool_result` → `RuntimeError` naming the probe + backend. Observe on unfixed code (it already raises here, for the wrong reason — it raises for ALL tools=True; capture that it raises for this case).
    - **Tool-less unchanged** (Req 3.2): `tools=False` → parsed `.result`, real usage when present, no probe, native `--system-prompt-file` delivery, `--strict-mcp-config` + `--tools ""`, no `--mcp-config`.
    - **Failure-mode parity** (Req 3.3): timeout → `TimeoutError`; non-zero exit → `RuntimeError`; envelope `is_error` → `RuntimeError` (even `tools=False`); unrecoverable `.result` → `ValueError`; missing `usage` → tolerate-and-warn (`usage=None`/`usage_source="estimate"`, no raise).
    - **Public-flags / no-enterprise-managed** (Req 3.4): command never contains `--aws-profile`/`--claude-help`; usage dict copied whole, no enterprise-managed-only branch.
    - **Codex + shared base untouched** (Req 3.5): `tests/test_codex.py` and `tests/test_mcp_probe.py` stay green (PROBE_NAME retained in the shared instruction).
  - Run tests on UNFIXED code. **EXPECTED OUTCOME**: These tests PASS on the unfixed adapter (using the appropriate current-format fixtures), confirming the baseline behavior to preserve. Note: the tool-less and failure-mode rows are observable today with the existing `_envelope`/`_ok` helpers; record their current outputs as the preserved contract.
  - Mark this task complete when the tests are written, run, and passing on the unfixed code.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Fix the false-positive probe failure on agentic Claude Code turns

  - [x] 3.1 Switch the command output format to stream-json --verbose
    - In `src/backends/claude_code.py` `_build_command`, replace `"--output-format", "json",` with `"--output-format", "stream-json", "--verbose",`.
    - Apply to BOTH `tools=True` and `tools=False` so there is a single extraction path (per the design "Single code path" decision); the terminal `{"type":"result"}` event is present on every completed turn regardless of tool use.
    - `--verbose` is required for `stream-json` to emit the full event stream (live repro); both flags are public (build `2.1.178.386`), so no enterprise-managed-only path is introduced.
    - _Bug_Condition: isBugCondition(X) where X.tools and tool_result_returned — F built `--output-format json` whose envelope has no transcript_
    - _Expected_Behavior: adapter emits `--output-format stream-json --verbose` (design Fix Implementation step 1)_
    - _Requirements: 2.4, 3.4_

  - [x] 3.2 Add _events_from and rework _envelope_from to the terminal result event
    - Add `_events_from(stdout) -> list | None`: parse the JSONL stream into a list of event dicts (one per line), skipping non-JSON lines; return `None` when nothing parses (mirrors `CodexInvoker._events_from`).
    - Rework `_envelope_from(result)`: stop calling the `parse_json_output` backstop on full stdout; parse events via `_events_from` and return the **last** event whose `type == "result"`; return `None` when no result event exists. This is the one consistent way the adapter recovers `.result`/`is_error`/`usage`.
    - _Expected_Behavior: `_envelope_from` returns the terminal `{"type":"result"}` event (design Fix Implementation steps 2–3)_
    - _Requirements: 2.4_

  - [x] 3.3 Extract .result and usage from the terminal result event
    - `_extract_raw(result)`: from the terminal result event, return `.result` (a `str` as-is; otherwise `json.dumps`). When there is no result event (or it carries no `.result`), return `""` so the shared `parse_json_output("")` raises `ValueError` — NOT full JSONL stdout, which would let the backstop recover an interior event as a bogus answer.
    - `_extract_usage(...)`: keep the tolerate-and-warn semantics verbatim. Because `_envelope_from` now returns the terminal result event, `usage`/`total_cost_usd` are read from that event — real `usage` → copy the whole dict + fold in `total_cost_usd` → `usage_source="real"`; missing `usage` → `None`/`"estimate"` + loud warning, never raise. The whole-dict copy preserves the enterprise-managed divergence tolerance with no enterprise-managed-only branch.
    - Add `test_no_result_event_raises_valueerror` coverage (stream missing the terminal result event → `ValueError`).
    - _Expected_Behavior: `.result`/`usage` read from the terminal result event; no-result-event → `ValueError` (design Fix Implementation steps 4–5, "No result event" decision)_
    - _Preservation: tolerate-and-warn + failure-mode mapping unchanged (Req 3.3)_
    - _Requirements: 2.4, 3.3_

  - [x] 3.4 Feed stream events to the probe in _run_probe
    - Keep the `is_error` check unconditional: `_envelope_from(result)` → terminal result event; if `is_error is True` raise `RuntimeError` (fires even for `tools=False`, preserving `test_is_error_raises_even_when_tools_false`).
    - In the `MCPStartupProbe.run(...)` call, replace `parsed=envelope` with `events=self._events_from(result.stdout)` and keep `raw=result.stdout` as the fallback. The `tool_result` blocks live in the assistant/user message events, not the terminal result event, so passing the result event as `parsed` would never confirm; `detect_tool_result` recurses into the event list and the terminal `result` event is correctly ignored (its type is not a tool_result marker).
    - _Bug_Condition: isBugCondition(X) — the real tool_result lives in stream events, not the terminal result event_
    - _Expected_Behavior: probe confirms via `events=`/`raw=` from the stream (design Fix Implementation step 6)_
    - _Preservation: genuine-failure still raises; is_error still raises even tools=False (Req 3.1, 3.3)_
    - _Requirements: 2.4, 3.1, 3.3_

  - [x] 3.5 Reword MCPStartupProbe.instruction() to a transparent request (Finding 2)
    - In `src/backends/mcp_probe.py`, rewrite `instruction()` to a natural-language request that drops the injection-flagged framing ("Before anything else", "Disregard whatever it returns", "return only JSON").
    - Constraints (keep the broad suite green): MUST retain the `TOOL` (`memory_search`) and `QUERY` (`__mcp_startup_probe__`) substrings (keeps `test_mcp_probe.py::test_instruction_mentions_trivial_tool_call`); SHOULD retain the `PROBE_NAME` (`MCP_Startup_Probe`) substring as a parenthetical label (keeps `PROBE_NAME in prompt_input` assertions in `test_claude_code.py` AND `test_codex.py` green, so Codex needs no change).
    - Keep the wording generic (no Claude/Codex specifics) since both adapters prepend it. Use the design's illustrative phrasing as the basis.
    - _Bug_Condition (Finding 2): safety-tuned model refuses the terse instruction → no tool call → no tool_result_
    - _Expected_Behavior: transparent natural-language request the model honors, tokens retained (design Fix Implementation step 7, Req 2.3)_
    - _Preservation: Codex + shared base behavior unchanged except shared wording (Req 3.5)_
    - _Requirements: 2.3, 3.5_

  - [x] 3.6 Rework tests/test_claude_code.py fixtures to stream-json JSONL
    - Replace the single-object envelope helpers with the design's JSONL helpers: `_stream(events)`, `_result_event(result_text, *, is_error=False, usage=None, total_cost_usd=None)`, `_system_event()`, `_assistant_text_event(text)`, `_assistant_tool_use_event(name="memory_search")`, `_user_tool_result_event(is_error=False)`, and the composites `_stream_ok(result_text)`, `_stream_with_tool_result(result_text)`, `_stream_with_usage(result_text, *, usage, total_cost_usd=None)`.
    - Update existing tests: `TestCommandConstruction::test_core_flags` asserts `--output-format` value is `"stream-json"` AND `--verbose` is present; migrate every `_ok(_envelope(...))`/`_envelope_with_tool_result`/`_envelope_with_usage` call site to the corresponding `_stream_*` fixture; `TestResultExtraction` asserts `raw` equals the terminal result event's `.result`; `TestFailLoud`/`TestMcpAttach` move the `tool_result` into a user/assistant stream event (not an envelope `transcript` key); `TestUsageCapture` reads `usage` from the terminal result event; `is_error` tests carry `is_error` + a parseable `.result` on the result event; `test_unparseable_result_raises_valueerror` puts prose in the result event `.result`. Instruction assertions (`PROBE_NAME in prompt_input`) stay as-is.
    - `tests/test_mcp_probe.py` and `tests/test_codex.py` need NO changes (token-retention decision); optionally add a `test_mcp_probe.py` assertion that the instruction omits the injection-flagged phrases.
    - _Preservation: mocked-subprocess tests updated to stream-json JSONL, suite stays green (Req 3.6)_
    - _Requirements: 3.6_

  - [x] 3.7 Verify the bug condition exploration test now passes
    - **Property 1: Expected Behavior** - stream-json probe confirms real tool results
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test. The test from task 1 encodes the expected behavior; when it passes, it confirms the false positive is gone.
    - Run the bug condition exploration test from task 1 on the FIXED code.
    - **EXPECTED OUTCOME**: Test PASSES — `invoke(..., tools=True)` with a confirmed `tool_result` in the stream now returns `{output, raw, usage, usage_source}` with no false-positive `RuntimeError`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.8 Verify preservation tests still pass
    - **Property 2: Preservation** - non-buggy inputs behave identically
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests.
    - Run the preservation property tests from task 2 on the FIXED code.
    - **EXPECTED OUTCOME**: Tests PASS — genuine MCP failure still raises, tool-less is unchanged, every failure mode maps identically, only public flags are used, and Codex/shared base are unchanged (no regressions).
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Checkpoint — full suite green + docs updated
  - Run the full suite (`.venv/bin/python -m pytest -q`) and confirm it stays green at the 814-passing baseline (only `tests/test_claude_code.py` fixtures changed; `test_codex.py` and `test_mcp_probe.py` stay green). If questions arise, ask the user.
  - Update `docs/MODEL-BACKENDS.md` and `docs/MODEL-BACKENDS-VERIFICATION.md` to mark the agentic (`tools=True`) Claude Code path fixed/verified: note the switch to `--output-format stream-json --verbose`, the terminal-result-event extraction, the event-stream probe confirmation, and the Finding-2 instruction rewording. No embedding-path changes.
  - _Requirements: 2.1, 2.2, 3.6_

## Notes

- Task ordering is mandated by the exploratory bugfix workflow: the exploration
  test (task 1) MUST fail on unfixed code and the preservation tests (task 2)
  MUST pass on unfixed code, both BEFORE the fix lands.
- The fix is contained to two files — `src/backends/claude_code.py` (format +
  extraction + probe wiring) and `src/backends/mcp_probe.py` (instruction
  wording) — plus the `tests/test_claude_code.py` fixture rework.
- Token retention (`PROBE_NAME`/`TOOL`/`QUERY`) in the reworded instruction is
  what keeps `test_codex.py` and `test_mcp_probe.py` green with no code change.
