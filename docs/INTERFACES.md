# Interface contracts

The canonical vocabularies and argument helpers live in `src/validation.py`. Unknown types, statuses, sources and relations are rejected at the boundary; migration 016 reinforces the source vocabulary with a `NOT VALID` CHECK, so historical out-of-vocabulary rows stay readable but new ones are refused. Historical data requires a measured migration, not silent relabeling.

## MCP

`src/mcp_server.py` exposes `memory_create`, `memory_search`, `memory_read`, `memory_update`, `memory_relate`, `memory_list`, `memory_recent_activity`, `memory_graph`, `memory_learn` and `memory_brief` over stdio (`scripts/jobs/mcp_serve.sh` is the launcher editor sessions point at). Read the registered function signatures for the exact schema.

Search/list limits are clamped to 1–100 and time windows to 0–365 days. Recent activity uses 1–90 days and 0–20 samples per source. Booleans, non-finite or fractional numbers and malformed integers are invalid. `memory_learn` accepts at most four distinct comma-separated topics of at most 200 characters before it invokes embeddings. `memory_graph` returns the neighbouring memory for either edge direction and excludes expired edges. `memory_read` does not ship the `search_vector` column.

Dictionary tools return `{error: message}`, string tools return `Error: message`, and list tools raise a safe `ValueError` which MCP reports as an error. Operational, pool and database diagnostics do not cross the boundary (`_guarded` translates residual psycopg2 shape errors); programming defects still surface for repair. Source SQL, constraint names, private row values and DSNs are never error messages.

### `memory_brief` and metered spend

`memory_brief(window_days=14, use_llm=False)` composes the Express briefing (insights, contradictions, resurfaced memories, digests, questions) and ranks it. With `use_llm=False` (the default) no backend is resolved and nothing is spent. With `use_llm=True` the briefing goes through `express.edit_briefing`, which resolves the `editor` role of the active profile (`controlled`: Amazon Nova 2 Lite on direct Bedrock, Flex tier, 4,096 output tokens) and calls it once through `invoke_metered` with its own `SpendTracker`, threshold `SECOND_BRAIN_EDITOR_CAP_USD` (default **$0.25**, a post-response stop threshold that one call can overshoot). The role is a corpus role, so the Kiro/Fable denial applies. A recoverable editor failure (`RuntimeError`, `OSError`, `ValueError`, including a malformed editor reply) falls back to deterministic ranking and records `editor_error`; spend and usage failures propagate without a second call. The briefing dict carries `editor` (`llm` | `deterministic` | `empty`), `editor_cost_usd` and `editor_spend_unknown`; the rendered Markdown the MCP client receives does not itemize cost, so an MCP caller who passes `use_llm=True` is accepting a metered Bedrock call per invocation. The scheduled email push is a separate product decision and is not enabled by this tool.

### Trusted and read-only registries

The interactive MCP registry includes writes and access reinforcement. `src/mcp_readonly.py` is an explicit separate registry with no write tools and a search that does not reinforce access counts, intended for discovery agents. The Claude Code Explorer attaches it (routing lane, branch `feat/routing-lane`): `--mcp-config` starts `python -m src.mcp_readonly`, `--tools ""` disables built-in tools and `--allowedTools` grants only `memory_search`, `memory_list`, `memory_read` and `memory_recent_activity` (`READONLY_TOOL_GRANTS`, `src/backends/claude_code.py`). `memory_graph` is served but not granted. Other tool-using stages still start `python -m src.mcp_server`. A prompt saying "read only" is not the capability boundary.

## Source and candidate contracts

Capture documents require a non-empty canonical source identity (channel + URL; `project` is descriptive, not part of identity) and preserve channel, source location, project when supported, author/role and time metadata. Stable identities let a changed source refresh transactionally; old chunks that are cited stay as superseded history. Source acquisition success, ingestion success and model success have separate outcomes and exit codes ([CAPTURE-COMPONENTS.md](CAPTURE-COMPONENTS.md)).

Dream candidates are proposals with cited source ids, an operation and an optional target. Only exact `ACCEPT`/`REJECT` verdicts count as votes. Storage independently authorizes targets and evidence and commits each candidate atomically. Audit and revision metadata stay visible in the ledger but are excluded from the voting projection.

## HTTP capture API

`src/capture_api.py` (port 8100, launched by `scripts/jobs/capture_api.sh` in service mode) accepts explicit caller-provided evidence under a bearer token read from the gitignored `.env`; the launcher refuses to start without it. Labels are `api_browser`, `api_email`, `api_slack`. `/health` returns 503 whenever the database is unreachable, which the job runner treats as a failed probe.

## Python and shell

The `src.db` facade retains supported imports while implementation lives in concern-specific modules (`pool`, `columns`, `memories`, `relationships`, `capture_queries`, `search_queries`, `express_queries`, `maintenance`). Optional `conn=` arguments join an existing transaction; callers must not commit a borrowed transaction. The dream-cycle repository facade separates runs, feedback, metrics, attention and briefing policy.

Every application path works with DML-only privileges (migration 017); DDL, `TRUNCATE`, `REINDEX` and `VACUUM` belong to the owner role and appear only in migrations, owner-gated maintenance scripts and the test fixture's owner connection. `migrations/migrate.sh` reads `DB_MIGRATE_USER`, `DB_OWNER_ROLE`, `DB_APP_ROLE`, `DB_MIGRATE_PASSWORD` and `MIGRATE_UNTIL` from the environment and exits 2 when the connected role neither owns the database nor is a direct member of its owner (superuser status is not an exemption), or when owner and app role are the same name; migration 017 itself RAISEs if the app role is a member of the owner role or lacks DML after the grants.

Job wrappers preserve the child's exit code, enforce one lock and one deadline per job and write machine-readable status under `logs/jobs/` ([scheduling/README.md](../scheduling/README.md) lists the exit-code taxonomy). A timeout, capture failure, missing prerequisite or empty required output is never success. Maintenance commands default to a dry-run manifest; mutation requires the explicit application of a qualified plan.
