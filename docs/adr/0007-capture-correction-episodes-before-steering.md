---
status: accepted
---

# Capture Correction Episodes before governing steering

The Task Semantic Pass may emit a `correction_episode` beside a decision or insight. A Correction
Episode is a neutral, user-attributed record of what was misaligned and what the user indicated
instead, with Exact Provenance to both the visible agent outcome and correcting prompt. It is stored
through the existing memory hierarchy as searchable episodic evidence, remains available to the
Dream Cycle, and is excluded from proactive delivery and automatic steering.

The corrected expectation preserves the narrowest faithful substance and terminology of the
correcting prompt. It does not add downstream implications, implementation consequences,
generalized rules, or claims supplied only by the agent's acknowledgement; those belong to later
decisions or Dream Cycle synthesis.

Conditional clarification requests abstain. Asking to preserve something if it is already the
standard, while requesting an explanation otherwise, is not a correction unless the user also
unambiguously identifies the prior outcome as wrong and states its replacement.

Build 1 implements only this evidence layer for Codex. A correction may extend the immediately
preceding Topic Segment; nonadjacent historical correction lookup, live hooks, new model calls,
dedicated tables, and a generic multi-integration runtime remain deferred. Codex stays the reference
implementation until a second agent integration supplies the second real adapter needed to extract
a shared capture module.

Build 1 passed its bounded real-data Proof Gate on 2026-07-23. The Codex-first Build 2 vertical
slice was implemented and live-proofed on 2026-08-29. The Dream Cycle can now evaluate a Steering
Candidate from Correction Episodes or explicit durable direction through four independent roles
and the existing three-of-four quorum. An accepted candidate is a retained recommendation, not an
active Steering Rule: the user must separately approve its wording, Authority Scope, and
Applicability. Duplicate settled candidates are suppressed before panel work; materially changed
or contradictory evidence creates a versioned Supersession Candidate. Approval preserves prior
rule history, and publication to `AGENTS.md` requires a reviewed diff and matching file digest.

## Considered Options

- Convert every detected correction directly into an active rule.
- Add a live correction hook or separate classification call.
- Store category, scope, confidence, keywords, occurrence counters, or decay state during capture.
- Introduce a dedicated correction table or generic artifact framework before another integration.
- Preserve minimal Correction Episodes first and defer governance to the Dream Cycle.

## Consequences

Capture still records only `what_was_misaligned`, `corrected_expectation`, the containing Topic
Segment, and supporting Agent Turn IDs. Classification, recurrence, Authority Scope, Applicability,
duplicate resolution, and rule wording remain Steering Governance concerns. Repeated correction
events remain separate evidence; same-Task and cross-Task recurrence are derived during evaluation
rather than stored as mutable counters or fixed numerical eligibility gates. Approved rules are
versioned memories; publication remains a separate, reversible adapter action.
