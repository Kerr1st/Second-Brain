# 06 — Live data quality: what `memory_bank` actually contains

**Scope.** Read-only measurement of the live `memory_bank` database (PostgreSQL 17.10, 2,139 MB, 124,280 `memories` rows on 2026-09-24 06:25 PDT) against what `src/`, `migrations/` and `docs/` assume. Companion to `docs/AUDIT-2026-09-23.md` §0, §3 and `docs/audit-2026-09-23/04-data-layer-search-mcp.md`, which it verifies, corrects and extends. Only `SELECT`, `\d`, `pg_stat_*`, `pg_indexes`, `pg_settings` were run via `psql -h 127.0.0.1 -U memory_bank`. No MCP tool was called (they increment `access_count`). Per-run cost was reconstructed from `logs/llm_metrics/*.jsonl` because the database does not store it (§4). Personal content is never quoted; only structure and counts.

**Severity.** S1 = the corpus the system exists to serve is silently wrong, unreachable or being destroyed. S2 = a stated contract is unenforced or a signal is misleading. S3 = waste, drift, cosmetic.

**Counts:** S1 × 6, S2 × 12, S3 × 9.

**Headline corrections to the prior audit.** (a) The "199 MB never embedded" figure is ~90 % parent rows that are *designed* to carry no embedding; real embedding truncation is **19 MB in 661 chunks** (§3). (b) The much larger problem the prior audit did not see: **62,384 of 67,757 `kiro_ide_chat` chunks (92 %) are byte-identical to another chunk** — 261 MB of duplicated, individually embedded text (§1.5). (c) The `kiro_ide_chat` channel — 59 % of all rows — has **captured nothing since 2026-05-17** (§2.4). (d) Relationship decay expires **99 % of edges before they are 90 days old** because `last_accessed_at` is NULL on every fresh row (§5.1).

---

## 1. Integrity

### 1.1 Chunk ↔ parent — structurally sound
```sql
SELECT count(*) total, count(parent_id) chunks FROM memories;                       -- 124280 | 75370
SELECT count(*) FROM memories c LEFT JOIN memories p ON p.id=c.parent_id
 WHERE c.parent_id IS NOT NULL AND p.id IS NULL;                                       -- 0 (FK holds)
SELECT count(*) FROM memories c JOIN memories p ON p.id=c.parent_id WHERE p.parent_id IS NOT NULL; -- 0 (no nesting)
SELECT c.status, p.status, count(*) FROM memories c JOIN memories p ON p.id=c.parent_id GROUP BY 1,2; -- active/active 75370
```
No orphan chunks, no chunk under a superseded parent, no nesting. `source_type` agrees between chunk and parent in all 75,370 cases. `source_url` differs in 113 cases — all `codex_task` segments, where the chunk carries `…#segment-N` by design (`src/capture/codex.py:984`). Every other channel gives chunks the parent's URL. **S3 — two URL conventions for chunks.** Fix: pick one (the `#fragment` form is the more useful) and document it in `INTERFACES.md`.

### 1.2 Chunk coverage of parents — complete
```sql
WITH c AS (SELECT parent_id, sum(length(content)) clen FROM memories WHERE parent_id IS NOT NULL GROUP BY 1)
SELECT p.source_type, round(avg(c.clen::numeric/length(p.content)),3) FROM c JOIN memories p ON p.id=c.parent_id GROUP BY 1;
```
Every chunked channel averages 0.999–1.015 coverage; only 4 parents (2 IDE, 1 article, 1 slack) have chunks summing to < 90 % of the parent. The chunker does not lose text; it only fails to split it (§3).

### 1.3 Half-written parents — S1 (confirms prior §3 prediction, now measured)
```sql
WITH pc AS (SELECT DISTINCT parent_id FROM memories WHERE parent_id IS NOT NULL)
SELECT m.source_type, count(*) FROM memories m LEFT JOIN pc ON pc.parent_id=m.id
 WHERE m.parent_id IS NULL AND pc.parent_id IS NULL AND m.embedding IS NULL
   AND m.source_type IN ('youtube','quick_desktop_chat','kiro_cli_chat','article','slack') GROUP BY 1;
--  youtube 8 | quick_desktop_chat 10
```
18 documents (YouTube 2026-03-09 … 2026-09-13; QD chats 2026-06-24 … 2026-09-03; 471–29,778 chars) have a parent row, zero chunks and no embedding. `ingest_content` committed the parent, then the chunk/embedding step failed, and because `source_url` now exists every subsequent run skips them ("already captured"). They are permanently invisible to vector search and to `memory_recent_activity`'s chunk counts. Fix: one transaction per document in `db.ingest_content` (the `capture/codex.py` pattern), plus a one-shot repair that re-chunks the 18.

### 1.4 Leaf rows with no embedding at all — S2
```sql
SELECT source_type, type, count(*) FROM memories m WHERE embedding IS NULL AND parent_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM memories c WHERE c.parent_id=m.id) GROUP BY 1,2 ORDER BY 3 DESC;
```
| source_type | type | rows |
|---|---|---|
| quick_desktop_people | source | 588 |
| quick_desktop_terminology | research | 138 |
| quick_desktop_decision | decision | 136 |
| quick_desktop_source | source | 64 |
| quick_desktop_procedure | insight | 57 |
| quick_desktop_tool_strategy | insight | 28 |
| quick_desktop_preference | insight | 25 |
| quick_desktop_anti_pattern | insight | 17 |
| quick_desktop / quick_desktop_profile | source | 16 |
| youtube / quick_desktop_chat (§1.3) | source | 18 |

