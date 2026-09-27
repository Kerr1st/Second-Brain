# Retrieval regression and ranking ownership: implementation and gates

- [x] Source implementation: Extract retrieval helpers and centralize coefficients while preserving public search behavior.
- [x] Isolated regression implementation: Maintain the 36-query gate, unembedded-parent case and independent rerank oracle.
- [x] Integrated qualification: **1,724 passed** in `/private/tmp/sbtest-xhkzov6p`; see the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Compare meaningful approved queries on the chosen repaired corpus before releasing ranking changes.
- [ ] Product effectiveness: Compare ordinary retrieval with discoveries for the same task; measure human usefulness separately.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
