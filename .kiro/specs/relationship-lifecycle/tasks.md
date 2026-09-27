# Relationship provenance and expiry: implementation and gates

- [x] Source implementation: Implement symmetric identity, directed traversal and protected provenance expiry.
- [x] Isolated regression implementation: Retain new-edge grace, recent-access, permanent-provenance and opposite-direction regressions.
- [x] Integrated qualification: **1,724 passed** in `/private/tmp/sbtest-xhkzov6p`; see the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Qualify/apply the historical expiry backfill before enabling live-edge filtering on the active corpus.
- [ ] Product effectiveness: Inspect citations and transferred project context to confirm useful links survive while stale associations lose influence.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
