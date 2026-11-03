# Discovery authorization and truthful completion: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. These requirements describe behavior, not deployed status; carried by: R3 (see [RELEASES.md](../../../docs/RELEASES.md)).

1. Require exact ACCEPT/REJECT from four independent seat requests and three accepts; the vote is a heuristic and claims no fault-tolerance guarantee.
2. Keep grounding audit and revision history out of voting projections while retaining revision provenance.
3. Authorize UPDATE/SUPERSEDE deterministically; protect captured evidence/governing guidance independently of model votes.
4. Validate source/target snapshots under ordered locks and source-set dedup protection before committing.
5. Commit memory mutation, relationship changes and the accepted candidate ledger row in one transaction.
6. Use an exclusive run advisory lock and a maximum of two candidates per Thinker slice; preserve prior successes and partial counts on abort.
7. Record known/unknown spend and stable run/digest identity; never fabricate votes on infrastructure failure.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
