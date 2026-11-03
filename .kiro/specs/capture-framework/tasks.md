# Capture channels and vocabulary: implementation and gates

- [x] Source implementation: Implement common validation/outcomes, native session parsing and atomic refresh with specialized adapters.
- [x] Isolated regression implementation: Retain malformed/partial source, DST, dry-run, refresh and semantic retry regressions.
- [x] Integrated qualification: the full isolated suite passes at the head of the carrying release (per-release totals and rehearsal evidence in [RELEASES.md](../../../docs/RELEASES.md) and the private notes). Deployment and usefulness remain separate gates.
- [ ] Deployment/operational qualification: Qualify actual source access and scheduled catch-up per channel; parser fixtures do not prove live coverage.
- [ ] Product effectiveness: Check that approved discovery inputs contain timely evidence from the user's actual work surfaces.

Unchecked gates are not inferred from source completion, historical checkboxes or synthetic success.
