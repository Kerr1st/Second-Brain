# Implementation Plan

## Overview

Add two agentic-CLI adapters — `ClaudeCodeInvoker` (`claude -p`) and
`CodexInvoker` (`codex exec`) — on top of the existing backend scaffold, and
register them in the resolver. The work is sequenced so the green baseline is
captured first, a shared `AgenticCliInvoker` base is extracted from
`KiroInvoker` behavior-identically (proving the default-preserving guarantee),
then each adapter is built and tested with mocked subprocess, then verified
(Claude Code live on the local enterprise-managed build; Codex via a manual checklist).

Per `docs/MODEL-BACKENDS.md`: build the Claude Code adapter live here, the Codex
adapter against the documented surface with mocks (Codex is not installed).

## Task Dependency Graph

```
1 (baseline)
└─> 2 (extract AgenticCliInvoker base; Kiro behavior-identical)
    └─> 3 (shared MCP_Startup_Probe helper)
        ├─> 4 (ClaudeCodeInvoker) ─> 4.1 ─> 4.2 ─> 4.3 ─> 4.4 ─┐
        └─> 5 (CodexInvoker)       ─> 5.1 ─> 5.2 ─> 5.3 ─> 5.4 ─┤
                                                                 └─> 6 (register in resolver)
                                                                     └─> 7 (live + manual verification)
                                                                         └─> 8 (full-suite verify + docs)
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "rationale": "Capture the green baseline before any change." },
    { "wave": 2, "tasks": ["2"], "rationale": "Extract the shared base from KiroInvoker; must stay behavior-identical for the Kiro path." },
    { "wave": 3, "tasks": ["3"], "rationale": "Build the shared MCP startup-probe helper both adapters depend on." },
    { "wave": 4, "tasks": ["4.1", "5.1"], "rationale": "Command construction + Invoker conformance for each adapter; independent." },
    { "wave": 5, "tasks": ["4.2", "5.2"], "rationale": "MCP attach/cwd/sandbox + fail-loud + tool-less per adapter." },
    { "wave": 6, "tasks": ["4.3", "5.3"], "rationale": "Usage capture (tolerate-and-warn) + failure-mode parity + parser reuse." },
    { "wave": 7, "tasks": ["4.4", "5.4"], "rationale": "Mocked-subprocess unit tests per adapter." },
    { "wave": 8, "tasks": ["6"], "rationale": "Register both adapters in DEFAULT_ADAPTERS + resolver/guard tests." },
    { "wave": 9, "tasks": ["7"], "rationale": "Live Claude Code verification + Codex manual smoke checklist." },
    { "wave": 10, "tasks": ["8"], "rationale": "Full-suite verification and doc update." }
  ]
}
```

## Tasks

- [x] 1. Capture the green test baseline
  - Run `.venv/bin/python -m pytest -q` and record the passing count on the `laptop` (all-Kiro) profile, so the default-preserving guarantee can be verified after the refactor.
  - _Requirements: 20.1, 20.2_

- [x] 2. Extract a shared `AgenticCliInvoker` base, behavior-identical for Kiro
  - Create `src/backends/agentic_cli.py` (or extend `base.py`) with the shared subprocess mechanics currently inline in `KiroInvoker`: subprocess spawn (`capture_output`, `text`, `errors="replace"`), `subprocess.TimeoutExpired → TimeoutError`, non-zero exit → `RuntimeError`, the per-call metrics JSONL writer (extended to carry `usage_source` and real token counts), raw-output debug dump, and temp-config cleanup in `finally`. Expose hook methods adapters override: command construction, system-prompt delivery, MCP-config emission, final-text extraction, effort flag, usage extraction, failure-mode/envelope mapping.
  - Refactor `KiroInvoker` to use the base while keeping its exact command surface (`--no-interactive --agent --model`, `--trust-all-tools --require-mcp-startup`, the MCP-startup retry, `usage=None`/`usage_source="estimate"`).
  - Re-run the full suite on the `laptop` profile; it MUST pass unchanged.
  - _Requirements: 20.1, 20.2, 20.3_

