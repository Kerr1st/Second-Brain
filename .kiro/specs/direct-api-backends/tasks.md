# Provider request and accounting: implementation and gates

- [x] Source implementation: Implement tool exclusion, shared usage/parse handling and metered failure propagation.
- [x] Isolated regression implementation: Maintain partial-usage, invalid-cap and billed-failure regressions.
- [x] Integrated qualification: **1,724 passed** in `/private/tmp/sbtest-xhkzov6p`; see the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Qualify selected provider/model/options and reconcile client estimates with billing before release.
- [ ] Product effectiveness: Compare discovery quality per known reasoning cost, reporting missing and embedding costs separately.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
