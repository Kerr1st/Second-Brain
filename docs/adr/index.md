# Architecture Decision Records

> **Status: canonical decision index.** Last reviewed: 2026-08-29.

Architecture Decision Records (ADRs) preserve durable choices and their
consequences. Component pages define current ownership and link to the ADRs
that govern their behavior.

| ADR | Decision | Components |
|---|---|---|
| [0001](0001-store-captured-tasks-in-memories.md) | Store Captured Tasks and Topic Segments in `memories` | Capture; Ingestion & Storage |
| [0002](0002-standardize-agent-task-capture.md) | Standardize agentic-assistant capture around Agent Tasks and Agent Turns | Capture |
| [0003](0003-accumulate-captured-task-history-monotonically.md) | Accumulate Captured Task history monotonically | Capture; Ingestion & Storage |
| [0004](0004-separate-source-provenance-from-semantic-project.md) | Keep source provenance separate from semantic project assignment | Capture; Ingestion & Storage; Retrieval |
| [0005](0005-use-real-agent-task-data-throughout-testing.md) | Use real Agent Task data throughout capture testing | Capture |
| [0006](0006-combine-task-segmentation-and-distillation.md) | Combine Topic Segmentation and Task Distillation into one semantic pass | Capture; Model Execution |
| [0007](0007-capture-correction-episodes-before-steering.md) | Capture Correction Episodes before proposing Steering Rules | Capture; Synthesis; Delivery |
| [0008](0008-classify-task-ownership-before-capture.md) | Classify Agent Task ownership before capture | Capture |
| [0009](0009-use-codex-jsonl-for-model-execution.md) | Use Codex JSONL as the default model-execution output | Model Execution |
| [0010](0010-prove-capabilities-vertically-before-generalizing.md) | Prove capabilities vertically before generalizing across integrations | All |
| [0011](0011-use-bounded-context-packs-and-outcome-receipts.md) | Use bounded context packs and outcome receipts for agent recall | Retrieval; MCP Interface; Steering Governance |

## Related

- [Architecture Component Index](../components/index.md)
- [System architecture](../ARCHITECTURE.md)
- [Design decisions](../DESIGN-DECISIONS.md)
