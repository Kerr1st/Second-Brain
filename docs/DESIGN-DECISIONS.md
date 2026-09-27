# Design decisions

These decisions explain current contracts; archived research records preserve their historical rationale without becoming operating instructions.

1. **Optimize for useful connections.** A discovery must connect evidence to a current problem with an explicit transfer mechanism and adaptation. More records, generic reminders and panel agreement are intermediate signals.
2. **Keep raw evidence and derived claims separate.** Source identities, roles, versions and lineage survive ingestion, dedup and discovery. Model proposals cannot overwrite captured evidence or governing instructions.
3. **Use one transactional storage boundary per logical result.** Source revisions and candidate operations each commit atomically. Model/embedding preparation precedes mutation; failure preserves earlier complete work.
4. **Combine lexical and vector retrieval.** Full-text search can reach unembedded sources. Synthetic ranking regressions protect code behavior; held-out user tasks evaluate meaning and benefit.
5. **Keep the existing four-seat policy while measuring benefit.** Three accepts decide model review, with cross-family agreement in the controlled configuration. This is a heuristic panel, not Byzantine fault tolerance or Bayesian confidence updating. Audit/revision is separate and hidden from voting projections.
6. **Enforce capabilities in tools and storage.** Explorer has a read-only registry/database session. Deterministic storage authorization is independent from a model vote. Prompts alone cannot guarantee those boundaries.
7. **Account for failed paid calls.** A valid response can still be malformed or refused. Count its usage before retry. Unknown potentially paid usage fails closed. A post-response spend threshold can overshoot and is distinct from an absolute cost ceiling.
8. **Preserve specialized capture semantics.** Common rendering/windows/outcomes reduce duplication, while Codex task/turn segmentation and retry state remain specialized. CLI-local execution is not model locality.
9. **Retire unused writers before deleting their data.** Graph tables are not part of current discovery retrieval. Preserve them in recoverable backups; source control is not data recovery.
10. **Qualify source, operation and effectiveness separately.** Offline tests, live provider probes, operational receipts and human outcomes each establish different facts. Documentation records the exact boundary reached.
