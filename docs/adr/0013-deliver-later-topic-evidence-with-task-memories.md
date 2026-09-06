---
status: accepted
---

# Deliver later topic evidence with task memories

A normal recruiter-preparation task retrieved an older draft decision while
omitting a later user correction and sent-status update. All three memories
already existed. The context broker used the legacy search helper, which
truncated candidates before utility ranking, then prioritized inferred memories
over evidence. Neither step followed the older memory's derivation links.

The broker now uses the stable candidate retrieval path before packing. For a
Codex task-memory or Topic Segment hit, it follows `derived_from` links to
active task decisions, insights, and correction episodes from that exact Topic
Segment. It delivers those memories in reverse supporting-turn order. Later
evidence is therefore available even if its wording ranked below the old plan.
The grouping does not expand to the whole Captured Task or infer a Semantic
Project from its workspace. Explicit project exclusions remain in force.

Approved applicable Steering Rules retain priority. Within a task-evidence
group, source order precedes the inferred/evidence display order in ADR 0011.
Each item's authority and exact supporting IDs remain unchanged. Older items
are labeled historical and must be read with the later evidence. If a later
item cannot fit, older members of that group are also omitted, rather than
silently presenting an old claim alone. The original item and token limits,
receipts, and outcome reporting remain in effect.

This is not semantic supersession. A later statement need not contradict an
earlier one, and a correction episode does not become governing guidance.
Historical content, approval, and the Dream Cycle are not rewritten. Cross-topic
or cross-task contradictions, missing distillation, and over-broad topic
segmentation remain limitations. The private original example and an untuned
second job-search query are retained as evaluation evidence, not public fixtures.

For Codex source and distilled-task records, `observed_at` is returned only when
explicitly present in provenance. Database creation time records processing, not
when a supporting turn occurred; absent source timestamps remain unknown.
