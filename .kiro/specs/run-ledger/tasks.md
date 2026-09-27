# Discovery authorization and truthful completion: implementation and gates

- [x] Source implementation: Implement deterministic authorization, candidate atomicity, run exclusion and truthful partial completion.
- [x] Isolated regression implementation: Retain concurrent storage, rollback, protected-target, cap-abort and invalid-verdict regressions.
- [x] Integrated qualification: **1,724 passed** in `/private/tmp/sbtest-xhkzov6p`; see the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Qualify the selected provider panel and approved bounded trial before enabling scheduled discovery.
- [ ] Product effectiveness: Review accepted discoveries against sources and record changed actions; panel acceptance is not usefulness.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
