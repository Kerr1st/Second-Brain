# Capture components

Source adapters acquire evidence. Common ingestion stores it atomically. A capture report is evidence about that attempt, not proof that every source surface was observed.

| Surface | Entry point | Identity and evidence boundary |
|---|---|---|
| Codex tasks | `src/capture/codex.py` | User prompt plus visible final outcome, stable task/turn identity; excludes hidden reasoning, tool output and delegated activity |
| Kiro CLI and IDE | `src/capture/chats.py`, `src/parsers/` | Legacy exports plus native IDE `~/.kiro/sessions/<workspace>/<session>/session.json` and `messages.jsonl`; explicit user and assistant `Say` only |
| Slack | `src/capture/slack.py` | Conversation/day or thread identity, original timestamps and source links |
| Outlook email/calendar | `src/capture/outlook.py` | Source-specific email/event identity; mail and calendar remain different channels |
| Browser | `src/capture/browser.py` | Browser history acquisition and canonical URL filtering; a history visit is not a complete page capture |
| YouTube | `src/capture/youtube.py` | Video identity and transcript availability; blocked or missing transcripts are reported |
| Quick Desktop | `src/capture/qd.py` and `qd_*` | Typed upstream records, feed and chat provenance; retired document/graph writers are not active sources |
| Crawlee/bookmarks | Named scripts in `scripts/` | Scraped page evidence and explicit failed fetches; credentials and web availability are independent dependencies |
| Manual/MCP/HTTP | `src/mcp_server.py`, `src/capture_api.py` | Explicit caller-provided evidence; endpoint availability is not observed capture volume |

Quick Desktop, Amazon Quick, Amazon Q Developer, Kiro and Codex are distinct source identities. Do not collapse them because a source description or older document used the names interchangeably.

## Common flow

`src/capture/framework.py` supplies preflight → collection/window → render/identity validation → ingestion → outcome. Adapters keep their source-specific retrieval and provenance. `render.py` owns underscore-only metadata headers and HTML conversion; `window.py` owns zoneinfo-aware windows; `filters.py` owns the URL exclusion vocabulary.

`processed` counts stored work. Dry runs report `planned` and keep processed at zero; collection failures remain failures even if some records succeeded. Exit codes must preserve partial/total failures. Empty source identities never reach embeddings or storage. A repeated URL in a batch is handled once.

Growing sessions refresh a stable raw parent and retain previous children as superseded evidence. Source replacement prepares embeddings before the transaction. Existing complete data survives failure. The CLI parser must not use a UUID session filename as a project; unknown project is null.

The native IDE format is qualified with synthetic fixtures. Finding a session directory is evidence of format presence, not proof of complete historic coverage. Historical `.chat` data remains distinct from native sessions until identity/parity is demonstrated.

## Codex semantic capture

Raw tasks and turns are preserved independently from the paid semantic pass. New turns use bounded batches with bounded previous-segment context. Oversize turns remain verbatim and receive an explicit report, not truncation. Validated semantic output is cached before embeddings/persistence. A storage retry must not buy the same inference again.

Contract failures and infrastructure failures are distinct. New arrivals do not reset a failed batch. One run-wide spend tracker covers the semantic pass; exhaustion skips later semantics while continuing eligible raw capture. Task-derived memories may share a task URL, so a global parent source-URL unique index would destroy valid records.

## Retired paths and maintenance

The two-phase staging hop is retired from schedules. `scripts/maintenance/drain_staged_chats.py` remains an explicit migration utility for staged leftovers. Obsolete placeholder canaries, dead importers and unused graph writers are removed after callers are retargeted. `quick_desktop_doc` is historical vocabulary, not a newly enabled source.

Deduplication and rechunking are separate resumable maintenance programs. Their dry-run manifests enumerate identity, references, row versions and prospective embedding work before any mutation. Identical text is insufficient to merge independent projects. See [Operations](OPERATIONS.md) for application gates.
