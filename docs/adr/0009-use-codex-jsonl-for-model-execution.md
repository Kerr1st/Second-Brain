---
status: accepted
---

# Use Codex JSONL as the default model-execution output

When Codex runs as a Model Execution backend, the adapter uses the CLI JSONL event stream by
default. The stream exposes the visible final agent message and real token usage through one
invocation, while `--output-last-message` remains an explicit compatibility fallback. Missing usage
never turns a successful semantic result into a failure.

This is an independent Model Execution decision, not part of Codex Task capture provenance. The
capture plan's telemetry deferral means capture does not persist model, prompt, timing, usage, or
hash fields as Exact Provenance. It does not prohibit the shared Invoker from reporting operational
usage diagnostics to its callers.
