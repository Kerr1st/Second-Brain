# Scheduled operation evidence: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Use one wrapper for environment selection, per-job locking and child exit forwarding.
2. Reject duplicate launches without overwriting the active run's authoritative status.
3. Bound batch execution and stop its owned process group on timeout, interruption or unexpected monitor failure.
4. Treat API services separately: require startup health and publish periodic health/heartbeat status.
5. Persist running/succeeded/failed/interrupted state, failure age/counts and last success; desktop notification is optional.
6. Report invalid status, overdue batches, stale service heartbeats and silent required capture channels as unhealthy.
7. Keep deployment/retirement inventory separate from enabled scheduler state; preparation must not activate or delete jobs.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
