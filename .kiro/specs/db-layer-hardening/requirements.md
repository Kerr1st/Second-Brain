# Database lifecycle and SQL ownership: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. These requirements describe behavior, not deployed status; carried by: R3, R7 (see [RELEASES.md](../../../docs/RELEASES.md)).

1. Bound pooled connections and acquisition time; concurrent borrowers must not share a connection.
2. Roll back read or unfinished transactions before reuse; discard connections that cannot be recovered.
3. Commit an explicit transaction once; operations receiving conn= participate without independently committing.
4. Allowlist writable columns, compose identifiers safely and parameterize values using shared validation.
5. Apply each migration and its tracking row atomically under an advisory lock; concurrent runners re-check applied state.
6. Run every application path as a NOSUPERUSER role holding DML only (migration 017); schema ownership and migrations belong to a separate owner role; `migrate.sh` refuses any other role (exit 2) and takes role names from the environment, never from SQL files.
7. Drop retired tables only behind a verified export marker whose row counts match (migration 018); preserve caller PGOPTIONS, including read-only sessions.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
