# Provider request and accounting: implementation and gates

- [x] Source implementation: tool exclusion, shared usage/parse handling, metered failure propagation, transport reservations, input-count reconciliation, per-seat output budgets (R1).
- [x] Isolated regression implementation: partial-usage, invalid-threshold, billed-failure, reservation and reconciliation regressions.
- [x] Integrated qualification: the full isolated suite passes at the head of the carrying release (per-release totals and rehearsal evidence in [RELEASES.md](../../../docs/RELEASES.md) and the private notes). Deployment and usefulness remain separate gates.
- [x] Deployment: R1 deployed 2026-10-05 (`8dd3bb7`); the first scheduled dream cycle under it is the live check of the reservation path.
- [ ] Operational qualification: reconcile client-side priced spend with the AWS invoice for one month; qualify any new model id with a price-table entry before pointing a role at it.
- [ ] Product effectiveness: compare discovery quality per known reasoning cost, reporting missing and embedding costs separately.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
