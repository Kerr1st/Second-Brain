# Scheduled operation evidence: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. These requirements describe behavior, not deployed status; carried by: R5 (see [RELEASES.md](../../../docs/RELEASES.md)).

1. Use one wrapper for environment selection, per-job locking and child exit forwarding.
2. Reject duplicate launches without overwriting the active run's authoritative status.
3. Bound batch execution and stop its owned process group on timeout, interruption or unexpected monitor failure.
4. Treat API services separately: require startup health and publish periodic health/heartbeat status.
5. Persist running/succeeded/failed/interrupted state, failure age/counts and last success; desktop notification is optional.
6. Report invalid status, overdue batches, stale service heartbeats and silent required capture channels as unhealthy.
7. Wait for the network before starting a network-dependent job and report an unreachable network as infrastructure (exit 68), never as a job failure; never auto-start PostgreSQL (exit 69); every plist carries a committed deadline and `ExitTimeOut` ≥ grace + 20 s.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
