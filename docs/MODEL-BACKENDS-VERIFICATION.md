# Model-backend adapters — per-environment verification

> Companion to `docs/MODEL-BACKENDS.md`. Records the **per-environment
> verification** for the two agentic-CLI adapters (`ClaudeCodeInvoker`,
> `CodexInvoker`) — spec `model-backend-adapters`, Task 7, Requirements 22.2,
> 22.3, 22.5.
>
> Two parts, matched to what each machine has (Req 22):
> - **Part A — Claude Code: LIVE** on the local enterprise-managed `claude` build.
> - **Part B — Codex: LIVE + MANUAL** smoke-test checklist on the local
>   authenticated Codex build.

---

## Part A — Claude Code live verification (local enterprise-managed build)

**Environment.** Verified on this laptop, `2026-06-16`.

- Binary: `claude` at `~/.toolbox/bin/claude` — the **enterprise-managed "provider CLI wrapper
  for Claude Code"**, version **`2.1.178.386`** (channel stable). (The spec text
  referenced `2.1.177.377`; the live build is slightly newer — flag surface
  below was re-confirmed against it.)
- `CLAUDE_CLI` env var: unset → adapter uses the default `claude` on PATH.
- Auth/provider routing: supplied out-of-band by the provider wrapper (routes to
  **Bedrock** — see metering note below). No adapter code involved.

### What was verified LIVE

| Check (Req 22.2) | Result | Evidence |
|---|---|---|
| Public flag surface exists on this build | ✅ PASS | `claude --claude-help` lists `-p`, `--model`, `--output-format json`, `--system-prompt-file`/`--append-system-prompt`, `--effort {low,medium,high,xhigh,max}`, `--json-schema`, `--mcp-config`, `--strict-mcp-config`, `--tools "" ` — every flag the adapter emits. |
| Native system-prompt delivery (`--system-prompt-file`) | ✅ PASS | The tool-less run below used the default `system_prompt_delivery="file"` (`--system-prompt-file <tmp>`) and succeeded. (Req 3.1) |
| Tool-less invoke (`tools=False`) parses a real `.result` | ✅ PASS | `claude -p … --output-format json --strict-mcp-config --tools ""` returned envelope `.result = '{"ok": true, "answer": 42}'`; `parse_json_output` recovered `{"ok": true, "answer": 42}`. |
| Real usage capture (`usage_source="real"`) | ✅ PASS | Envelope carried `usage` + `total_cost_usd`; adapter returned `usage_source="real"`, `total_cost_usd ≈ $0.011`, `input_tokens`/`output_tokens` populated. |
| Forced-MCP-failure → fail-loud `RuntimeError` | ✅ PASS | With the MCP server entry pointed at a command that exits immediately, a `tools=True` call returned valid JSON but **no** tool result, and the `MCP_Startup_Probe` raised `RuntimeError` naming the probe + backend (never a silent tool-less "success"). |
| Positive tool-attachment confirmed *through the adapter's probe* | ✅ PASS (fixed — see Finding 1) | After the `stream-json` fix, a `tools=True` call with the real Second Brain MCP server confirms the real `tool_result` from the stream events and returns success — no false-positive `RuntimeError`. |

The reproduction scripts were run ad-hoc and removed after the run (they made
small, real, metered `claude -p` calls; total spend ≈ a few cents). The exact
commands are reproduced in the findings below so they can be re-run.

### Finding 1 (FIXED — spec `claude-code-stream-json-probe-fix`)

**`--output-format json` did not expose the tool-use/tool-result transcript the
MCP_Startup_Probe relies on, so every `tools=True` Claude Code call failed the
probe even when MCP tools attached and were used successfully. FIXED by
switching the adapter to `--output-format stream-json --verbose`.**

The adapter previously built `claude -p … --output-format json` and the probe
(`_run_probe` → `MCPStartupProbe.run`) scanned the parsed envelope + raw stdout
for a completed `tool_result` block. Live evidence showed the single-result
`json` envelope contained **no** transcript:

- `--output-format json` (the old mode) → one object:
  `{"type":"result","is_error":false,"result":"…","usage":{…},"num_turns":1,…}`.
  No `tool_use` / `tool_result` anywhere.
- `--output-format stream-json --verbose` (same prompt + same MCP config) →
  the MCP server **did** attach and the model **did** use the tool:
  top-level event types `{system, assistant, user, result}`, and content block
  types **`{thinking, tool_use, tool_result, text}`** (2 `tool_use`, 2
  `tool_result`).

