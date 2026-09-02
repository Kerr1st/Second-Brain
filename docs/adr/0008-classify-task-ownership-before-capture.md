---
status: accepted
---

# Classify Agent Task ownership before capture

Every agentic-assistant Source Connector classifies each discovered Agent Task as `user-owned`, `delegated`, or `unknown` from native source evidence before applying eligibility rules. Only User-Owned Tasks are independently captured. Delegated Tasks are excluded because their useful results may already appear in the parent task's visible outcome; Unknown-Ownership Tasks are skipped and reported because assuming user ownership would admit duplicate or internal orchestration evidence.

## Considered Options

- Capture every discovered task and deduplicate later.
- Infer ownership from transcript content alone.
- Treat tasks without delegation evidence as user-owned.
- Require an evidence-based three-state ownership decision at each Source Connector.

## Consequences

Each connector documents the native fields or relationships that establish ownership and retains connector-specific fixtures for those record shapes. Shared behavioral contract tests verify the same observable policy across integrations, while native-data tests verify each connector's evidence mapping.

Codex Desktop is the reference implementation. Its ownership evidence includes `thread_source`, `thread_spawn_edges`, `agent_path`, and structured `source` metadata. Kiro, Claude Code, Quick Desktop, Amazon Quick, Amazon Q Developer, and later integrations must establish their own evidence before activation; an existing content heuristic is not automatically accepted as ownership proof.

This decision standardizes behavior, not a premature source-neutral runtime. Codex keeps its concrete implementation until a second connector proves which interface and implementation should be shared. Only then may common code be extracted at the demonstrated seam.
