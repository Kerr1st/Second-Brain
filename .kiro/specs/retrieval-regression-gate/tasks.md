# Retrieval regression and ranking ownership: implementation and gates

- [x] Source implementation: Extract retrieval helpers and centralize coefficients while preserving public search behavior.
- [x] Isolated regression implementation: Maintain the 36-query gate, unembedded-parent case and independent rerank oracle.
- [x] Integrated qualification: the full isolated suite passes at the head of the carrying release (per-release totals and rehearsal evidence in [RELEASES.md](../../../docs/RELEASES.md) and the private notes). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Compare meaningful approved queries on the chosen repaired corpus before releasing ranking changes.
- [ ] Product effectiveness: Compare ordinary retrieval with discoveries for the same task; measure human usefulness separately.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