- [x] 3. Build the shared MCP_Startup_Probe helper
  - Add a probe helper that issues one trivial, read-only tool call (e.g. `memory_search` with a fixed query, `limit=1`) and confirms success only when a tool *result* is returned through the CLI envelope/event stream (process-attach alone is insufficient). On no confirmed result, raise `RuntimeError` naming the probe and backend.
  - Ensure the probe runs only when `tools=True` and performs no caching across invocations (each `invoke()` is a fresh subprocess).
  - _Requirements: 5.1, 5.4, 5.6, 14.1, 14.3, 14.5_

- [x] 4. Implement ClaudeCodeInvoker
- [x] 4.1 Command construction + Invoker conformance
  - Create `src/backends/claude_code.py` with `ClaudeCodeInvoker(model=<id>)` implementing the `Invoker` signature. Invoke a configurable binary (`CLAUDE_CLI`, default `claude`) using only public-reference flags: `claude -p <user_message> --model <id> --output-format json`, `[--effort <level>]`, `[--json-schema <file>]`; deliver `system_prompt` via `--system-prompt-file` (preferred) or `--append-system-prompt`, with the prepend-to-user-message fallback. Extract final text from envelope `.result`. Do not depend on the enterprise-managed provider wrapper surface (`--aws-profile`, Bedrock routing, `--claude-help`); leave auth/provider routing to out-of-band env.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.1, 3.2, 3.3, 21.1_
- [x] 4.2 MCP attach, fail-loud, and tool-less enforcement
  - tools=True: emit `--mcp-config <sb.json>` `--strict-mcp-config` with the server entry `python -m src.mcp_server` and `cwd` = repo root; run the MCP_Startup_Probe; raise `RuntimeError` on probe failure or envelope `is_error`. tools=False: pass `--strict-mcp-config` WITHOUT `--mcp-config` (no MCP servers load) plus `--tools ""` (built-in tools off) — `--tools ""` alone does not disable MCP tools — and skip the probe.
  - _Requirements: 4.1, 4.2, 4.3, 5.2, 5.3, 5.5, 6.1, 6.2, 6.3_
- [x] 4.3 Usage capture (tolerate-and-warn), failure-mode parity, parser reuse
  - On success with envelope `usage`: populate `usage` from `usage`/`total_cost_usd`, set `usage_source="real"`. On success with parseable payload but no `usage`: keep `output`/`raw`, set `usage=None`, `usage_source="estimate"`, log a loud warning, do NOT raise. Map timeout→`TimeoutError`, non-zero exit→`RuntimeError`, `is_error`→`RuntimeError`, unrecoverable JSON→`ValueError`. Parse via shared `parse_json_output`, preferring schema-validated output when requested.
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 9.3_
- [x] 4.4 Mocked-subprocess unit tests for ClaudeCodeInvoker
  - Mock `subprocess.run`. Cover: command construction (public flags), tools=True vs tools=False shapes, native + fallback system-prompt delivery, MCP-config emission with `cwd=repo` + `--strict-mcp-config`, `.result` extraction, usage capture including the tolerate-and-warn fallback, every failure-mode row, and probe behavior (performed on tools=True, skipped on tools=False, not cached).
  - _Requirements: 22.1, 22.6_

