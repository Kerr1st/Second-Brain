# Relationship provenance and expiry: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Validate relation type and endpoint IDs; refuse self-links.
2. Canonicalize related_to/contradicts endpoint order so reverse creation has one symmetric identity.
3. Preserve other relation directions, including opposite directed edges between the same memories.
4. Expire associations only after creation grace and both endpoints' recent-access grace have elapsed.
5. Never expire derived_from/superseded_by; repair premature historical expiry before relying on live-edge reads.
6. Return the other endpoint for either traversal direction; exclude expired links unless explicitly requested.
7. Accept caller-owned transactions so edge changes can roll back with discovery storage.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
