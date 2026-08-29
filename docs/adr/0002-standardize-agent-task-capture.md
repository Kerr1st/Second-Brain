---
status: accepted
---

> The separate Topic Segmentation and Task Distillation call/stage design below is superseded for
> Codex v1 by ADR 0006. The shared task, turn, segment, and Dream Cycle boundaries remain accepted.

# Standardize agent task capture around prompt-outcome turns and topic segments

Every current and future agentic-assistant integration will normalize its source task into a stable Captured Task containing ordered Agent Turns: each turn is the user's prompt and the agent's visible outcome, excluding hidden reasoning, system instructions, tool activity, and progress commentary. Complete turns are grouped into semantic Topic Segments only when they have a distinct purpose, form a coherent unit, and have independent search or distillation value; uncertain boundaries and brief asides are merged with their surrounding segment. Every qualifying segment remains stored and searchable even when it produces no immediate decision, insight, or Correction Episode.

After source capture commits, the same capture invocation performs the one Task Semantic Pass defined by ADR 0006. It returns Topic Segments and optional task-level decisions, insights, or Correction Episodes without multi-judge consensus. The semantic result is atomic; failure preserves source capture and leaves the single Semantic Processing Cursor unchanged so the entire unprocessed tail retries. The Dream Cycle remains a separately scheduled consolidation stage that later creates higher-order synthesis across memories and subjects candidates to its evaluator panel.

## Considered Options

- Store each assistant transcript only as one large source record.
- Split transcripts into fixed-size chunks or source-specific units.
- Retain only source portions that immediately produce a durable memory.
- Let every assistant integration define its own capture and distillation model.

## Consequences

Each integration must provide stable task and turn identities, preserve complete prompt-outcome pairs, and support refresh without duplicating the Captured Task. Source identity is namespaced by integration: similarly named units or matching native IDs from different assistants never identify the same Agent Task. For example, Amazon Quick Sessions and Quick Desktop Sessions come from different storage locations and remain distinct despite both products using the word “session.” Segment boundaries need structured evidence and conservative reconciliation so completed segments remain stable when a task resumes. Existing integrations will adopt this standard incrementally rather than through a big-bang rewrite.

The capture path treats a successful source commit as successful capture and invokes the combined semantic pass immediately for the unprocessed tail. A retry does not roll back source evidence or require a new source turn; it processes the same tail because the cursor did not advance.

Task Refresh is monotonic as recorded in ADR 0003: captured turns remain immutable and only unseen complete turns are appended. Source Drift never rewrites established evidence.

Correction Episodes use neutral, user-attributed language and cite both the visible agent outcome containing the misalignment and the correcting user prompt. They remain separate real episodes even when materially similar, are searchable and available to the Dream Cycle, and are excluded from proactive delivery and automatic steering. See ADR 0007.

Before any of these rules apply, the Source Connector classifies Task Ownership
from native evidence. Only User-Owned Tasks enter this capture model; Delegated
Tasks are excluded, and Unknown-Ownership Tasks are skipped and reported. See
ADR 0008.
