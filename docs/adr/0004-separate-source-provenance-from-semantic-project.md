---
status: accepted
---

# Separate source provenance from semantic project attribution

Every agent integration preserves its native project or grouping, workspace history, and repository
context as source provenance. These values describe where an Agent Task ran, not necessarily what
its knowledge concerns, so they never populate `memories.project` automatically. A Topic Segment or
task-distilled memory receives a semantic project only when the task content clearly establishes
that subject; otherwise, `project` remains unset.

Codex Build 1 deliberately defers content-based semantic-project classification. It preserves the
native Codex Project, workspace history, and Git context as provenance and leaves `project` unset
for Captured Tasks, Topic Segments, and derived memories. A later build may implement the policy
above through an explicit semantic result field; source location must still never be copied into
`project` automatically.

## Considered Options

- Copy the source-native project or current workspace into `memories.project` by default.
- Infer a project from location first and override it when task content disagrees.
- Preserve location only as provenance and require clear semantic evidence for project attribution.

## Consequences

Memories created from wandering or cross-project tasks do not acquire misleading project labels
merely because of where the conversation ran. Some captured or distilled records will remain
unscoped until their subject is explicit or later classification assigns one. Source provenance is
still retained, so operational origin and semantic meaning remain independently searchable and
auditable.