So the tool transcript exists and is reachable — just only under
`stream-json`, not the `json` envelope the adapter and probe used.

**The fix (implemented).** `ClaudeCodeInvoker` now emits
`--output-format stream-json --verbose` for **both** `tools=True` and
`tools=False` (single extraction path), and:
- `_events_from(stdout)` parses the JSONL stream into a list of event dicts
  (skipping non-JSON lines);
- `_envelope_from` returns the **last** `{"type":"result"}` event and reads
  `.result` / `is_error` / `usage` from it (no `parse_json_output`-on-full-stdout
  backstop, which could recover a bogus interior event);
- `_extract_raw` returns the terminal result event's `.result` (and `""` when
  there is no result event, so `parse_json_output("")` raises `ValueError`);
- `_extract_usage` reads `usage` / `total_cost_usd` from that terminal event
  (whole-dict copy + tolerate-and-warn unchanged);
- `_run_probe` feeds `events=self._events_from(result.stdout)` (with
  `raw=result.stdout` fallback) to `MCPStartupProbe.run`, so `detect_tool_result`
  recurses into the assistant/user message events and confirms the real
  `tool_result` (the terminal `result` event is correctly ignored — its type is
  not a tool-result marker). The `is_error` check stays unconditional (fires
  even for `tools=False`).

**Verification.** The bug-condition exploration test
(`tests/test_claude_code_stream_json_probe.py`) now **passes** on the fixed code
(`invoke(..., tools=True)` with a confirmed `tool_result` returns
`{output, raw, usage, usage_source}` with no raise), the preservation property
tests (`tests/test_claude_code_stream_json_probe_preservation.py`) confirm no
regressions, and the `tests/test_claude_code.py` fixtures were reworked to
stream-json JSONL. Full suite green.

**Impact (resolved).** The Explorer/Thinker (the only `tools=True` stages) no
longer raise a false-positive `RuntimeError` on Claude Code. The tool-less
stages (evaluators/Express) were never affected.

Repro:
```bash
# Build the MCP config the adapter uses:
cat > /tmp/sb_mcp.json <<EOF
{"mcpServers":{"second-brain":{"command":"$(pwd)/.venv/bin/python","args":["-m","src.mcp_server"],"cwd":"$(pwd)"}}}
EOF
# json mode → single result envelope, NO transcript:
PYTHONPATH=. claude -p 'Search my memory for "test" (limit 1) with your memory tools, then say how many results.' \
  --model sonnet --output-format json --mcp-config /tmp/sb_mcp.json --strict-mcp-config
# stream-json mode → tool_use + tool_result blocks ARE present:
PYTHONPATH=. claude -p 'Search my memory for "test" (limit 1) with your memory tools, then say how many results.' \
  --model sonnet --output-format stream-json --verbose --mcp-config /tmp/sb_mcp.json --strict-mcp-config
```

### Finding 2 (FIXED — probe-instruction rewording, spec `claude-code-stream-json-probe-fix`)

