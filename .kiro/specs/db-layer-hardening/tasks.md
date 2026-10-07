# Database lifecycle and SQL ownership: implementation and gates

- [x] Source implementation: Implement connection ownership, conn= composition, validated updates and atomic migration tracking.
- [x] Isolated regression implementation: Retain disposable-SQL rollback, concurrency, stale-connection and migration-failure tests.
- [x] Integrated qualification: the full isolated suite passes at the head of the carrying release (per-release totals and rehearsal evidence in [RELEASES.md](../../../docs/RELEASES.md) and the private notes). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Apply 016/017/018 in their windows after the verified backup and the hard gate; run the app-role smoke before and after the demotion (R7 runbook).
- [ ] Product effectiveness: Confirm capture and discovery retain correct data through failures without elevated runtime privileges.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
