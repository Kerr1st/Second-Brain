# Second Brain Project Charter

> **Status: canonical scope.** Accepted 2026-08-29.

Second Brain is a user-controlled, cross-agent learning and governance system. It captures evidence
from agent tasks and other sources, distills durable knowledge, recalls relevant context, and turns
validated user direction into reviewed guidance that can improve future agent behavior.

The system owns four planes:

1. **Evidence** — Agent Tasks, Agent Turns, Topic Segments, decisions, insights, Correction Episodes,
   source ownership, and Exact Provenance.
2. **Learning** — hybrid retrieval, task distillation, Dream Cycle synthesis, duplicate and conflict
   discovery, and outcome evaluation.
3. **Governance** — inactive Steering Candidates, independent consensus, explicit approval,
   Authority Scope, Applicability, versioning, and supersession.
4. **Adaptation** — bounded context packs and reviewed target adapters for `AGENTS.md`, future
   steering files, skills, hooks, tests, lint, or CI.

Second Brain does not replace an agent harness's native memory, depend on undocumented native
memory files, orchestrate arbitrary coding work, or automatically turn observed text into governing
instructions. Source content is evidence; only explicit approval creates authority. Publication or
enforcement is a separate, least-privilege, reviewable action.

## Delivery strategy

Develop cross-cutting capabilities as Vertical Slices. Prove one Reference Integration through
source evidence, durable processing, retrieval or governance, delivery, and recorded outcome before
generalizing to other integrations. Codex is the current Reference Integration. A second real
adapter must demonstrate what varies before a shared integration seam is extracted.

## Measure of value

The highest-value result is not a stored memory. It is an evidence-backed improvement in what the
user or a future agent can retrieve, decide, or do, with an outcome record showing whether the
guidance was followed, corrected, unused, or still unknown.

## Related

- [Architecture Component Index](components/index.md)
- [ADR 0010](adr/0010-prove-capabilities-vertically-before-generalizing.md)
- [ADR 0011](adr/0011-use-bounded-context-packs-and-outcome-receipts.md)
- [Purpose and evolution](SECOND-BRAIN-PURPOSE-AND-EVOLUTION.md)
