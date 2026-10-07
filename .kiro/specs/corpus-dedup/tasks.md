# Source identity and resumable repair: implementation and gates

- [x] Source implementation: Implement reviewed manifests, reference-preserving dedup and atomic resumable rechunking.
- [x] Isolated regression implementation: Retain drift, unknown-FK, partial-chunk, invalid-vector, rollback and cached-resume regressions.
- [x] Integrated qualification: the full isolated suite passes at the head of the carrying release (per-release totals and rehearsal evidence in [RELEASES.md](../../../docs/RELEASES.md) and the private notes). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Review manifests on the restored target; authorize paid embedding work separately before live application.
- [ ] Product effectiveness: Verify source coverage and stable citations after repair, then rerun retrieval/discovery evaluation.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
