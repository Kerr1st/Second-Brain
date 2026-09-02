# Memory Context Broker Module

> **Status: Codex-first vertical slice implemented and live-proofed.** Last reviewed: 2026-08-29.

The Memory Context Broker turns retrieval results and approved guidance into a small, task-ready
context pack and records what happens after that context is used.

## Interface

```python
build_context(ContextRequest) -> ContextPack
record_context_outcome(receipt_id, used_memory_ids, outcome, note=None,
                       correction_episode_id=None) -> None
```

`build_context` hides hybrid retrieval, hard applicability checks for approved Steering Rules,
authority ordering, token packing, conflict discovery, retrieval reinforcement, and receipt
persistence. Approved guidance precedes inferred knowledge and source evidence. The returned pack
contains stable memory IDs, source and task provenance when available, a retrieval reason, any
selected contradictions, an estimated token count, and a receipt ID.

`record_context_outcome` accepts `followed`, `corrected`, `not_used`, or `unknown`. Used IDs must be
a subset of the IDs actually returned. A `corrected` outcome must cite a Correction Episode, which
prevents injected guidance from circulating back as apparently independent support.

## Runtime flow

```text
task objective + scope
  → applicable active Steering Rules
  → project-scoped hybrid retrieval
  → authority ordering and token packing
  → context_receipts row with outcome=pending
  → connected Codex Task
  → used IDs and observed outcome
  → receipt closed as followed, corrected, not_used, or unknown
```

## Entry points and data

| Purpose | Entry point |
|---|---|
| Python interface | `src/context_broker.py` |
| Agent recall | `memory_context` in `src/mcp_server.py` |
| Outcome reporting | `memory_context_outcome` in `src/mcp_server.py` |
| Receipt storage | `context_receipts` from migration 013 |
| Behavior tests | `tests/test_context_broker.py` |

## Activation and proof

The module is available through MCP. Automatic session-start injection remains opt-in future work;
the first proof delivered one explicit pack to a new Codex Task. Receipt
`c15a25c2-6a0b-4304-89a7-63be33667939` returned four items, all four were reported used, and the
follow-up outcome was recorded as `followed` with no correction.

## Related

- [Architecture Component Index](index.md)
- [ADR 0011](../adr/0011-use-bounded-context-packs-and-outcome-receipts.md)
- [Codex memory integration research](../research/CODEX-LOCAL-MEMORIES-INTEGRATION-RESEARCH.md)
- [Steering Governance](steering-governance.md)
