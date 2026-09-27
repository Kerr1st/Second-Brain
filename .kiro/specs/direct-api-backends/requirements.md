# Provider request and accounting: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Resolve corpus workloads through explicit roles; reject tools=True before a direct API request.
2. Use Bedrock Converse with validated vendor-specific options; unsupported combinations fail before invocation.
3. Preserve required token counts as unknown when absent; reject negative, nonfinite, boolean or malformed usage.
4. Charge response-bearing parse, truncation and refusal failures exactly once before permitting another attempt.
5. Validate finite positive reasoning thresholds; block subsequent calls after exhaustion or unknown spend. A completed call can overshoot.
6. Record requested model, backend, usage source and known cost; embedding charges remain outside the reasoning threshold.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
