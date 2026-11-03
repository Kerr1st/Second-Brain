# Scheduled operation evidence: implementation and gates

- [x] Source implementation: Implement shared lifecycle status, batch/service monitoring and process-group cleanup.
- [x] Isolated regression implementation: Retain signal, timeout, failed-spawn, silent-channel, network-preflight, advisory-reason and plist-table regressions.
- [x] Integrated qualification: the full isolated suite passes at the head of the carrying release (per-release totals and rehearsal evidence in [RELEASES.md](../../../docs/RELEASES.md) and the private notes). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Install the plists as symlinks, observe capture-api heartbeat and the first dark-wake Monday weekly-digest run before removing it from `[jobs.advisory]`.
- [ ] Product effectiveness: Measure whether source gaps and discovery failures are detected before recommendations become stale or disappear.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
