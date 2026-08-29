---
status: accepted
---

# Store captured tasks and topic segments in memories

Codex capture extends the existing `memories` source hierarchy: a Captured Task is an unembedded root source memory, and each Topic Segment is an embedded child source memory. Distilled decisions and insights remain root semantic memories, while Correction Episodes are root episodic memories; all connect to their source segment with `derived_from`. Exact Provenance is the traceability chain from that memory through its supporting Agent Turns and Topic Segment to the original Agent Task; model versions, hashes, timing, and usage telemetry are not required parts of the chain. This preserves the retrieval and dream-cycle assumptions that child memories are raw source material while avoiding parallel task and segment tables that every downstream system would otherwise need to join.

Codex is the first adopter of the cross-integration agent-task capture standard recorded in ADR 0002.

## Considered Options

- Add dedicated Codex task and topic-segment tables.
- Store topic boundaries only inside the Captured Task's metadata.
- Reuse the existing source-parent and embedded-child memory model.

## Consequences

Codex task identity needs a capture-specific uniqueness constraint, and mutable-task refresh behavior must reconcile child segments and derived memories explicitly. Topic Segments replace arbitrary fixed-size chunks for Codex Tasks rather than duplicating them. Correction Episodes remain searchable evidence and do not acquire steering authority merely by sharing the memory hierarchy.
