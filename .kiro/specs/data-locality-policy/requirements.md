# Private source boundaries: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. These requirements describe behavior, not deployed status; carried by: R1, R5 (see [RELEASES.md](../../../docs/RELEASES.md)).

1. Describe CLI/stdio as transport, not proof of local inference; identify actual provider and model.
2. Bind any private trial to exact reviewed records and configuration; narrower earlier approval does not cover added content (procedure in docs/VALIDATION.md; no runner in the release line).
3. Keep call captures, digests and debug artifacts private; raw debugging requires explicit opt-in.
4. Encrypt backups before cloud transfer and verify the local encrypted payload before exposing the final archive path.
5. Claim cloud verification only after matching ciphertext readback; failures retain local ciphertext without a success receipt.
6. Verify key custody and full restore separately from checksums; reject unsafe archive members and payload tampering.
7. Do not infer wrapper AWS account identity from a supplied profile; record unresolved identity explicitly.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