- [x] 5. Implement CodexInvoker
- [x] 5.1 Command construction + Invoker conformance
  - Create `src/backends/codex.py` with `CodexInvoker(model=<id>)` implementing the `Invoker` signature against the documented `codex exec` surface: `codex exec <user_message> -m <id>`, `[-c model_reasoning_effort=<level>]`, final text via `--output-last-message <file>` or `--json` events, `[--output-schema <file>]`; deliver `system_prompt` via `developer_instructions`/`instructions`/`model_instructions_file`, with the prepend fallback.
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 11.1, 11.2, 11.3, 11.4, 12.1, 12.2, 12.3, 21.2_
- [x] 5.2 MCP attach/sandbox, fail-loud, and tool-less enforcement
  - tools=True: config `[mcp_servers.second_brain] command=<python> args=["-m","src.mcp_server"] cwd=<repo> required=true`, plus `--sandbox workspace-write` and `sandbox_workspace_write.network_access=true`; treat `required=true` as the primary startup guard and the MCP_Startup_Probe as the secondary sandbox-reachability check. tools=False: no `mcp_servers`, `--sandbox read-only`, skip the probe.
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 14.2, 14.4, 14.5, 15.1, 15.2, 15.3_
- [x] 5.3 Usage capture (tolerate-and-warn), failure-mode parity, parser reuse
  - On success with `--json` usage events: populate `usage`, set `usage_source="real"`. On success without usage events: keep `output`/`raw`, set `usage=None`, `usage_source="estimate"`, warn, do NOT raise. Map timeout→`TimeoutError`, non-zero exit→`RuntimeError`, unrecoverable JSON→`ValueError`. Parse via shared `parse_json_output`, preferring schema-validated output.
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 17.1, 17.2, 17.3, 18.1, 18.2, 18.3_
- [x] 5.4 Mocked-subprocess unit tests for CodexInvoker
  - Mock `subprocess.run`. Cover: `codex exec` command construction, tools=True vs tools=False shapes, sandbox flags (`workspace-write`+`network_access` vs `read-only`), `mcp_servers` config with `cwd` + `required=true`, native + fallback system-prompt delivery, `--output-last-message`/`--json` extraction, usage capture including tolerate-and-warn, every failure-mode row, and probe behavior (secondary check; skipped tool-less; not cached).
  - _Requirements: 22.4, 22.6_

- [x] 6. Register both adapters in the resolver
  - In `src/backends/resolver.py`, import `ClaudeCodeInvoker` and `CodexInvoker` and add them to `DEFAULT_ADAPTERS` (`"claude_code"`, `"codex"`). Add tests asserting a profile selecting either backend resolves to the correct adapter (not `NotImplementedError`), is cached per `(backend, model)`, and that Explorer on either passes `assert_backend_supports_role`.
  - _Requirements: 19.1, 19.2, 19.3, 19.4_

- [x] 7. Verify adapters per environment
  - Claude Code (live, local enterprise-managed build): confirm tool attachment via the probe, fail-loud on a forced MCP failure, JSON parsing of a real `.result`, and real usage capture; record any enterprise-managed auth/metering divergence as a noted caveat handled tolerantly (no enterprise-managed-only code path).
  - Codex (manual): write a smoke-test checklist (`docs/` or the spec dir) covering `required=true` startup-fail, the sandbox `network_access` path reaching Postgres/Bedrock, `--output-last-message` extraction, and usage events — to run on a Codex-equipped box.
  - _Requirements: 22.2, 22.3, 22.5_

- [x] 8. Final verification and documentation
  - Run the full suite (`.venv/bin/python -m pytest`) on the `laptop` profile (zero behavior change) and with a `claude_code` profile against the mocked adapters; confirm green.
  - Update `docs/MODEL-BACKENDS.md` status (adapters implemented) and note the Codex smoke-checklist location. No embedding-path changes.
  - _Requirements: 20.1, 20.2, 20.3, 23.1, 23.2_

## Notes

- Behavior preservation (Tasks 1, 2, 8) brackets the KiroInvoker refactor to
  prove the all-Kiro path is unchanged.
- Adapter unit tests mock `subprocess` so no live CLI is required; DB-touching
  probe tests may use the `test_db`/`clean_tables`/`mock_embedding` fixtures.
- Out of scope (deferred): direct-API adapters (Bedrock/OpenAI/Anthropic),
  per-role model selection, evaluator-panel diversity, and the Explorer
  slice-quality validation (capability-eligibility only here — Req 23).