**1,087 leaf rows (8,877 NULL embeddings total, of which 7,790 are parents by design)** are reachable only by full-text search. All `quick_desktop_*` fact rows imported 2026-04-21 … 2026-09-05 by `scripts/migrate/migrate_quick_desktop.py` carry `embedding = NULL`; `CAPTURE-COMPONENTS.md` says "new embeddings now supplied by local wrapper" — nothing in the live data shows that ever ran. The 136 `quick_desktop_decision` rows are `type='decision'` — exactly the type the reranker boosts (`TYPE_BOOST_TYPES`) — yet they can never enter the vector leg. Fix: a batch embed for the 1,069 QD rows (≈ 640 KB of text, negligible Titan cost) and a `NOT NULL`-when-leaf check in the write path.

### 1.5 Duplicate content — S1 (the dedup left the biggest duplication untouched)
```sql
WITH g AS (SELECT md5(content) h, min(length(content)) len, count(*) n FROM memories GROUP BY 1 HAVING count(*)>1)
SELECT count(*) groups, sum(n-1) redundant_rows, pg_size_pretty(sum((n-1)*len)::bigint) FROM g;
--  6929 | 70441 | 340 MB          (total corpus text: 652 MB → 52 % is an exact duplicate of another row)
```
By source_type (all rows, exact `md5(content)` match):

| source_type | dup groups | redundant rows | of which parent==its only chunk | cross-parent chunk dups | within-parent chunk dups |
|---|---|---|---|---|---|
| kiro_ide_chat | 2,642 | **63,700** | 32 | **62,384** | 11,828 |
| quick_desktop_doc | 2,614 | 3,568 | — (no chunks) | — | — |
| kiro_cli_chat | 584 | 1,343 | 27 | 1,250 | 12 |
| article | 616 | 847 | 242 | 491 | 0 |
| youtube | 103 | 540 | 101 | 438 | 0 |
| slack | 185 | 187 | 184 | 3 | 0 |
| outlook_email | 86 | 100 | 89 | 11 | 0 |
| outlook_calendar | 55 | 55 | 55 | 0 | 0 |

Three distinct phenomena:

**(a) IDE-chat boilerplate chunks — 62,384 redundant, embedded chunks, 261 MB.**
```sql
WITH c AS (SELECT md5(content) h, length(content) len, parent_id FROM memories WHERE source_type='kiro_ide_chat' AND parent_id IS NOT NULL)
SELECT count(*) n, min(len), count(DISTINCT parent_id) dp FROM c GROUP BY h HAVING count(*)>1 ORDER BY n DESC LIMIT 4;
--  1630 | 3856 | 380      ← same 3.9 KB chunk 1,630× inside 380 chats (≈4× per chat)
--  1400 | 2553 | 225
--  1122 | 11177 | 1122    ← same 11 KB chunk once in each of 1,122 different chats
--  1050 | 2621 | 236
```
Group-size profile: 138 groups of 100–924 copies (28,126 redundant rows, 105 MB), 4 groups of 1,050–1,630 copies (5,198 rows, 25 MB). Every one of these has an embedding (`embedded = n` in all top groups) — 62k Titan calls and ~250 MB of TOAST + a proportional share of the 425 MB HNSW index spent on identical vectors. Consequence for retrieval: any query near one of these vectors gets the same text back N times from N parents; `search.py`'s per-parent dedup cannot help because the parents differ. This is the IDE exporter re-emitting repeated context (steering/system/tool blocks) per turn and `chunk_by_section` faithfully chunking it; `ide_chat.py` "strips system prompts, tool messages, and boilerplate" per its docstring — the data says it does not.

**(b) Same document under many URLs.** IDE: 794 groups, 2,080 *top-level* parents with identical content and 2,080 distinct `chat://` URLs (1,286 redundant parents plus all their chunks) — the same conversation stored under multiple ids. QD docs: 2,614 groups / 3,568 redundant rows / 6,182 distinct `qd-doc://` URLs (1.5 MB; 49 groups are < 50 chars, i.e. empty-ish chunks).

**(c) Article re-ingestion after the 2026-09-22 fix — S2.**
```sql
SELECT created_at::date, count(*) FROM memories WHERE parent_id IS NULL AND source_type='article'
  AND md5(content) IN (SELECT md5(content) FROM memories WHERE parent_id IS NULL AND source_type='article' GROUP BY 1 HAVING count(*)>1)
 GROUP BY 1 ORDER BY 1 DESC LIMIT 3;   -- 2026-09-23: 83 | 2026-09-22: 1 | 2026-09-20: 7
```
83 article parents created on **2026-09-23** duplicate content already present (71 of them twins of 2026-05-31 rows) under **different hosts** (0 of 83 match after scheme/query/slash normalisation). Content lengths 2–60 KB, so not paywall boilerplate. One article URL exists 10× with identical content (2 chunks each; 7 copies from the 2026-09-20 03:00 ingest, i.e. one run inserted the same URL seven times). The 6,493-row cleanup on 09-22 did not touch these, and the URL-keyed dedup cannot: identity here must be content-keyed (`md5(content)` or a normalised-body hash) in addition to URL.

Fix, in order: (1) a `content_hash` column with a unique partial index on `(source_type, content_hash) WHERE parent_id IS NULL`; (2) strip repeated context blocks in `ide_chat.py` *before* chunking, then delete-and-re-chunk the 5,493 IDE parents; (3) content-keyed skip in `ingest_batch`.

