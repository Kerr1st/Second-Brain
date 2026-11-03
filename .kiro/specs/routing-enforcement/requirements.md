# Corpus routing: acceptance criteria

Reviewed against the refactor source on 2026-09-26 and re-pinned to the R7 head of the release line on 2026-10-06. These requirements describe behavior, not deployed status; carried by: R1 (deployed) (see [RELEASES.md](../../../docs/RELEASES.md)).

1. Default to controlled; validate configured roles and explicit/programmatic overrides before invocation.
2. Reject Kiro backends and Fable models for every corpus role at resolution and invocation boundaries.
3. Use dedicated distill, codex_semantic and editor roles. The grounding audit is one graded call through the skeptic seat's backend (`GROUNDING_AUDIT_ROLE`) whose record is excluded from the voting projection; a dedicated `grounding_auditor` role is an open item of the routing lane.
4. Pass the configured model id to the CLI unchanged and record it as requested provenance; canonical-alias mapping for the Claude wrapper is **not** in the release line (open item).
5. Treat a CLI report that the requested model was not honoured as a routing failure for corpus stages (`CorpusRoutingError`) after billing the call's usage; never silently continue on a server-selected model.
6. Keep `src/mcp_readonly.py` as an explicit registry of exactly five read endpoints with no access reinforcement. **Open:** the Explorer still attaches the full `src.mcp_server` (`--mcp-config` + `--strict-mcp-config`); tool-less calls use `--strict-mcp-config --tools ""`.
7. Qualify provider and tools per release with synthetic content; document that the wrapper's AWS account identity is not proven by an `AWS_PROFILE` value.

Qualification evidence and unresolved release gates belong in [RELEASES.md](../../../docs/RELEASES.md) and the private release notes it names.