**The enterprise-managed model refused the terse probe instruction as "prompt injection."**
The old `MCPStartupProbe.instruction()` text — *"Before anything else, call
the `memory_search` tool exactly once with query=… and limit=1 … Disregard
whatever it returns, then carry out the task normally."* — was refused by the
enterprise-managed `sonnet` model as a suspected prompt-injection/exfiltration pattern
(`num_turns:1`, no tool call, a refusal in `.result`). A natural-language
phrasing of the same request ("Please search my memory for … using your memory
tools, then tell me how many results") was honored and the tool was called.

**The fix (implemented).** `MCPStartupProbe.instruction()` was reworded to a
transparent, natural-language request that drops the injection-flagged framing
("Before anything else", "Disregard whatever it returns", "return only JSON").
The `TOOL` (`memory_search`), `QUERY` (`__mcp_startup_probe__`), and
`PROBE_NAME` (`MCP_Startup_Probe`, as a parenthetical label) substrings are
**retained**, so `tests/test_mcp_probe.py` and `tests/test_codex.py` stay green
with no code change (Codex prepends the same shared instruction). The wording is
generic — no Claude/Codex specifics — since both adapters use it.

### Finding 3 (enterprise-managed auth/metering divergence — handled tolerantly, Req 22.3)

The local build is the **enterprise-managed/Bedrock** distribution; its `usage` envelope
diverges from public Claude Code but is handled **without any enterprise-managed-only code
path** (consistent with Req 7's tolerate-and-warn, Req 22.3):

- Routing/metering: usage reported under `modelUsage` keyed by the **Bedrock**
  model id `global.anthropic.claude-sonnet-4-6[1m]` (the `sonnet` alias resolved
  to a 1M-context Sonnet 4.6). Real `total_cost_usd` is present — i.e. the enterprise-managed
  path **is** metered with real cost, not flat/$0.
- Extra `usage` fields beyond public Claude Code: `service_tier`,
  `inference_geo`, `iterations`, `speed`, `cache_creation`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, plus the top-level
  `modelUsage`, `permission_denials`, `terminal_reason`, `fast_mode_state`.
- Tolerance: `ClaudeCodeInvoker._extract_usage` copies the entire envelope
  `usage` dict and folds in `total_cost_usd`, so these extra fields ride along
  untouched and the standard `input_tokens`/`output_tokens`/`total_cost_usd`
  are captured. **No enterprise-managed-specific branching is required or present.** ✅

### Net status — Claude Code

- Tool-less path (evaluators, Express): **live-verified, fully working.**
- Real usage capture + `.result` parsing + native system-prompt file delivery:
  **live-verified.**
- Fail-loud on MCP failure: **live-verified.**
- Agentic tool path (Explorer/Thinker): **FIXED** (spec
  `claude-code-stream-json-probe-fix`) — the adapter now uses
  `--output-format stream-json --verbose`, extracts from the terminal
  `{"type":"result"}` event, and confirms the real `tool_result` via the
  stream events, so `tools=True` turns no longer raise a false-positive
  `RuntimeError`. The probe instruction was also reworded (Finding 2). Verified
  by the bug-condition + preservation tests; full suite green.

---

## Part B — Codex manual smoke-test checklist

> Codex is installed and authenticated on this machine. `CodexInvoker` is
> verified by mocked-subprocess unit tests (`tests/test_codex.py`, Req 22.4)
> plus this **manual** checklist (Req 22.5). A bounded tool-less JSONL smoke
> test was completed on 2026-07-23; the unchecked agentic and fallback rows
> remain to be exercised independently.

### Pre-flight

- [x] `codex --version` prints a version: `codex-cli 0.145.0-alpha.30`
      (2026-07-23).
- [ ] `codex exec --help` confirms the documented surface used by the adapter:
      positional prompt, `-m <model>`, `-c <key=value>`, `--output-schema`,
      `--output-last-message <file>`, `--json`, `--sandbox <mode>`,
      `-c sandbox_workspace_write.network_access=…`, and
      `-c mcp_servers.<name>.{command,args,cwd,required}`. Note any flag whose
      name/spelling drifts from the adapter (`src/backends/codex.py`).
- [x] Auth/provider routing works out-of-band — a bounded adapter call using
      configured model `gpt-5.6-sol` returned successfully (2026-07-23).
- [ ] Postgres and Bedrock are reachable from this box (the MCP server needs
      both): `.venv/bin/python -c "from src import db; db.get_connection()"`
      succeeds; AWS creds for embeddings are present.

### 1. `required=true` startup-fail (PRIMARY fail-loud guard — Req 14.4)

Goal: prove Codex hard-fails at startup when the MCP server can't start, and the
adapter maps that to `RuntimeError` (never a silent tool-less success).

- [ ] Force a broken server: temporarily point the MCP entry at a command that
      exits non-zero, e.g. set `_MCP_SERVER_ARGS = ["-c", "import sys; sys.exit(1)"]`
      in a scratch copy, **or** run `codex exec` directly with
      `-c mcp_servers.second_brain.command=<py>`
      `-c mcp_servers.second_brain.args='["-c","import sys; sys.exit(1)"]'`
      `-c mcp_servers.second_brain.required=true --sandbox workspace-write`.
- [ ] Expected: non-zero exit from `codex` → `CodexInvoker.invoke(... tools=True)`
      raises **`RuntimeError`** (base `_check_returncode`). Record the exit code
      and message: `__________`.
- [ ] Restore the real server entry.

### 2. Sandbox `network_access` path reaches Postgres/Ollama (Req 13.4, 15)

Goal: prove the agentic sandbox lets the MCP server reach PostgreSQL and the local Ollama runtime, and
that the read-only tool-less sandbox is used when `tools=False`.

- [ ] `tools=True`: confirm the adapter emits `--sandbox workspace-write` **and**
      `-c sandbox_workspace_write.network_access=true` (inspect
      `_tool_and_sandbox_args`, or `codex exec` argv). Run a real `tools=True`
      turn that asks the model to `memory_search` for a known term; confirm a
      **tool result with real rows** comes back (proves the sandboxed server
      reached PostgreSQL and local BGE-M3). Record row count:
      `______`.
- [ ] Negative control: rerun with `network_access` **off** (or `--sandbox
      read-only`) and confirm the server's database/Ollama call is blocked → the
      `MCP_Startup_Probe` raises `RuntimeError` (the secondary
      sandbox-reachability check, Req 14.2/14.3). Record behavior: `__________`.
- [ ] `tools=False`: confirm the adapter emits `--sandbox read-only` and **no**
      `mcp_servers` config, and that the probe is skipped (Req 15.1–15.3).

### 3. Structured JSONL extraction and file fallback (Req 11.3)

- [x] Default `final_message_source="json"`: run a turn that returns a small
      JSON object and confirm the adapter passes `--json`, recovers the final
      `agent_message` from the event stream, and `parse_json_output` recovers the
      object. Recorded 2026-07-23: `{"ok": true}`.
- [ ] Explicit compatibility fallback: construct
      `CodexInvoker(model=…, final_message_source="output-last-message")`,
      confirm the adapter passes `--output-last-message <tmp>`, and confirm
      `_extract_raw` reads the file contents. Record: `__________`.
- [ ] In fallback mode, confirm the temp file is removed after the call
      (`_cleanup_temp_files`).

### 4. Usage events → real usage capture (Req 16)

- [x] With the default `final_message_source="json"`, confirm the `--json` stream carries a
      usage event (`turn.completed` with `usage`, or `token_count` with
      `info.total_token_usage`) and the adapter returns **`usage_source="real"`**
      with `input_tokens`/`output_tokens` (and `total_cost_usd` if present)
      populated and folded into the metrics line. Recorded 2026-07-23:
      `input_tokens=18095`, `cached_input_tokens=0`, `output_tokens=9`,
      `reasoning_output_tokens=0`; this CLI event did not include cost.
- [ ] With the explicit `output-last-message` fallback (no `--json` usage events
      on stdout), confirm the adapter **tolerates-and-warns**: keeps
      `output`/`raw`, returns `usage=None` / `usage_source="estimate"`, logs the
      loud warning, and **does not raise** (Req 16.2, 17.1). Record:
      `__________`.

### 5. Failure-mode parity spot-checks (Req 17)

- [ ] Timeout: a turn exceeding `timeout` → **`TimeoutError`**.
- [ ] Non-zero exit (other than the `required=true` case above) → **`RuntimeError`**.
- [ ] Unrecoverable / non-JSON final text → **`ValueError`** (shared
      `parse_json_output` backstop). Confirm an infra failure never becomes a
      silent empty result or a fabricated verdict.

### Record any Codex divergences

Note here any flag, config-key, sandbox, or event-shape drift from
`src/backends/codex.py` so the adapter (or this checklist) can be reconciled:

```
(record findings here when run on a Codex box)
```

---

## Summary

| Adapter | Approach | Status |
|---|---|---|
| `ClaudeCodeInvoker` (tool-less) | LIVE (enterprise-managed build) | ✅ Verified: `.result` parse, real usage (`usage_source="real"`), `--system-prompt-file`, fail-loud on MCP failure. |
| `ClaudeCodeInvoker` (agentic, `tools=True`) | LIVE (enterprise-managed build) + tests | ✅ **FIXED** (spec `claude-code-stream-json-probe-fix`) — adapter uses `--output-format stream-json --verbose`, extracts `.result`/`is_error`/`usage` from the terminal `{"type":"result"}` event, and confirms the real `tool_result` via the stream events. **Finding 1** (transcript gap) and **Finding 2** (probe instruction refused as injection) both resolved. |
| enterprise-managed auth/metering divergence | LIVE | ✅ **Finding 3** — handled tolerantly, no enterprise-managed-only code path (Req 22.3). |
| `CodexInvoker` | MOCKED tests + bounded LIVE tool-less JSONL smoke | ✅ Default JSONL final-message extraction and real token usage verified on Codex CLI 0.145.0-alpha.30 (2026-07-23); agentic and explicit file-fallback checklist rows remain. |
