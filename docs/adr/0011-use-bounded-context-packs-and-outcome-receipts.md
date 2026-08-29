---
status: accepted
---

# Use bounded context packs and outcome receipts for agent recall

Second Brain supplies task-ready context through a Memory Context Broker instead of writing native
agent memory stores or returning an unbounded search result. Each pack orders approved applicable
Steering Rules before inferred knowledge and evidence, stays within an explicit token budget,
preserves stable IDs and provenance, reports selected conflicts, and creates a durable receipt.

The later task outcome records which returned IDs were actually used and classifies the result as
`followed`, `corrected`, `not_used`, or `unknown`. A corrected outcome must cite the resulting
Correction Episode. Returned guidance therefore cannot acquire new support merely because an agent
repeated it after injection.

## Considered options

- Depend on each harness's undocumented native memory selection and ranking.
- Return ordinary search results without authority, applicability, or a prompt budget.
- Inject large context automatically into every task without recording exposure or outcome.
- Use bounded context packs with explicit outcome receipts.

## Consequences

MCP exposes separate `memory_context` and `memory_context_outcome` tools. Automatic startup or
post-compaction injection remains optional until explicit recall demonstrates useful outcomes with
acceptable latency, token cost, and cross-project leakage. Receipt state is operational evidence,
not a new memory and not independent support for the memories it returned.
