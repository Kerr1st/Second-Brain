---
status: accepted
---

# Accumulate Captured Task history monotonically

Once Second Brain captures an Agent Turn, that evidence remains immutable. A later source observation appends only unseen complete turns; changes, disappearance, or reordering of previously captured turns are Source Drift and are ignored without additional state. Topic processing receives the previous tail segment as context with the new turns, so a prior `A B C` capture observed later as `A C changed-B D E` remains `A B C D E` and can continue the segment containing `C` with `D E`.

## Considered Options

- Reevaluate and rewrite the complete Captured Task whenever earlier source history changes.
- Stop processing and require an explicit full-reconciliation workflow.
- Treat the Captured Task as a monotonic evidence log and accept corrections only through new turns.

## Consequences

Source anomalies cannot silently rewrite or delete established knowledge, and there is no full-reconciliation workflow to operate. A genuine correction must appear in a new Agent Turn, where the Task Semantic Pass can create a new memory supported by that evidence.

This decision governs later observations of a task that the connector can still read. A new correction may extend the immediately preceding Topic Segment so the prior visible outcome and correcting prompt remain together; nonadjacent historical correction lookup is deferred. Explicit lifecycle tracking for an entirely unavailable source is also deferred: captured evidence remains intact, and normal recurring inventory can rediscover the source if it returns.
