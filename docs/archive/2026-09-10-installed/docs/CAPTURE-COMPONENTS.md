# Capture, ingestion, and distillation — original implementation

Source-reviewed 2026-09-10. [Architecture](ARCHITECTURE.md) describes the downstream contracts. This document lists actual capture paths; the aspirational common connector/runner design from June is [historical](archive/2026-09-10/docs/CAPTURE-COMPONENTS.md).

## Source coverage

| Surface | Actual entrypoint/input | Output/state | Dependency and boundary |
|---|---|---|---|
| Kiro CLI | `src/parsers/cli_chat.py`: `~/.kiro/sessions/cli/*.jsonl`; `scripts/chat_extract.py` | Filtered user/assistant text → `staging/chats/cli_*.md` | Reads recorded sessions; tool results stripped, short/low-content sessions filtered |
| Kiro IDE | `src/parsers/ide_chat.py`: current/legacy Kiro globalStorage `.chat` paths | Filtered text+project hints → staged Markdown | Does not inspect every UI surface; project hints can be incomplete |
| Staged chats | `scripts/batch_ingest_staged.py`, scheduled by `jobs/ingest_staged.sh` | `chat://<id>` parent/chunks → moves staged file to `staging/ingested` | URL-existence skip; no source revision check; `ingest_chats.py` uses a different legacy URL scheme |
| QD chats | `scripts/ingest_qd_chats.py`: active-profile `sessions/sessions.db` | `qd-chat://<id>` → raw parent/chunks | Minimum 2 messages; truncates large tool content to 2,000 characters; an existing session URL prevents recapture after append |
| QD extracted facts/decisions | `scripts/migrate/migrate_quick_desktop.py` | `qd://memory/<id>` / `qd://entity/<id>`, graph rows; ID watermarks | Already-distilled input, direct memory writer; new embeddings now supplied by local wrapper |
| QD documents | `scripts/migrate/ingest_doc_chunks.py`: `search_content` joined to `file_chunks` | One memory per `qd-doc://<node>`; `file_id`, chunk index, source file/folder metadata | Local docs only, Downloads excluded. Existing chunk IDs skipped; no full-file parent/revision completeness contract |
| QD feed/engagement | `scripts/migrate/ingest_eventlog.py`: event/interactions JSONL | `qd-feed://<id>` memories plus interaction metadata | Engagement counts are source signals, not evidence of useful SB recommendations |
| QD Slack cache | `scripts/migrate/import_slack_graph.py`: newest cache channel/user files | Entity graph rows | Imports channels/users, **not Slack message bodies**; graph not consumed by core search |
| Browser bookmarks/web | `scripts/scrape_bookmarks.py` → external `~/Work/Tools/Crawlee`; `src/parsers/crawlee.py`, `scripts/crawlee_ingest.py` | Knowledge-store/output Markdown → article/YouTube parent/chunks | Chrome bookmark/cookie access and external Node tool; not general browser activity capture |
| YouTube | `python -m src.capture.youtube` | Liked/Watch Later video transcripts → parent/chunks keyed by video URL | yt-dlp+curl_cffi+Chrome cookies; default minimum 300 seconds; missing transcript/network failures logged |
| ChatGPT / Claude / Notion exports | `scripts/migrate/migrate_chatgpt.py`, `migrate_claude.py`, `migrate_notion.py` | Export normalization via `migration_utils.py` → ingestion | Manual export importers; **not automatic live Codex or Claude Code capture** |
| Agent-created notes | MCP `memory_create` / two-step `memory_learn` | One embedded memory per create | Caller supplies evidence/context; depth guidance is a warning, not rejection |
| HTTP messages | Optional `src/capture_api.py` `/capture`, `/capture/slack`, `/capture/browser`, `/capture/email` | One embedded memory | Caller must supply payload+bearer token. Endpoint implementation is not a working inbox/browser connector |

The source tree has **no native automatic Codex conversation, Claude Code conversation, Gmail inbox, or Outlook inbox collector**. Quick Desktop may contain observations from external integrations; that upstream coverage must be audited separately. Broad “capture everything” is the purpose, not the present coverage guarantee.

## Common ingestion path

