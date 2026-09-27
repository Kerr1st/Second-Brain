# Project attribution: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Derive attribution from explicit project fields or source workspace metadata, not opaque session identifiers.
2. Normalize labels/paths consistently; empty, invalid-type, dot-prefixed and shallow absolute paths resolve to null.
3. Let an explicit ingestion project override a Project header; retain null when provenance supplies no project.
4. Preserve native Codex project ID/name/root metadata separately from the project label and source identity.
5. Include project in captured-source identity so identical content from different projects does not collapse.
6. Repair historical UUID tags through a reviewed target-bound manifest, retaining the original value in metadata.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
