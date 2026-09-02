# Capture — Component Architecture

**Status: reference for the component build-out.** Captures the target structure so each
piece can be built, tested, scheduled, and replaced **independently**. Grounded in the
2026-06-08 capture/ingest audit. YouTube (`src/capture/youtube.py`) is the first component
and the reference implementation for the contract below.

The maintained ownership boundary, entry points, tests, and operational status
are indexed in the canonical [Capture component contract](components/capture.md).

Codex Desktop is the reference implementation for agent-task capture. Its schema specification,
single capture interface, simplification deletion list, and verification path are recorded in
[`CODEX-TASK-CAPTURE-BUILD-PLAN.md`](CODEX-TASK-CAPTURE-BUILD-PLAN.md).

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

### Agentic-assistant capture standard

Agentic assistants have a stronger shared contract than general documents. Kiro, Amazon Quick,
Quick Desktop, Amazon Q Developer, Claude Code, Codex, and future assistants are separate source
integrations; each normalizes its source task into:

- an evidence-based Task Ownership classification of `user-owned`, `delegated`, or `unknown`;
- a stable source task identity and source timestamps;
- an ordered sequence of complete **Agent Turns**;
- one user prompt and one visible agent outcome per turn; and
- source provenance such as assistant, workspace, and source-defined project when available.

Only User-Owned Tasks continue to eligibility. Delegated Tasks are excluded as separate capture
sources, and Unknown-Ownership Tasks are skipped rather than presumed user-owned; both exclusions
are reported. System and developer instructions, hidden reasoning, tool calls and results,
delegated-agent activity within the retained task, and progress commentary are also excluded. An
integration may use its own eligibility policy, but a later source update refreshes the same
Captured Task rather than creating a duplicate.

