# Capture components

Source adapters acquire evidence; common ingestion stores it atomically. A capture report is evidence about that attempt, not proof that every source surface was observed. Code: `src/capture/`, `src/parsers/`, `src/ingest.py`; schedules in [scheduling/README.md](../scheduling/README.md).

| Surface | Entry point (job) | Identity and evidence boundary |
|---|---|---|
| Codex tasks | `src/capture/codex.py`, `src/capture/sources/codex_turns.py` (`codex-capture`, every 6 h) | User prompt plus visible final outcome, stable task/turn identity (`RolloutTurnAssembler`, drop counters, `rollout_fingerprint`); excludes hidden reasoning, tool output and delegated activity. The paid semantic pass is a separate metered role with its own run threshold |
| Kiro IDE and CLI chats | `src/capture/chats.py`, `src/parsers/kiro_sessions.py`, `src/parsers/{ide_chat,cli_chat}.py` (`chat-capture`, 02:30) | Native IDE `~/.kiro/sessions/<workspace>/<session>/session.json` + `messages.jsonl`, user and assistant `Say` only; legacy `.chat` exports and CLI JSONL; bounded 50 sessions per run per channel, newest first; incomplete sessions reported, not failed; a session UUID is never a project |
| Slack | `src/capture/slack.py` (`capture-slack`, 01:00) | Conversation-day identity, original timestamps and links; honours `Retry-After` within a bounded budget; a day still lost after the budget is a failure |
| Outlook mail and calendar | `src/capture/outlook.py` (`capture-outlook`, 00:45) | Per-message / per-event identity; mail and calendar are different channels; an item without a timestamp is one failed item, not an aborted run |
| Browser | `src/capture/browser.py` (`capture-browser`, 01:15) | Chrome history from a SQLite backup copy, canonical URL filtering (`filters.py`); a history visit is not a page capture; missing History is a failure |
| YouTube | `src/capture/youtube.py` (`youtube`, 01:30) | Video identity and transcript availability (yt-dlp); blocked or missing transcripts are reported; a non-zero enumeration with partial entries is `partial_enumeration` |
| Quick Desktop | `src/capture/qd.py` with stages `facts, tags, feed, chats` (`qd-sync`, hourly) | Typed upstream records (`qd://memory/<id>`, `qd://entity/<id>`, `qd-feed://`, `qd-chat://`); refreshes the eligible snapshot every run, no cursor file; `qd://` parents that left the selection are retired as superseded (`metadata.capture_retired`, reactivated on return); private/incognito chat sessions are never selected; the document channel (`qd_docs.py`) and the Slack knowledge-graph writer are retired — the `graph` stage was removed in R7 together with its tables |
| Crawlee and bookmarks | `src/capture/crawlee.py`, `scripts/crawlee_ingest.py` (`crawlee-ingest`, 03:00); `scripts/scrape_bookmarks.py` (`bookmarks`, Saturday) | Index-verified scraped pages; at most `--max-unindexed` (25) never-indexed internal documents per run; unresolved index rows are reported, not failed |
| Manual / MCP / HTTP | `src/mcp_server.py` (`memory_create`, `memory_learn`), `src/capture_api.py` (`capture-api`, service) | Explicit caller-provided evidence; endpoint availability is not observed capture volume |

Quick Desktop, Amazon Quick, Amazon Q Developer, Kiro and Codex are distinct source identities. Do not collapse them because an older document used the names interchangeably.

## Common flow

`src/capture/framework.py` supplies preflight → collection/window → render and identity validation → ingestion → outcome (`run_channel` for documents, `run_records` for typed Quick Desktop records). `render.py` owns the one-line underscore-only metadata headers and HTML-to-text conversion; `window.py` owns zoneinfo-aware local-day windows including 23- and 25-hour days; `filters.py` owns the URL exclusion vocabulary and the indexed-URL inventory.

`processed` counts stored work. Dry runs report `planned` and classify each record `new` / `changed` / `unchanged` by reading the stored row; processed stays zero. A failed item is counted in `CaptureBatch.failed`, logged with its identity, and makes `main()` exit non-zero (exit 1; 3/4 for auth/transport in the MCP-backed channels) while the rest of the batch continues. Empty source identities never reach embeddings or storage; a repeated URL in a batch is handled once. Every channel runs under the job runner, so a failure is also a durable status and a liveness finding ([OPERATIONS.md](OPERATIONS.md)).

Growing sessions refresh a stable raw parent and keep previous children as superseded evidence when they are cited. Source replacement prepares embeddings before the transaction; existing complete data survives failure. Metadata patches merge keys (tags are merged, not overwritten); a refreshed row keeps `created_at`, which liveness uses as the capture-time column.

The native IDE format is qualified with synthetic fixtures (`tests/fixtures/`); finding a session directory proves format presence, not complete historic coverage. Historical `.chat` data remains distinct from native sessions and is never refreshed.

## Codex semantic capture

Raw tasks and turns are preserved independently of the paid semantic pass (`codex_semantic` role, Bedrock). New turns are processed in frozen bounded batches with bounded previous-segment context; oversize turns stay verbatim with an explicit report; validated semantic output is cached before embeddings and persistence so a storage retry never buys the same inference again. Contract failures and infrastructure failures are distinct; new arrivals do not revive an abandoned batch; one run-wide spend tracker covers the pass and exhaustion skips later semantics while raw capture continues. Open findings: F-086 (legacy turn identity wins over the native one), F-094 (private/incognito Quick Desktop chats are filtered only by message count and deletion), F-090 (Quick Desktop rows that fell out of the eligible selection stay active) — tracked in [PLAN-refactor.md](PLAN-refactor.md).

## Retired paths and maintenance

The two-phase staging hop (`chat_extract.py` → `staging/chats` → `ingest_staged.sh`), the one-shot `scripts/migrate/` migrators, `codex-canary`, the Quick Desktop document channel and the Slack knowledge-graph writer are gone; `scripts/maintenance/drain_staged_chats.py` remains for staged leftovers. `quick_desktop_doc` is historical vocabulary, not a source.

Deduplication and rechunking are separate resumable maintenance programs (`scripts/maintenance/dedup_corpus.py`, `rechunk_and_embed.py`, `prepare_release.py`); their dry-run manifests enumerate identity, references, row versions and prospective embedding work before any mutation, and application is the pending R6 step ([RELEASES.md](RELEASES.md)). Identical text is insufficient to merge independent projects.
