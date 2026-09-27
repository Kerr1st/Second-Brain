# Interface contracts

The canonical vocabularies and argument helpers live in `src/validation.py`. Unknown types, statuses, sources and relations are rejected at the boundary. Storage constraints reinforce the contract; historical out-of-vocabulary data requires a measured migration, not silent relabeling.

## MCP

`src/mcp_server.py` exposes memory_create, memory_search, memory_read, memory_update, memory_relate, memory_list, memory_recent_activity, memory_graph, memory_learn and memory_brief over stdio. Read the registered function signatures for the exact schema.

Search/list limits are clamped to 1–100 and time windows to 0–365 days. Recent activity uses 1–90 days and 0–20 samples per source. Booleans, nonfinite/fractional numbers and malformed integers are invalid. Learn accepts at most four distinct comma-separated topics of at most 200 characters before it invokes embeddings. Graph returns the neighboring memory for either edge direction.

Dictionary tools return `{error: message}`, string tools return `Error: message`, and list tools raise a safe ValueError which MCP reports as an error. Operational/pool/database diagnostics do not cross the boundary. Programming defects still surface for repair. Source SQL, constraint names, private row values and DSNs are not error messages.

The trusted interactive MCP registry includes writes and access reinforcement. Discovery uses `src/mcp_readonly.py`, an explicit separate registry with no write tools or access-count mutation, backed by a read-only PostgreSQL session. A prompt saying “read only” is not the capability boundary.

## Source and candidate contracts

Capture documents require a nonempty canonical source identity and preserve channel, source location, project when supported, author/role and time metadata. Stable identities allow a changed source to refresh transactionally. Source acquisition success, ingestion success and model success have separate outcomes.

Dream candidates are proposals with cited source IDs, operation and optional target. Only exact validated verdicts count as votes. Database storage independently authorizes targets and evidence and commits each candidate atomically. Candidate audit/revision metadata remains visible in the ledger but is excluded from review-seat voting input.

## Python and shell

The `src.db` facade retains supported imports while implementation resides in concern-specific modules. Optional `conn=` arguments join an existing transaction. Callers must not commit that borrowed transaction themselves. The Dream repository facade similarly separates runs, feedback, metrics, attention and briefing policy.

Job wrappers preserve command exits, enforce bounded execution/locking and write machine-readable status. A timeout, capture failure, missing prerequisite or empty required output must not become success. Maintenance commands default to a dry-run manifest; mutation requires explicit application of a qualified plan.