Task Ownership behavior is universal, while evidence is source-specific. Codex uses
`thread_source`, `thread_spawn_edges`, `agent_path`, and structured `source` metadata. Later
connectors must document their native evidence and retain native-shape fixtures before activation.
Kiro's current transcript-content heuristic is not yet sufficient proof of ownership for the
shared standard. See the canonical [Task Ownership contract](components/capture.md#task-ownership-contract)
and [ADR 0008](adr/0008-classify-task-ownership-before-capture.md).

Native task identity is always namespaced by integration. Amazon Quick is web-based and Quick
Desktop is a desktop application; both call their task units **sessions**, but they store them in
different locations and require separate connectors. An Amazon Quick Session and a Quick Desktop
Session never identify the same Agent Task merely because their native session IDs match.

The common behavior above does not imply a source-neutral runtime interface. Codex remains the
reference implementation. Shared runtime code and shared test helpers are extracted only after a
second connector demonstrates which seam is genuinely common; until then, connector-specific code
implements the common observable contract directly.

#### Exact provenance

For agent-task capture, Exact Provenance is one readable traceability chain:

```text
Decision, Insight, or Correction Episode
  → supporting Agent Turn IDs
  → Topic Segment
  → Captured Task
  → native Agent Task identity
```

The Captured Task preserves the source integration, native task identity and title, source
timestamps, and available native project, workspace, and repository context. The Topic Segment
preserves its ordered Agent Turn IDs. A derived decision, insight, or Correction Episode links to
its Topic Segment and identifies the supporting turns. A Correction Episode cites both the visible
outcome containing the misalignment and the correcting user prompt. Because the Captured Task and
Topic Segment retain the original prompts and visible outcomes, the chain reaches the exact source
evidence.

When an Agent Turn includes an image or file, its Attachment Descriptor remains associated with
that turn as an audit trail. It records that the attachment existed and preserves available source
metadata, but it does not copy, interpret, embed, or retain the attachment bytes. Exact Provenance
therefore reaches the attachment record, not the attachment's contents.

Model and prompt versions, content or provenance hashes, timing, usage, and other processing
telemetry are diagnostics—not Exact Provenance—and are not Codex v1 requirements.

#### Project attribution policy

Source location and semantic subject are separate. A connector always preserves its native project
or grouping, workspace history, and repository context as provenance. Those values describe where
an Agent Task ran; they do not automatically populate `memories.project`. Topic Segments and
task-distilled memories receive a semantic project only when the task content clearly establishes
that subject. Otherwise, `project` remains unset. See
[`ADR 0004`](adr/0004-separate-source-provenance-from-semantic-project.md).
Codex Build 1 does not yet classify semantic project from content, so all of its Captured Tasks,
Topic Segments, and derived memories leave `project` unset while retaining source provenance.

#### Real-data testing policy

Real agent-task history may be used throughout connector, capture, segmentation, distillation,
retrieval, and evaluation tests, and raw real-data fixtures may be committed to Git. Second Brain
does not require synthetic or redacted source substitutes. Persistence tests still use an isolated
test database so test mutations cannot damage the live memory store. Automated tests may inject
deterministic model and embedding boundaries while retaining real Task evidence; the bounded live
Proof Gate exercises the configured services. See
[`ADR 0005`](adr/0005-use-real-agent-task-data-throughout-testing.md).

#### Monotonic capture policy

The shared spine treats each Captured Task as an append-only evidence record:

- Once an Agent Turn is stored, its prompt, visible outcome, and position are immutable.
- A refresh appends only unseen complete turns, in their newly observed order.
- Changed, missing, or reordered previously captured turns are **Source Drift**. Codex v1 ignores
  that drift without extra state and does not rewrite, delete, or move established evidence.
- Source Drift without unseen complete turns is a no-op: it does not write capture state or trigger
  the Task Semantic Pass.
- Source-native title, archive, project, workspace, Git, and timestamp metadata may refresh without
  a new turn. That provenance-only refresh preserves stored turns and does not trigger the Task
  Semantic Pass.
- A genuine correction must arrive as a new Agent Turn so both the original evidence and the
  correction remain available to later semantic processing.

For example, if Second Brain has captured `A B C` and later observes `A C changed-B D E`, the
durable task becomes `A B C D E`. Semantic processing reconsiders the previous active tail with
the appended turns—in this example, `C D E`—without reopening the full task history. This policy
governs source observations that can be read. The Codex proof gate does not track an entirely
unavailable source: captured evidence remains intact, and normal recurring inventory can rediscover
the source if it returns. Availability states and persistent disappearance retries are deferred
until operational evidence demonstrates a need. See
[`ADR 0003`](adr/0003-accumulate-captured-task-history-monotonically.md).

The shared spine groups adjacent complete turns into **Topic Segments**. A proposed segment
must have a distinct purpose, be coherent under one title, and have independent value for
search or distillation. Brief asides and uncertain boundaries merge into the surrounding
segment. Every qualifying segment is stored and searchable even if Task Distillation produces
no decision, insight, or Correction Episode from it.

On refresh, completed segments remain stable by default. Only the prior tail segment and new
turns are reconsidered, so a resumed topic can continue without reorganizing the entire task.
After source capture commits, the same scheduled run invokes one **Task Semantic Pass** over that
tail. The single model response returns the Topic Segments and zero or more supported decisions,
insights, or Correction Episodes for each segment. A segment consists of a title plus its original
Agent Turns; it does not require a separate summary. Topic Segmentation and Task Distillation remain
useful concepts, but they are not separate model calls in Codex v1.

The combined semantic result is stored atomically. If the model call, validation, or semantic write
fails, the Captured Task remains successful and no partial segments or derived memories are kept.
The Semantic Processing Cursor stays on the last successfully processed Agent Turn, and the next
capture invocation retries the whole unprocessed tail. Once hourly scheduling is separately
approved, that invocation will normally be the next hourly run. Codex v1 has no separate
segmentation or distillation status, cursor, or stage-specific retry.

The Dream Cycle is not part of this same-run workflow; on its separate schedule, it later creates
evaluator-gated, higher-order synthesis across memories, including contradiction discovery. Build 1
stores Correction Episodes as searchable evidence but does not create Steering Candidates, active
rules, or automatic context. Build 2 is specified in the Codex build plan and remains unimplemented
in this build; its prerequisite real-data Correction Episode Proof Gate passed on 2026-07-23.
Codex v1 does not run a separate
contradiction planner or create immediate `contradicts` links. See
[`ADR 0002`](adr/0002-standardize-agent-task-capture.md) and
[`ADR 0006`](adr/0006-combine-task-segmentation-and-distillation.md), plus
[`ADR 0007`](adr/0007-capture-correction-episodes-before-steering.md).

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
| **amazon_quick** | Amazon Quick web sessions | Future connector | Separate from Quick Desktop; source access and storage discovery remain integration-specific work. |
| **amazon_q_developer** | Amazon Q Developer sessions | Future connector | Separate from Kiro; source access and storage discovery remain integration-specific work. |
| **web** | Articles / bookmarks | `crawlee_ingest.py`, `scrape_bookmarks.py`, `parsers/crawlee.py` (+ external Node) | Bring in-repo (drop external Crawlee dependency), like youtube. |
| **quick_desktop** | Quick Desktop docs / feed / sessions / 9 micro-cats | `migrate/{ingest_doc_chunks,ingest_eventlog,migrate_quick_desktop}.py`, `ingest_qd_chats.py`, `qd_sync.sh`, `qd_profile.py` | Separate from Amazon Quick. Consolidate. **Bug:** micro-cats (~1,140 recs) created with **no embeddings** → invisible to vector search. |
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
  sessions distilled (459/5,493). Target: generalize to any `source_type` + backfill; agentic
  assistant sources distill stable Topic Segments rather than whole transcripts or fixed chunks.

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