`ingest_content(content, source_type, source_url=None, project=None)` parses Markdown header fields, resolves/normalizes project, stores the full raw parent without an embedding, tries semantic/temporal auto-links, then chunks/embeds/stores children. A header `Date` is metadata, not automatically the `created_at` timestamp.

Chunks split on section/paragraph boundaries and merge small sections. A single oversized section/paragraph is not forcibly subdivided. Embedding truncation can therefore omit tail meaning while full text remains stored. Auto-linking compares parent embeddings to existing records but keeps only parent neighbors; failed linking is swallowed. The wrapper is **not atomic across parent, children, and relationships**, and direct callers do not share a universal dedup contract.

`ingest_batch` snapshots already-processed URLs once. Staged and QD importers implement additional URL/ID skips. A partial parent write can cause future runs to treat an incomplete source as already captured. Existing URLs also skip changed/resumed material. Repairing source revisions is more important than advertising idempotence as a complete guarantee.

There are several storage paths: ingestion, MCP create, HTTP capture, QD facts/docs, distillation, digest, and dream-cycle storage. Shared `store.py` / `runner.py` in the June component plan are **not implemented in this original tree**.

## Distillation

`python scripts/distill_sessions.py` selects undistilled parent records for `kiro_cli_chat` and `quick_desktop_chat` by default. Repeatable `--source-type` can select another type with suitable parent records. It makes one **Kiro** call per transcript (first 100,000 characters) to extract up to eight proposed decisions/insights. The prompt requests the cap; `valid_items` only checks type/content and does not enforce the number or source quotes.

Outputs use `source_type='distilled_chat'`, `distill://<session>#<index>`, fixed confidence 0.8, semantic class, and `metadata.distilled_from`. Successful new decisions may trigger another Kiro comparison against the most similar prior active decision; a contradiction edge does not itself supersede the prior record.

The script does not verify that a decision was the user's rather than the assistant's proposal, or validate exact quotation support. It does not store a successful empty-distillation receipt. Once any indexed item marks a session processed, later content and some partial-write failures can be skipped. `--dry-run` still makes real model calls; it prevents memory writes, not external transmission or inference cost.

`redistill_capped.py` is a manual backfill tool for sessions that hit an older cap; it has its own lock/similarity filtering and writes additional distilled records. It is not part of the normal scheduled chain. `weekly_digest.py` summarizes aggregates and decision titles; it is neither raw capture nor evidence-grounded cross-project discovery.

## Quick Desktop integration

`src/qd_profile.py` selects `profiles.json:last_active` and its data path, falling back to legacy `~/.quickwork`. Slack cache resolution selects the newest cache directory. `warn_if_stale` only warns; a valid filename does not prove the external collector is current.

`jobs/qd_sync.sh` runs **six** scripts, in order: facts/graph migration, tags/domains enrichment, feed/interactions ingestion, Slack channel/user graph, chats, document chunks. Its shell stops on nonzero subprocess status, but some Python scripts log failures and still exit zero. Facts migration advances maximum-ID watermarks across failures; older edits/reinforcements are not replayed by that new-ID filter. The state file is shared under the QD root, not namespaced by SB test installation, so never run it against a test DB with live state.

Quick Desktop's historical design describes alpha/beta Bayesian confidence and retrieval reinforcement. The importer reads `confidence`/`effective_confidence`, copies values/metadata and retrieval count, and filters by effective confidence (default 0.5). **Second Brain does not import or execute an alpha/beta update algorithm here.** Importing scores does not establish that subsequent SB feedback updates QD beliefs. Bidirectional learning is not implemented.

## Tests and capture acceptance checks

Existing tests cover CLI/IDE parsing, ingest/chunk behavior, QD mappings/tags/docs/events/engagement, Slack graph import, YouTube helpers, distillation, and migration utilities. These test contracts and many mocked boundaries, not current upstream availability or every real source format.

For each connector's end-to-end fixture, retain raw bytes/version/date, normalized text, source ID, exclusions and reasons, all expected chunks, DB IDs, and failures. Re-run the same source, append it, change its tail, and inject an embedding failure mid-ingest. Assert completeness and retry behavior, then trace the retained evidence into a delivered recommendation. See [Validation](VALIDATION.md).
