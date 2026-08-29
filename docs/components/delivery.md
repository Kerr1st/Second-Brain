# Delivery Component

> **Status: canonical component contract.** Last reviewed: 2026-07-23.

Delivery is the Express layer. It selects stored results worth surfacing,
composes a concise briefing, applies delivery preferences, and renders or
sends the approved output.

## Boundary

Delivery owns:

- briefing candidate selection;
- preference-based filtering and ranking;
- optional model editing;
- terminal and email rendering;
- gated proactive-push decisions; and
- delivery feedback such as useful, less, mute, and unmute.

It does not capture source material, rank arbitrary search queries, decide
Dream Cycle consensus, or mutate Correction Episodes into Steering Rules.

## Contract

Inputs include accepted insights, active contradictions, resurfacing
candidates, digest material, open questions, and `express_feedback`.

Outputs include:

- an on-demand Markdown briefing;
- a `memory_brief` MCP response;
- a rendered email; or
- a no-push result when the proactive gate is not satisfied.

## Runtime flow

```text
read eligible stored material
  → apply hard mutes and soft preferences
  → compose a bounded briefing
  → optionally run the editor model
  → render Markdown or email
  → deliver only through the selected surface
  → record later user feedback
```

The model editor has a deterministic fallback. A model outage does not prevent
an on-demand briefing from being rendered.

## Delivery policy

Proactive email is gated rather than unconditional. Correction Episodes are
searchable and Dream Cycle-readable, but Build 1 excludes them from proactive
Express delivery. A future accepted Steering Rule may have a different
delivery policy, but that promotion path is not implemented.

Email configuration is external to the repository. When required variables
are absent, the push command composes the output but skips sending.

## Entry points and data

| Purpose | Entry point |
|---|---|
| Compose, edit, render, gate, and send | `src/express.py` |
| On-demand CLI | `scripts/brief.py` |
| Proactive push | `scripts/express_push.py` |
| Agent-facing briefing | `src/mcp_server.py::memory_brief` |
| Delivery preferences | `express_feedback` table |

## Tests

- `tests/test_express.py`
- `tests/test_mcp_server.py`
- Dream Cycle tests that verify accepted and rejected synthesis state

## Related

- [Architecture Component Index](index.md)
- [Express plan and activation runbook](../EXPRESS-PLAN.md)
- [Express architecture](../ARCHITECTURE.md#express-delivery-layer)
- [Operations](../OPERATIONS.md#express-briefing--feedback)
- [ADR 0007: Correction Episodes before steering](../adr/0007-capture-correction-episodes-before-steering.md)
