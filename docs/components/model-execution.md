# Model Execution Component

> **Status: canonical component contract.** Last reviewed: 2026-07-23.

Model Execution gives semantic workflows one backend-independent invocation
contract while preserving backend, model, effort, tool, usage, and failure
provenance.

## Boundary

Model Execution owns:

- role-to-backend profile resolution;
- the shared `Invoker` contract;
- Kiro, Claude Code, and Codex CLI command construction;
- system-prompt delivery and structured-result recovery;
- MCP attachment and tool-less enforcement;
- timeout and process-failure mapping;
- token and cost telemetry when the backend reports it; and
- per-call backend provenance.

It does not read Codex Desktop task history. That responsibility belongs to the
Codex Desktop Source Connector in [Capture](capture.md). The
`CodexInvoker` name refers to using Codex as a model backend, not capturing
Codex Tasks.

## Contract

All implemented backends conform to:

```python
invoke(
    system_prompt,
    user_message,
    *,
    tools=False,
    effort=None,
    timeout=300,
    stage=None,
    run_id=None,
) -> {
    "output": object,
    "raw": str,
    "usage": dict | None,
    "usage_source": "real" | "estimate",
}
```

`tools=True` means the workflow requires the Second Brain MCP tool loop.
Direct or tool-less evaluation roles must not silently receive tools.

## Resolution flow

```text
semantic workflow requests a role
  → load config/backends.toml
  → select SECOND_BRAIN_PROFILE
  → validate every required role
  → resolve backend, model, and effort
  → reuse the matching Invoker
  → run one isolated backend call
  → parse structured output and usage
```

## Implemented backends

| Backend | Adapter | Output and usage |
|---|---|---|
| Kiro | `KiroInvoker` | CLI output; estimated usage |
| Claude Code | `ClaudeCodeInvoker` | Stream JSON result and real usage |
| Codex | `CodexInvoker` | JSONL final agent message and real usage by default |
| Direct Bedrock | Deferred | Design retained; no registered adapter |

The Codex `--output-last-message` mode remains an explicit compatibility
fallback. Default JSONL mode captures the final message and structured usage
events in one stream.

## Failure behavior

All adapters map:

- subprocess timeout to `TimeoutError`;
- nonzero backend exit to `RuntimeError`; and
- unrecoverable semantic JSON to `ValueError`.

Missing telemetry preserves a successful semantic result, records estimated or
absent usage, and warns. It does not become an infrastructure failure.

Tool-using adapters must fail loudly when the required MCP server cannot start
or when the expected tool result never returns. This prevents a disconnected
Explorer from appearing successful.

## Entry points and configuration

| Purpose | Entry point |
|---|---|
| Invoker protocol and capabilities | `src/backends/base.py` |
| Shared CLI mechanics | `src/backends/agentic_cli.py` |
| Profile resolver | `src/backends/resolver.py` |
| Kiro adapter | `src/backends/kiro.py` |
| Claude Code adapter | `src/backends/claude_code.py` |
| Codex adapter | `src/backends/codex.py` |
| Backend profiles | `config/backends.toml` |

## Tests and verification

- `tests/test_backends.py`
- `tests/test_resolver.py`
- `tests/test_agent_invoker.py`
- `tests/test_mcp_probe.py`
- `tests/test_claude_code.py`
- `tests/test_claude_code_stream_json_probe.py`
- `tests/test_codex.py`
- `docs/MODEL-BACKENDS-VERIFICATION.md`

## Related

- [Architecture Component Index](index.md)
- [Model backend architecture](../MODEL-BACKENDS.md)
- [Backend verification](../MODEL-BACKENDS-VERIFICATION.md)
- [ADR 0009: Codex JSONL model execution](../adr/0009-use-codex-jsonl-for-model-execution.md)
- [MCP Interface](mcp-interface.md)
- [Synthesis](synthesis.md)
- [Operations](../OPERATIONS.md#model-backend-profile)
