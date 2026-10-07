# Provider request and accounting: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06; carried by R1 (deployed). These requirements describe behavior, not deployed status. The original 2026-06 design of the Bedrock adapter (`DirectApiInvoker` as the tool-less sibling of `AgenticCliInvoker`, Converse as the default path, failure-mode parity with the CLI adapters, the considered-and-rejected AgentCore and Strands alternatives) is preserved verbatim in [the installed-snapshot archive](../../../docs/archive/2026-09-10-installed/.kiro/specs/direct-api-backends/requirements.md); the adapter it describes was implemented before that spec was checked off and is the code this family now describes.

1. Resolve corpus workloads through explicit roles; reject `tools=True` before a direct API request; the Explorer can never resolve to a direct-API backend.
2. Use Bedrock Converse with validated vendor-specific options (`temperature`, `service_tier`, `max_tokens`; adaptive thinking and effort fields per model family); unsupported combinations fail at config load, not at invocation.
3. Preserve required token counts as unknown when absent; reject negative, non-finite, boolean or malformed usage; reconcile implausible input counts (`totalTokens − outputTokens`, one token per 12 characters floor) before pricing and flag the estimate.
4. Charge response-bearing parse, truncation, refusal and model-not-honoured failures exactly once from real usage before permitting another attempt.
5. Charge a call that fails before any response exists (throttle, timeout, 5xx, DNS, dead CLI) a conservative reservation — input chars / 3 plus the output budget at list price, or the stage estimate if larger — so the run stops on the threshold instead of looping or blocking.
6. Validate finite positive thresholds; block subsequent calls after exhaustion, after a routing-guarantee failure or after a successful response without complete usage. A completed call can overshoot the threshold.
7. Record requested model, backend, usage source, served tier, reconciliation fields and known cost per call in the metrics JSONL; embedding charges remain outside the reasoning threshold.
8. Bound each seat's output (`max_tokens`) so the worst-case reservation per failed call stays at the per-seat floor.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
