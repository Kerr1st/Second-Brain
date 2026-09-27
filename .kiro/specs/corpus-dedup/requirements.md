# Source identity and resumable repair: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Create a dry-run manifest of identities, row fingerprints and database incarnation before destructive repair.
2. Merge only identical captured-source content with matching project/type/chunk identity and status; keep derived tasks distinct.
3. Revalidate rows and reference layout under locks; refuse unknown references or changed identities.
4. Remap foreign keys, exact JSON/array IDs, candidate/slice/skim references and lineage before duplicate deletion.
5. Plan complete chunk sequences from parents; revalidate parent status and active children after embedding preparation.
6. Cache only finite 1024-element vectors; persisted operation progress and cached preparation support safe resume.
7. Atomically supersede chunks, install replacements and record maintenance; preserve cited historical IDs.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
