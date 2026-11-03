# Capture channels and vocabulary: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. These requirements describe behavior, not deployed status; carried by: R4 (see [RELEASES.md](../../../docs/RELEASES.md)).

1. Run channel preflight before collection and carry per-item fetch failures into the final outcome.
2. Require nonempty identity/body, deduplicate each batch and retain typed source provenance.
3. Use shared rendering and timezone-aware calendar windows, including 23/25-hour daylight-saving days.
4. Keep dry runs read-only with processed=0 and separate planned counts; attempted failures yield a failing exit status.
5. Prepare embeddings before atomically committing parent/chunks; refresh retains cited old chunks as superseded history.
6. Capture native IDE user/assistant Say content only; reject incomplete/changing records and exclude tools, reasoning and system payloads.
7. Keep Codex semantic batching/retry contracts specialized while sharing source/outcome boundaries.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
