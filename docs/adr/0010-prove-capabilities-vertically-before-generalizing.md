---
status: accepted
---

# Prove capabilities vertically before generalizing across integrations

Second Brain develops cross-cutting functionality through one Reference Integration from source
evidence to observable user outcome before extending it to other agentic assistants. Codex is the
current Reference Integration.

A qualifying Vertical Slice includes every lifecycle stage needed to evaluate the capability's
value: capture or source evidence, durable processing, retrieval or governance, delivery, and a
recorded outcome. After the proof, the implementation and result are reviewed before a second
integration is added. A shared integration seam is extracted only when that second real adapter
demonstrates what actually varies.

## Considered options

- Build shared abstractions and all connectors before exercising the complete lifecycle.
- Implement one lifecycle stage across every integration at a time.
- Prove one complete integration path, evaluate it, and only then generalize.

## Consequences

The project may temporarily contain a source-specific implementation where only one real adapter
exists. That duplication risk is accepted in exchange for learning from an observable end-to-end
result. Horizontal rollout is not evidence that the underlying capability works; the Vertical
Slice proof and its recorded outcome are the gate.

This policy does not waive shared contracts such as Task Ownership, Exact Provenance, monotonic
capture, or human approval. It determines implementation order, not safety or quality standards.