### 1.6 Duplicate `source_url` within a source_type — S3 / by design
```sql
SELECT source_type, count(*), sum(n-1) FROM (SELECT source_type, source_url, count(*) n FROM memories
  WHERE parent_id IS NULL AND source_url IS NOT NULL GROUP BY 1,2 HAVING count(*)>1) g GROUP BY 1;
-- distilled_agent_task 24/87 | distilled_chat 10/10 | article 1/9 | kiro_cli_chat 3/3 | youtube 1/1
```
`distilled_agent_task` legitimately writes N memories per Codex task with the parent task's URL (distinguished by `metadata.record_kind`). 26 URLs are shared *across* source_types (`codex_task` ↔ `distilled_agent_task`). Any URL-existence dedup applied generically ("does this `source_url` exist?") is therefore wrong for two channels; `get_processed_source_urls(source_type)` must always be scoped, and `capture_api` (no dedup at all) must not be allowed to write these types.

### 1.7 NULL / empty fields
```sql
SELECT count(*) FILTER (WHERE btrim(content)='') , count(*) FILTER (WHERE btrim(title)='') FROM memories;  -- 0 | 6 (all article/source)
SELECT source_type, count(*) FROM memories WHERE source_url IS NULL GROUP BY 1;   -- NULL source_type 88 | kiro_cli_chat 4
```
- **88 rows have `source_type IS NULL`** — every one is dream-cycle output (`metadata.dream_cycle = true`, types insight/connection/synthesis/question). `memory_recent_activity` reports them as `generated:<type>`, but `memory_list(source_type=…)` can never select them and the `memories_source_type_idx` cannot serve them. **S2.** Fix: write `source_type='dream_cycle'` (and add it to the vocabulary).
- 4 `kiro_cli_chat` rows with NULL `source_url`, top-level, embedded, `type` research/insight, `metadata = {depth_score}` only, created 2026-04-22 … 2026-09-22: agent-created notes via `memory_create` that borrowed a capture channel's `source_type`. **S3** — evidence that `source_type` is free text at the MCP boundary (prior §5).
- `summary` is populated in **0** rows; `encoding_context` in 258; `project` in 2,679 (2.2 %); `confidence` is 1.0 for 95 % and never read by rerank. `summary` is a dead column selected on every read. **S3.**

## 2. Taxonomy

### 2.1 Live distributions
```sql
SELECT type, count(*) FROM memories GROUP BY 1 ORDER BY 2 DESC;
```
`source` 118,977 · `insight` 2,763 · `decision` 2,337 · `research` 139 · `synthesis` 27 · `connection` 20 · **`correction_episode` 16** · `question` 1. `status`: `active` 124,273, `superseded` 7 (`archived` 0). `mem_class`: `episodic` 44,127 · `procedural` 39,004 · NULL 35,131 (all top-level: every `quick_desktop_doc`, `distilled_chat`, `quick_desktop_feed`, …) · `semantic` 5,877 · **`source` 141**. `metadata->>'record_kind'`: `task_memory` 113 (`distilled_agent_task`), `topic_segment` 113 and `captured_task` 28 (`codex_task`) — exactly the three literals in `src/capture/codex.py:495,937,1009`; no drift there. `relation_type`: `related_to` 23,015 · `derived_from` 125 · `extends` 118 · `supports` 99 · `contradicts` 30 · `superseded_by` 7 (`inspires` 0).

