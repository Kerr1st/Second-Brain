# Effectiveness evaluation: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. **Status: the frozen-source trial runner of the refactor branch (`scripts/eval/discovery_trial.py`, `src/evaluation/`) was not ported into the release line.** What exists is the retrieval diagnostics under `scripts/eval/` and the protocol in [docs/VALIDATION.md](../../../docs/VALIDATION.md). These criteria describe what a trial must satisfy when it is re-implemented or run by hand; none of them is met today.

1. Freeze 1–35 records with source identities, content hashes, a current task and explicit omitted stages before any private content moves.
2. Bind execution to the reviewed manifest, the role/skim configuration and the embedding destination; reject drift before ingestion.
3. Run against a fresh socket-only cluster whose application tables start empty, never against the installed database.
4. Capture ordinary retrieval before native discovery; retain stage status, candidate evidence, the briefing and blinded comparison packets.
5. Report aborts, known reasoning cost, missing spend and unmeasured embedding cost without fabricated completion or zero charges.
6. Accept explicit human ratings with supporting observations; leave usefulness, action and outcome unknown until reviewed.
7. Retrieval diagnostics (`scripts/eval/*`: golden/curated rank, cold/warm rerank, ablation, HNSW recall) measure index recall and ranking agreement, not usefulness; golden queries derived from memories are not a held-out set.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
