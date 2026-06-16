# Capture — Component Architecture

**Status: reference for the component build-out.** Captures the target structure so each
piece can be built, tested, scheduled, and replaced **independently**. Grounded in the
2026-06-08 capture/ingest audit. YouTube (`src/capture/youtube.py`) is the first component
and the reference implementation for the contract below.

## Why components
Today capture is the most sprawled part of the system: ~15 scripts, an external Node tool,
and **3 duplicate storage paths**. The goal is to make capture a set of small components with
clear boundaries so we can work on one without touching the others, and so the shared spine
(storage, distillation, retrieval) stays consistent across every source.

## System components (top level)
Second Brain is four components with clean boundaries (verified: the core does not import
capture):

```
CAPTURE  →  RETRIEVAL substrate  →  SYNTHESIS (dream cycle)  →  DELIVERY (Express)
```

- **Retrieval** — `db`, `embeddings`, `search` (hybrid BM25+vector+RRF + cognitive rerank).
- **Synthesis** — `dream_cycle/*` (Explorer → Thinker → 4 evaluators), consensus-gated.
- **Delivery** — `express` (`brief` / push / `memory_brief`).
- **Capture** — the subject of this doc; decomposed below.

This doc decomposes **Capture**. The same component treatment can later apply to the others.

## Capture = source connectors + a shared spine

```
                       ┌─────────── source connectors ───────────┐
   youtube  kiro_chat  web  quick_desktop        (each: enumerate + fetch)
                       └──────────────────┬──────────────────────┘
                                          ▼
                          SPINE:  store → ingest (chunk + parent/chunk + relationships)
                                          ▼
                                  distill  (raw → semantic decision/insight)   ← the value step
                                          ▼
                              memories (Postgres/pgvector)  → retrieval → synthesis
```

A **connector** owns only *"how do I reach this source and normalize it to markdown."*
Everything after that (dedup, storage, chunking, distillation, retrieval) is **shared spine**.

## The connector contract (what makes a source separately-workable)
Every source connector implements three things — nothing more:

```python
SOURCE_TYPE = "youtube"                 # 1. its label

def enumerate() -> Iterable[Item]:      # 2. list what's available (id/url, title, metadata)
    ...

def fetch(item) -> str | None:          # 3. produce markdown + metadata header, or None to skip
    ...
```

A shared **runner** then does the cross-cutting work once, for every connector:
dedup (`get_processed_source_urls(SOURCE_TYPE)`), `ingest_content(md, source_type=...)`,
stats, `--limit`/`--dry-run`, and request backoff. (Today `youtube.py` inlines its own
runner; the refactor lifts that into a shared `run(connector, ...)`.)

Markdown each connector emits uses a metadata header the ingest pipeline parses:
```
# <title>
Source: <url>
Type: <source-specific>
Engagement: <tier>        # e.g. liked | watch_later — interest signal for Phase 2
---
<body>
```

## Source connectors (status)

| Connector | Signal | Current home | State / work to bring into structure |
|---|---|---|---|
| **youtube** ✅ | Liked + Watch Later transcripts | `src/capture/youtube.py` | Done — reference impl. Refactor: split connector vs. shared runner. |
| **kiro_chat** | Kiro IDE + CLI chat sessions | `chat_extract.py`, `batch_ingest_{parallel,staged}.py`, `ingest_chats.py`, `parsers/{ide,cli}_chat.py` | Consolidate 4 scripts → one connector. IDE source idle since 2026-02-18 (resumes when editor is used). |
| **web** | Articles / bookmarks | `crawlee_ingest.py`, `scrape_bookmarks.py`, `parsers/crawlee.py` (+ external Node) | Bring in-repo (drop external Crawlee dependency), like youtube. |
| **quick_desktop** | QD docs / feed / chat / 9 micro-cats | `migrate/{ingest_doc_chunks,ingest_eventlog,migrate_quick_desktop}.py`, `ingest_qd_chats.py`, `qd_sync.sh`, `qd_profile.py` | Consolidate. **Bug:** micro-cats (~1,140 recs) created with **no embeddings** → invisible to vector search. |
| ~~capture_api~~ | Slack / browser / email (HTTP) | `src/capture_api.py` + `capture-api` launchd job + `test_capture_api.py` | **Retire** — 0 captures ever. |
| ~~migrations~~ | Claude / ChatGPT / Notion (one-time) | `migrate/migrate_{claude,chatgpt,notion}.py` | **Archive** — 0 rows present. |

## Shared spine

- **store** — the single primitive: `embed → classify → depth → create_memory`.
  *Currently triplicated* (`ingest.ingest_content`, `capture_api._store_memory`,
  `mcp_server.memory_create`) → collapse to one `store.py`; the other two call it.
- **ingest / pipeline** — metadata-header parse + section chunking + parent (unembedded) +
  embedded chunks + relationship discovery. Healthy today (`src/ingest.py`); keep
  `ingest_content`'s signature stable (6 callers).
- **distill** — raw chunks → semantic `decision`/`insight` memories. **The value-multiplier**
  (synthesis cites distilled memories, not raw chunks). Today: chat-only, and only ~8% of IDE
  sessions distilled (459/5,493). Target: generalize to any `source_type` + backfill.

## Target layout
```
src/capture/
  store.py            # the one storage primitive (all writers route here)
  pipeline.py         # chunk + parent/chunk + relationships  (today's ingest.py)
  distill.py          # raw → semantic, generalized to any source_type
  runner.py           # shared capture loop: enumerate → dedup → fetch → ingest → stats
  sources/
    youtube.py  ✅
    kiro_chat.py    web.py    quick_desktop.py
```

## Migration path (incremental — never a big-bang rewrite)
1. **Retire dead** — `capture_api` (+ job + test), one-off migrations, the `import_slack_graph`/entity-KG path. (Verify-before-cut; each its own commit.)
2. **Lift one connector at a time** onto the shared spine, newest pattern first
   (`youtube` ✅ → `kiro_chat` → `web` → `quick_desktop`), retiring its scattered scripts as it lands.
3. **Collapse the 3 store paths → 1** (`store.py`); point `mcp_server`/any HTTP writer at it.
4. **Generalize `distill`** to any source_type + backfill (the highest-leverage step for synthesis).
5. Keep the test suite green and commit per step (origin + mini).

## Status snapshot
- ✅ `youtube` connector shipped (`ac3054d`); reference contract established.
- ▢ Extract shared `runner.py` from `youtube.py`.
- ▢ Collapse the 3 store paths → `store.py`.
- ▢ Lift `kiro_chat`, `web`, `quick_desktop` connectors.
- ▢ Generalize `distill` + backfill (IDE remainder, youtube, articles).
- ▢ Retire `capture_api` + one-off migrations.
- ▢ Fix `quick_desktop` micro-category embeddings.