### 2.2 Vocabulary reconciliation
| Live value | rows | `validation.py` | `classify.py` | `mcp_server` docstrings (:74 / :131 / :362) | `CAPTURE-COMPONENTS.md` | Verdict |
|---|---|---|---|---|---|---|
| type `correction_episode` | 16 | ✗ | ✗ | — | — | **S2** live, unreachable via MCP type filter; written by `codex.py:999` |
| type `idea`,`priority`,`project` | 0 | ✓ | ✓ | — | — | declared, never used |
| type `schema` | 0 | filter-only | ✗ | advertised in `memory_search` | — | feature cannot fire (prior §4.2, still true) |
| status `archived` | 0 | ✓ | — | — | — | declared, never used |
| mem_class `source` | 141 | — | ✗ (classifier can't emit it) | — | — | **S3** written by `codex.py:551`; `MEM_CLASS_BOOST.get` silently → 0.0 |
| mem_class NULL | 35,131 | — | — | — | — | 28 % of corpus unclassified; rerank treats as episodic |
| source_type NULL | 88 | — | — | listed as `generated:<type>` | — | **S2** (§1.7) |
| source_type `quick_desktop_*` (13 variants) | 36,720 | — | — | only "`quick_desktop_*`" | 6 listed under other names | 13 live spellings incl. bare `quick_desktop` (10 rows) |
| source_type `distilled_chat`, `distilled_agent_task`, `weekly_digest`, `codex_task`, `browser_history`, `outlook_*`, `slack` | 9,256 | — | — | partly | `codex`/`outlook` said to have **no collector** | doc is false (prior §8) |
| docstring `pdf`, `course`, `notes`, `manual`, `cli_chat` | **0** | — | — | ✓ (:74, :131) | — | **S3** five advertised values with zero rows; `cli_chat` is a misspelling of `kiro_cli_chat` |
| code literals `browser_extension`, `email`, `notion_page`, `claude_chat`, `chatgpt_chat`, `codex_desktop`, `slack_cache`, `local` | 0 | — | — | — | — | declared in `capture_api`/migrators, never written |

Net: **27 live `source_type` values, zero vocabulary anywhere**; 5 values advertised to agents do not exist; 8 values in code have never been written. `classify.SEMANTIC_TYPES` omits `research` (139 live rows → classified episodic/procedural) — `quick_desktop_terminology` is `type='research'` and shows `mem_class` NULL because the migrator bypasses `classify_memory` entirely. Fix: one `SOURCE_TYPES` frozenset in `validation.py` seeded from this table, `optional_one_of` on the three MCP tools, `classify.py` importing from `validation.py`, and a CHECK constraint after `correction_episode` and `dream_cycle` are admitted.

### 2.3 Channel liveness from `created_at`
```sql
SELECT source_type, count(*), min(created_at)::date, max(created_at)::date FROM memories GROUP BY 1 ORDER BY 2 DESC;
```
| channel | rows | first | last |
|---|---|---|---|
| kiro_ide_chat | 73,250 | **2026-05-17** | **2026-05-17** |
| quick_desktop_doc | 32,624 | 2026-05-17 | 2026-06-04 |
| quick_desktop_session_event | 704 | 2026-05-17 | 2026-05-17 |
| quick_desktop_people/terminology/source/profile | 796 | 2026-04-21 | ≤ 2026-06-03 |
| quick_desktop_feed | 1,803 | 2026-05-17 | 2026-09-16 |
| quick_desktop_chat | 519 | 2026-05-17 | 2026-09-03 |
| kiro_cli_chat, article, youtube, slack, outlook_*, codex_task, distilled_*, browser_history | — | — | 2026-09-23/24 ✓ |

### 2.4 The IDE-chat channel is dead — S1
All 73,250 `kiro_ide_chat` rows were written between **12:19 and 12:49 on 2026-05-17** (one backfill; `metadata.date` spans 2025-06-11 … 2026-05-17). Since then: every one of the 31 `logs/chat-extract-*.log` files ends `IDE=0`; `staging/ingested/` holds 22 files, all `cli_*`; and the two directories `src/parsers/ide_chat.py:23-24` scans contain 12,447 `.chat` files of which **0 were modified after 2026-05-17** (`find … -newermt 2026-05-18 | wc -l → 0`). Kiro IDE stopped writing there; `~/.kiro/sessions/` has 53 non-`cli` session directories with 1,027 files modified since 2026-09-20 that no parser reads. The largest channel in the corpus has silently captured nothing for 130 days, with a green log line every night. Fix: repoint/extend `find_chat_files()` (likely `~/.kiro/sessions/<workspace>/`), and make `chat_extract` exit non-zero (or at least warn in `verify_liveness.sh`) when a channel that has ever produced output yields 0 for > N days.

### 2.5 QD facts/graph — stale since June; entities churned every night — S3
`quick_desktop_doc` last row 2026-06-04, `quick_desktop_people` 2026-04-26, `entities`/`entity_edges` `max(created_at)` 2026-09-16 — yet `pg_stat_user_tables.n_tup_upd` on `entities` is **648,652** for 29,642 rows (≈22 updates/row): `qd_sync` re-upserts every entity each run without changing anything, which is why `entities` has 1,944 dead tuples (6.2 %) and gets autoanalyzed nightly. `entities.embedding` is `vector(1536)` (a different space from `memories`' 1024) and is **NULL in all 29,642 rows**, while `idx_entities_embedding` (ivfflat, 808 kB) indexes it. Nothing in `src/` reads any of the three KG tables (prior §1.6 confirmed); `memory_entities` links 1,995 memories (1.6 %) and all 1,995 resolve.

## 3. Embedding truncation — prior figures reproduced, then corrected

```sql
SELECT count(*) FILTER (WHERE length(content)>25000) rows_gt_25k,
       count(*) FILTER (WHERE length(content)>25000 AND embedding IS NOT NULL) embedded_gt_25k,
       pg_size_pretty(sum(length(content)-25000) FILTER (WHERE length(content)>25000)::bigint),
       pg_size_pretty(sum(length(content)-25000) FILTER (WHERE length(content)>25000 AND embedding IS NOT NULL)::bigint),
       count(*) FILTER (WHERE length(content)>4000) FROM memories;
--  4834 | 667 | 199 MB | 19 MB | 21411
```
The prior audit's 4,826 rows / 199 MB / 21,352 reproduce (drift = 8 days of ingest). But **4,167 of the 4,834 rows are parents that have no embedding by design** and are fully represented by their chunks (§1.2). The embedding that is actually truncated is **19 MB in 661 chunks + 6 `quick_desktop_session_event` leaves**. The S1 in `AUDIT-2026-09-23 §0` should be re-stated as: *"19 MB of chunk text past the 25k cut is unembedded, concentrated in 661 chunks; and `chunk_by_section` violates its 4,000 limit in 19 % of chunks"* — still a real defect, an order of magnitude smaller.

Per source_type, share of **leaf** text (rows that are not parents) that no vector represents = truncated tails + leaf rows with NULL embedding:

| source_type | leaf text | truncated tail | leaf rows w/o embedding | % leaf text unembedded |
|---|---|---|---|---|
| kiro_ide_chat | 272 MB | 8.4 MB | — | 3.0 |
| kiro_cli_chat | 22 MB | 7.4 MB | — | **32.1** |
| youtube | 9.3 MB | 2.2 MB | 83 kB | **24.7** |
| quick_desktop_chat | 2.1 MB | 519 kB | 54 kB | 27.5 |
| quick_desktop_session_event | 619 kB | 103 kB | — | 16.6 |
| article | 8.1 MB | 699 kB | — | 8.6 |
| slack | 1.9 MB | 30 kB | — | 1.6 |
| quick_desktop_{people,terminology,source,decision,…} | 640 kB | — | 640 kB | **100** |
| all others | — | 0 | — | 0 |

Chunk length distribution (`parent_id IS NOT NULL`, n = 75,370):

| bucket | n | % |
|---|---|---|
| ≤ 1,999 | 10,380 | 13.8 |
| 2,000–3,999 | 50,652 | 67.2 |
| 4,000–4,394 (merge overshoot) | 3,000 | 4.0 |
| 4,395–7,999 | 5,274 | 7.0 |
| 8,000–24,895 | 5,403 | 7.2 |
| **25,403–360,823 (embedding truncated)** | **661** | 0.9 |

Chunks are **not** "≤ 4,000": 14,338 (19 %) exceed it, and the worst channel structurally is YouTube (no blank lines → 540 parents, `max_chunks = 2`, median chunk 5,283, 83 chunks > 25k). `rerank_weights.py:40`'s "up to 4,000 chars" and `docs/CAPTURE-COMPONENTS.md`'s "merge small sections" both describe intent, not data. Fix as prior §0 (hard split), re-chunk the 661 + the YouTube/CLI parents; embed the 1,069 QD leaves (§1.4).

## 4. Dream-cycle ledger

### 4.1 Stats columns vs reality
```sql
WITH cand AS (SELECT run_id, count(*) n, count(*) FILTER (WHERE final_verdict='ACCEPTED') acc, count(created_memory_id) mem FROM dream_cycle_candidates GROUP BY 1)
SELECT r.started_at, r.candidates_generated g, r.candidates_accepted a, r.candidates_rejected j, c.n, c.acc, c.mem
  FROM dream_cycle_runs r LEFT JOIN cand c ON c.run_id=r.id ORDER BY 1;
```
54 runs (33 scheduled, 21 user_triggered), 51 completed, **3 never completed** (2026-03-18 18:01:30 / 18:01:40 / 18:16:27 — NULL `completed_at`, NULL stats, 0 candidates). 170 candidate rows: 114 ACCEPTED (81 CREATE, 26 UPDATE, 7 SUPERSEDE), 56 REJECTED; every ACCEPTED has a `created_memory_id` that resolves (0 dangling); all 88 `metadata.dream_cycle` memories link back to a candidate. No DEFERRED ever occurred (`candidates_deferred` is dead, as prior §7 says).

**The zeroed-stats lie is real, twice — S1 (confirms prior §2):**

| run started | recorded g/a/j | actual candidates | actual accepted | memories created |
|---|---|---|---|---|
| 2026-05-17 18:33:46 | 0 / 0 / 0 | 3 | 3 | 3 |
| 2026-09-18 07:32:51 | 0 / 0 / 0 | **6** | **4** | **4** |

Plus one **partial ledger**: 2026-09-14 14:57:30 recorded `generated = 12, accepted = 0, rejected = 3` but only 3 candidate rows exist — 9 generated candidates were never judged or stored, and `g ≠ a + j` (the only run where that holds). Every tier-1 metric in `dream_cycle_db.get_run_metrics` (acceptance rate, trend, cost efficiency) is computed from these columns.

### 4.2 Cost is not in the database at all — S2
`\d dream_cycle_runs` has **no `cost_usd` column**, and `stats` JSONB is non-NULL on only **3 of 54 runs** (the post-skim ones, 2026-09-20 → 23) with 19 keys, of which the only money key is `skim_cost_usd`. Thinker/panel/explorer spend lives solely in `SpendTracker` (in-memory) and `logs/llm_metrics/<run_id>.jsonl`. The $9 cap therefore cannot be audited from the DB; `docs/MODEL-BACKENDS.md` and the tier-1 "cost efficiency" metric (candidates ÷ *estimated* invocations) both imply otherwise. Fix: persist `SpendTracker.total_usd` and a per-role breakdown into `stats` in `complete_run` — on every exit path, including the aborted ones.

### 4.3 Per-stage cost, reconstructed from `logs/llm_metrics` (`real_input_tokens`/`real_output_tokens` × `cost.estimate_cost_usd`, standard tier; `usage_source='real'` on 100 % of rows)

| run | started | total | explorer | thinker (calls, $/call) | panel (4 seats) | grounding+revision | skim | candidates judged | **$/candidate judged** |
|---|---|---|---|---|---|---|---|---|---|
| 47fffa94 | 09-17 20:09 | $3.55 | 0.20 | 1.09 (4, **0.27**) | 2.26 | — | — | 12 | 0.19 |
| 3a1263a6 | 09-18 07:32 | $4.20 | 0.29 | 1.23 (5, **0.25**) | 1.87 | 0.81 | — | 7 | **0.38** |
| 720f03b3 | 09-21 02:16 | $4.24 | 0.22 | 0.94 (4, **0.23**) | 1.86 | 1.22 | 0.001 | 8 | **0.39** |
| 7ab94704 | 09-23 02:00 | $5.08 | 0.21 | 0.94 (4, **0.23**) | 1.98 | 1.39 | 0.56* | 8 | **0.42** |

\* standard-tier price; `memory_skim.cost_usd` for the same run sums to **$0.28** because the skim is billed at the Flex tier (`cost.py:71`) — the two figures are consistent, but anyone re-pricing from the logs without the `service_tier` gets 2×.

Calibration of `orchestrator.py:126-127`: `EST_THINKER_CALL_USD = 0.30` vs measured $0.23–0.27 — **well calibrated (≈ +15 %)**. `EST_JUDGE_CANDIDATE_USD = 0.70` vs measured $0.38–0.42 — **~1.7× too high**; the code comment cites "~$0.66" from run 3a1263a6, which today's price table reproduces as $0.38. No run has yet hit `slices_skipped_for_budget`/`candidates_unjudged_for_budget` (both NULL on all 3 stats rows), so the over-estimate has cost nothing so far; it will start skipping slices roughly $2.50 before the cap actually binds. **S3.** All four Bedrock-era runs finished at $3.55–5.08, under the $9 cap and above the $5 documented in `MODEL-BACKENDS.md:62` for one of them. Run 720f03b3 took **379 minutes** (skim: 46 docs, 45 reused, 1 skimmed — the time went elsewhere; only `duration_s` per call is logged, no stage timings persisted).

## 5. Relationships

### 5.1 Decay expires edges before they are 90 days old — S1
```sql
SELECT count(*) FILTER (WHERE expired_at::date = created_at::date) same_day,
       count(*) FILTER (WHERE expired_at < created_at + interval '90 days') before_90d,
       count(*) FILTER (WHERE expired_at IS NOT NULL) expired, count(*) FROM memory_relationships;
--  17604 | 22269 | 22461 | 23394
```
`expire_stale_relationships` (`dream_cycle_db.py:846-858`) expires any edge where **no endpoint has `last_accessed_at` within 90 days — and NULL counts as never**. Every freshly ingested memory has `last_accessed_at = NULL`, so the auto-links `ingest_content` creates are retired by the next dream-cycle run: 17,604 edges (78 %) expired on the calendar day they were created (16,762 on 2026-05-17 alone), **22,269 of 22,461 expirations (99 %) happened before the edge was 90 days old**. The docstring's "90+ days without access" is false for the corpus; `created_at` is never consulted. Live edges by type: `related_to` 715 / 23,015; `derived_from` 37 / 125; `extends` 94 / 118; `supports` 70 / 99; `contradicts` 11 / 30; `superseded_by` 6 / 7. Per origin channel, `kiro_ide_chat → related_to` is **16,479 edges, 0 live**; `youtube` 1,590 / 3 live; `quick_desktop_chat` 339 / 0 live. Fix: `AND GREATEST(mr.created_at, …) < now() - 90 days` and treat NULL `last_accessed_at` as `created_at`.

### 5.2 Codex provenance edges are being expired against their own contract — S1
```sql
SELECT count(*) FILTER (WHERE expired_at IS NOT NULL), count(*) FROM memory_relationships WHERE relation_type='derived_from';  -- 88 | 125
```
`origin/main:migrations/012_agent_task_capture.sql` (never applied to `memory_bank`, see §9) states "Exact Provenance is evidence, not a temporal association … future expiry is prohibited" and adds `CHECK (relation_type <> 'derived_from' OR expired_at IS NULL)`. The ported `src/capture/codex.py` creates these edges; the unported migration means 88 of 125 `derived_from` edges (79 of the 113 `distilled_agent_task → codex_task` ones) already carry `expired_at`, so `express`, which filters `expired_at IS NULL`, cannot trace an agent-task memory to its source.

### 5.3 Self-loops and other shape defects — S2
```sql
SELECT relation_type, count(*), count(*) FILTER (WHERE expired_at IS NULL) FROM memory_relationships WHERE source_id=target_id GROUP BY 1;
-- extends 9 (6 live) | supports 1 (1 live)
```
10 edges relate a memory to itself (dream-cycle `store_accepted` writing the candidate's own id as target). 559 pairs exist in both directions with the same type (A→B and B→A `related_to` from bidirectional auto-linking; harmless but doubles `memory_graph` output). 31 live edges touch a non-active memory. 19,957 of 23,394 edges (85 %) point from a parent to a **chunk of another document** (auto-link matches chunk embeddings), so `memory_graph` neighbours are fragments, never documents. All 7 `superseded` memories have exactly one `superseded_by` edge — that path is correct.

## 6. Skim (`memory_skim`)

```sql
SELECT count(*), count(DISTINCT memory_id), count(DISTINCT run_id), sum(cost_usd), count(cluster_id), count(DISTINCT cluster_id) FROM memory_skim;
--  399 | 399 | 3 | 0.379094 | 52 | 17
```
| run | `stats.skim_docs` | `skim_skimmed` | `skim_reused` | `skim_failed` | `stats.skim_cost_usd` | `memory_skim` rows | Σ `cost_usd` |
|---|---|---|---|---|---|---|---|
| 05171b48 (09-20) | 164 | 164 | 0 | 0 | 0.19063 | **87** | **0.0983** |
| 720f03b3 (09-21) | 46 | 1 | 45 | 0 | 0.0007 | 1 | 0.0007 |
| 7ab94704 (09-23) | 312 | 311 | 0 | 1 | 0.280044 | 311 | 0.280055 |

- **77 paid skims from the 09-20 run are gone** (164 skimmed, 87 stored; costs scale exactly 47 %). `memory_skim.memory_id` is `ON DELETE CASCADE`; the 09-22 dedup deleted 6,493 memories and took their skim records with it. Nothing records that a skim was paid for and discarded. **S3** (money already spent; but any future cleanup will repeat it silently).
- JSON shape is uniform: 11 keys on all 399 rows; `quotes` live inside `decisions[]` — 198 decisions, **5 with `quote = null`**, while `Σ skim.quotes_unverified = 6` across 6 rows (one unverified quote has no null decision to match — minor bookkeeping mismatch, **S3**). 310 of 399 skims (78 %) have zero decisions; mean salience 2.7, novelty 1.6.
- Coverage vs eligible arrivals (`parent_id IS NULL AND created_at ≥ 2026-09-13`): slack 112/248, distilled_chat 81/211, quick_desktop_feed **0/192**, article 16/186, distilled_agent_task 85/113, outlook_email 48/100, outlook_calendar 16/55, kiro_cli_chat 13/30, codex_task 23/28, youtube 0/2, NULL-source 0/17. The skim spends 42 % of its rows (166/399) on `distilled_chat` + `distilled_agent_task` — **derivative outputs of other model passes**, i.e. the run's own upstream inferences — while `quick_desktop_feed` (192 fresh arrivals) is never skimmed. **S2**: the arrivals filter should exclude derived `source_type`s (the same drift prior §5 found across the four arrivals queries).
- `cluster_id` set on 52 rows (13 %) in 17 clusters; 87 % of skims are singletons, so `cluster_window` rarely groups anything.

## 7. Access reinforcement

```sql
SELECT count(*), count(*) FILTER (WHERE access_count>0), count(*) FILTER (WHERE last_accessed_at IS NOT NULL), sum(access_count) FROM memories;
--  124280 | 2142 | 2760 | 4712
SELECT count(*) FROM memories m WHERE last_accessed_at IS NOT NULL AND EXISTS (SELECT 1 FROM dream_cycle_runs r
  WHERE m.last_accessed_at BETWEEN r.started_at AND coalesce(r.completed_at, r.started_at + interval '3 hours'));   -- 2077
```
1.7 % of rows have ever been accessed; **75 % of all `last_accessed_at` timestamps fall inside a dream-cycle run window**. The 22 distinct access dates are the run dates (03-18, 05-17, 05-30, 06-05/06, 06-13…17, 09-14…23) plus three single-digit days. Top accessed rows by class: `kiro_cli_chat` sources (424 rows, 1,421 hits), `distilled_chat` decisions/insights (549 rows, 896 hits), dream-cycle's own outputs (71 rows, 733 hits — 10 hits/row, 5× the corpus mean). 618 rows carry `last_accessed_at = 2026-03-09` with `access_count = 0` (a backfill that set one column and not the other). **S2**: the `0.03·log1p(access)·spacing` rerank term (`rerank_weights.py`) is fed almost entirely by the dream cycle reading what the dream cycle previously wrote or sliced — a self-reinforcing loop, not a user signal. Fix: attribute accesses (`accessed_by` = mcp | dream_cycle | express) and feed the reranker only the MCP/user share, or drop the term until it has a real source.

## 8. Size, bloat, autovacuum

```sql
SELECT pg_size_pretty(pg_database_size('memory_bank'));   -- 2139 MB
```
| table | total | heap | indexes | TOAST | live | dead | dead % | last autovacuum | last autoanalyze |
|---|---|---|---|---|---|---|---|---|---|
| memories | 2,094 MB | 134 MB | 551 MB | **1,409 MB** | 124,280 | 127 | 0.1 | 2026-06-12 | **2026-06-12** (manual VACUUM 2026-09-22) |
| entities | 14 MB | 8.7 MB | 6 MB | — | 29,642 | 1,944 | 6.2 | 2026-06-13 | 2026-09-24 |
| entity_edges | 11 MB | 5 MB | 6 MB | — | 45,776 | 0 | 0 | 2026-06-12 | 2026-06-12 |
| memory_relationships | 7.6 MB | 4.5 MB | 3 MB | — | 23,394 | 402 | 1.7 | 2026-09-22 | 2026-09-22 |
| dream_cycle_candidates | 2 MB | — | — | 1.7 MB | 170 | 0 | — | never | 2026-09-14 |
| memory_skim | 856 kB | — | — | — | 399 | 0 | — | 2026-09-23 | 2026-09-23 |

- 67 % of the database is `memories` TOAST (embeddings + long content). With §1.5's 62k duplicate embedded chunks at ~4 kB/vector, roughly **250 MB of TOAST and a proportional slice of the 425 MB HNSW index are duplicates**. Removing them is the single largest size win available.
- Bloat is low (`n_dead_tup` ≤ 6 % everywhere); the 09-22 manual VACUUM after the dedup was appropriate. `autovacuum = on`, default thresholds (`scale_factor` 0.2 / 0.1), `maintenance_work_mem` 64 MB, `shared_buffers` 128 MB, `work_mem` 4 MB — Homebrew defaults untuned for a 2 GB vector workload; HNSW builds/reindexes (`reindex_embedding.sh`) will spill at 64 MB. **S3.**
- Index scan counts since postmaster start 2026-08-24 (`pg_stat_database.stats_reset` NULL): unused = `idx_entities_name` 1.9 MB, `idx_entities_embedding` 808 kB (all-NULL column), `idx_memories_encoding_context` 504 kB, `idx_dream_cycle_runs_run_type`, `idx_dream_cycle_candidates_final_verdict`, both `express_feedback` indexes (table has 0 rows) — 3.2 MB total. `memories_metadata_idx` (13 MB) has **7** scans, not 0 as the prior audit reported; still not worth 13 MB.
- `memories_source_type_idx` (1 MB) has 12,023 scans reading **508,746,994** tuples — ~42k rows per scan, i.e. the `WHERE source_type = …` predicates return whole channels and then filter; a composite `(source_type, created_at DESC)` or `(source_type) WHERE parent_id IS NULL` would cut that by ~30×. **S3.**
- `entities`: 648,652 updates on 29,642 rows (§2.5) — nightly no-op upserts.

## 9. The third database: `second_brain_codex_test`

```sql
-- from postgres: datname | size | owner            -- second_brain_codex_test | 12 MB | memory_bank
SELECT (pg_stat_file('base/'||oid||'/PG_VERSION')).modification FROM pg_database WHERE datname='second_brain_codex_test'; -- 2026-09-14 07:59:48-07
```
Created **2026-09-14 07:59** (memory_bank/memory_bank_test both date from the 2026-06-12 native-Postgres migration). It has **0 memories**, 10 tables (no `memory_skim`, no `dream_cycle_slices`; an extra **`context_receipts`**), and a 15-row `schema_migrations` ledger whose 012–015 are `012_agent_task_capture`, `013_context_governance`, `014_local_embedding_space`, `015_enforce_active_embedding_space` — the **`origin/main` migration lineage**, which shares version numbers 012–014 with the local `012_dream_cycle_slices` / `013_evaluator_details` / `014_memory_skim` but has different content. It carries `hnsw.ef_search = 200` because **`origin/main`'s `009_hnsw_recall.sql` already uses `EXECUTE format('ALTER DATABASE %I …', current_database())`** (lines 31-32) — the exact fix prior §1.2 recommends for the local copy, which still hardcodes `memory_bank`. The fix exists in the repo's own remote and was not ported. It is a leftover scratch DB from testing the Codex port (`5386c2b`/`b6d4484`). **S2 — migration-number fork**: `migrate.sh` keys on filename, so a future `brazil`-style "apply everything ≥ 012" against either lineage will half-apply or collide; and two of the four origin migrations encode contracts the ported code relies on (§5.2's `derived_from` CHECK; unique indexes `uq_memories_codex_captured_task` on `(source_url) WHERE codex captured_task` and `uq_memories_codex_topic_segment_index` on `(parent_id, segment_index)`) that live `memory_bank` lacks — today codex.py's `FOR UPDATE` discipline keeps both invariants (0 duplicate captured_task URLs, 0 duplicate `(parent_id, segment_index)`), but nothing in the DB enforces them.

## 10. Recommended cleanup / migration list (ordered by payoff ÷ risk)

1. **Fix `expire_stale_relationships`** to respect `created_at` and treat NULL `last_accessed_at` as fresh; then un-expire edges expired before 90 days of age whose endpoints are still active (22,269 rows) — or accept the loss and re-run auto-link. Add the origin `derived_from` CHECK. (§5.1–5.2, S1)
2. **Repoint IDE-chat discovery** and add a "channel produced 0 for N days" liveness alarm. (§2.4, S1)
3. **Content-hash dedup**: add `content_hash`, unique partial index on top-level `(source_type, content_hash)`, strip repeated context in `ide_chat.py`, then delete-and-re-chunk the 5,493 IDE parents (removes ~62k chunks, ~250 MB TOAST, shrinks HNSW). Remove the 1,286 duplicate IDE parents and 3,568 QD-doc duplicates. (§1.5, S1)
4. **Make `ingest_content` transactional**; repair the 18 half-written parents; embed the 1,069 QD leaves. (§1.3–1.4, S1/S2)
5. **Persist run cost** (`total_usd`, per-role) in `complete_run` on every exit path; fix the two zeroed runs and the 09-14 partial ledger with a one-off UPDATE from candidate counts; delete or complete the 3 never-completed 03-18 runs; recalibrate `EST_JUDGE_CANDIDATE_USD` to ~0.45. (§4, S1/S2/S3)
6. **Vocabulary**: `SOURCE_TYPES` frozenset (27 live values + `dream_cycle`), `MEMORY_TYPES += correction_episode`, `classify` imports from `validation`, backfill `source_type='dream_cycle'` on the 88 NULLs and `mem_class` on the 35,131 NULLs, then CHECK constraints. Fix docstring lists at `mcp_server.py:74,131,362`. (§2, S2)
7. **Skim arrivals filter**: exclude `distilled_*`/`weekly_digest`/dream-cycle rows; include `quick_desktop_feed`. Record "skim paid but memory later deleted" or drop the CASCADE. (§6, S2/S3)
8. **Access attribution** (`accessed_by`) before the rerank term is trusted; fix the 618 rows with `last_accessed_at` but `access_count = 0`. (§7, S2)
9. **Drop**: `idx_entities_embedding`, `idx_entities_name`, `idx_memories_encoding_context`, `idx_dream_cycle_runs_run_type`, `idx_dream_cycle_candidates_final_verdict`, `memories_metadata_idx` (13 MB, 7 scans), the `summary` column (0 rows), `candidates_deferred`; add `(source_type, created_at DESC)` and `parent_id` indexes. Stop `qd_sync` re-upserting unchanged entities. (§8, S3)
10. **`DROP DATABASE second_brain_codex_test`** (after confirming nothing points at it — repo grep finds no reference outside the audits), and renumber/merge the origin 012–015 lineage explicitly so the two histories cannot collide. (§9, S2)
11. Tune `maintenance_work_mem` (≥ 512 MB for HNSW builds) and `shared_buffers`; schedule `ANALYZE memories` after bulk jobs. (§8, S3)

## Could not verify

- **Why IDE chats stopped landing in `globalStorage`** — I confirmed 0 files modified there since 2026-05-17 and 1,027 recent files under `~/.kiro/sessions/<hash>/`, but did not parse those to prove they are the IDE's new chat store.
- **The 77 missing 09-20 skim rows** are attributed to the 09-22 dedup's `ON DELETE CASCADE` by arithmetic (47 % of rows and 47 % of cost gone); no deletion log exists to confirm.
- **The 83 post-fix article duplicates (2026-09-23)**: content is identical and hosts differ; whether Crawlee's source map mis-attributed bodies or the bookmarks genuinely pointed at mirrors requires reading `~/Work/Tools/Crawlee` output, outside the DB.
- **Per-run dollar figures** are reconstructions from logged real token counts × today's `cost.py` price table at the standard tier; served `service_tier` and the price table at run time were not logged, so ±20 % is possible. Runs before 2026-09-17 logged no token usage at all (Kiro era) and are unpriceable.
- **`idx_scan = 0`** is "since 2026-08-24 postmaster start" (stats never reset), not "never".
- **`memories.last_analyze`** (manual) is not exposed in the version of `pg_stat_user_tables` I queried; `n_mod_since_analyze = 1,453` suggests the 09-22 VACUUM included ANALYZE, unconfirmed.
- Whether `ide_chat.py`'s boilerplate stripping ever worked on the 2026-05-17 backfill input (the duplicated chunks could also be legitimately repeated user-pasted context); I did not read chunk content.
- No MCP tool, job, test, or write of any kind was executed; `memory_graph`'s inbound-edge bug and the `ALTER DATABASE` hazard remain confirmed by data shape only, as in the prior audit.
