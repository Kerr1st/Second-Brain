# Database lifecycle and SQL ownership: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Bound pooled connections and acquisition time; concurrent borrowers must not share a connection.
2. Roll back read or unfinished transactions before reuse; discard connections that cannot be recovered.
3. Commit an explicit transaction once; operations receiving conn= participate without independently committing.
4. Allowlist writable columns, compose identifiers safely and parameterize values using shared validation.
5. Apply each migration and its tracking row atomically under an advisory lock; concurrent runners re-check applied state.
6. Support non-superuser runtime access and preserve caller PGOPTIONS, including read-only sessions.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
