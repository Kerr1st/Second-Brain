# Corpus routing: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Default to controlled; validate configured roles and explicit/programmatic overrides before invocation.
2. Reject Kiro backends and Fable models for every corpus role at resolution and invocation boundaries.
3. Use dedicated distill, codex_semantic, editor and grounding_auditor roles; auditing does not occupy a voting seat.
4. Map recognized Bedrock Claude wrappers to the same canonical CLI model version while retaining requested provenance.
5. Reject observed served-model drift and preserve billed usage; never silently accept another family/version.
6. Expose/grant exactly five read-only Explorer MCP endpoints; disable built-ins, ambient MCP, skills and local settings discovery.
7. Keep Explorer DB sessions read-only without access reinforcement; qualify provider/tools and document unresolved account identity.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
