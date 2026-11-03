# 07 — Quick Desktop pipeline and `scripts/migrate/` — second, deeper review

**Repo:** `/Users/kerrfir/second-brain` @ `6b8c2d2` · **Date:** 2026-09-24 (≈06:20 PDT) · **Mode:** read-only. Live `memory_bank` queried with SELECT only; Quick Desktop's sqlite stores were inspected for schema and row counts only (never content); no script, launchctl, pytest or git write was run.

**Input:** `docs/AUDIT-2026-09-23.md`, `docs/PLAN-refactor.md`, `docs/audit-2026-09-23/{02,04,05,06}*.md`. Where this review confirms or refutes those, it says so inline (✅ confirmed / ❌ refuted / ➕ new).

**Severity:** S1 = corrupts or hides data the system exists to serve, or spends / exfiltrates unmetered. S2 = a stated rule is unenforced, or a failure is invisible. S3 = duplication, dead weight, doc drift.

**Totals:** 29 findings — S1 × 4, S2 × 16, S3 × 9.

---

## 0. Headline

1. **The nightly distiller is the largest raw-corpus → Kiro/Fable path in the repo, and the prior audit missed it.** `scripts/distill_sessions.py:27,150` constructs `KiroInvoker` directly (resolver bypassed, profile ignored, `usage=None` so unmetered) and sends up to 100,000 chars of raw chat transcript per session (`:34`), plus stored decision bodies for the contradiction check (`:135`). The served model self-identifies in our logs: *"This session is on claude-fable-5.1, which is not approved for customer data/ITAR/PII. The transcript to distill contains customer account data (Boeing and Epsilon metrics, AWSentral records)…"* (`logs/distill-20260922.log`, 76 such lines across 7 daily logs). The same flagged session (`chat://d7e55229-e26`) was re-sent on 13 nights.
2. **That job has been failing 100 % since 2026-09-23 and reporting OK.** `[warn] failed to set model 'claude-sonnet-5': Method not found` → `failed=15` (09-23), `failed=18` (09-24), `memories=0`; `job_wrapper` logs `OK` because `main()` returns `None` (`distill_sessions.py:212-221`).
3. **Every row `migrate_quick_desktop.py` has ever written (1,069) has `embedding IS NULL`.** `map_memory`/`map_decision` (`:120-146`) never produce an embedding and the `create_memory` wrapper at `:26-28` only *removes* a kwarg. `docs/CAPTURE-COMPONENTS.md:13` says "new embeddings now supplied by local wrapper" — false. The QD facts/decisions/people channel is invisible to vector search.
4. **The QD "sync" is insert-only.** Nothing updates, deletes or re-scores. Measured drift: 634 of 933 mirrored QD memories are now *below* QD's own 0.5 confidence threshold; ≥ 40 were deleted upstream; ~4.6 k ghost entities and ~7.5 k ghost edges; 9,609 `quick_desktop_doc` rows whose source chunk no longer exists; 8 QD chats deleted upstream but retained; 18 of 34 surviving chats captured mid-conversation and frozen.

---

## 1. Writer map (deliverable 1)

Schedule: `scheduling/com.second-brain.qd-sync.plist` → `scripts/jobs/qd_sync.sh` every 3600 s (`StartInterval`, `RunAtLoad=false`, `Nice=10`), six scripts in fixed order (`qd_sync.sh:24-29`). Observed: 21 runs on 09-23, ~15 s each when idle. Distill: `com.second-brain.distill.plist` → `job_wrapper.sh scripts/distill_sessions.py` daily 05:00.

| `source_type` | rows (live) | last row | writer | step | `source_url` scheme | dedup key | embedded? |
|---|---|---|---|---|---|---|---|
| `quick_desktop_people` | 588 | 2026-04-26 | `migrate_quick_desktop.py` `map_memory` | qd_sync #1 | `qd://memory/<qd_id>` | `source_url LIKE 'qd://%'` set + id cursor `last_memory_id` | **no (0 %)** |
| `quick_desktop_terminology` | 138 | 06-03 | same | #1 | same | same | **no** |
| `quick_desktop_source` | 64 | 05-31 | same | #1 | same | same | **no** |
| `quick_desktop_procedure` | 57 | 06-13 | same (`PROCEDURE_MAP`) | #1 | same | same | **no** |
| `quick_desktop_tool_strategy` | 28 | 06-13 | same | #1 | same | same | **no** |
| `quick_desktop_preference` | 25 | 06-02 | same | #1 | same | same | **no** |
| `quick_desktop_anti_pattern` | 17 | 07-23 | same | #1 | same | same | **no** |
| `quick_desktop_profile` | 6 | 04-24 | same | #1 | same | same | **no** |
| `quick_desktop` (uncategorised) | 10 | 06-30 | same (`DEFAULT_MAP`) | #1 | same | same | **no** |
| `quick_desktop_decision` | 136 | 09-05 | same, `map_decision` | #1 | `qd://entity/<qd_entity_id>` | same set + cursor `last_entity_id` | **no** |
| `quick_desktop_feed` | 1,803 | 09-16 | `ingest_eventlog.py` | #3 | `qd-feed://<event_id>` | `get_processed_source_urls('quick_desktop_feed')` | yes (1 whole-doc) |
| `quick_desktop_chat` | 519 (122 parents) | 09-03 | `scripts/ingest_qd_chats.py` via `ingest_content` | #5 | `qd-chat://<session_id>` | `get_processed_source_urls('quick_desktop_chat')` | chunks yes, parent no (by design) |
| `quick_desktop_doc` | 32,624 | 06-04 (retired) | `ingest_doc_chunks.py` | #6 | `qd-doc://<node_id>` | `get_processed_source_urls('quick_desktop_doc')` | yes (1 per chunk) |
| `quick_desktop_session_event` | 704 | 05-17 | **none** — writer deleted in `7fc1723` (2026-06-02) | — | `qd-session-event://…` | — | yes |
| `distilled_chat` | 4,691 | 09-22 | `distill_sessions.py` (nightly) and `redistill_capped.py` (manual) | distill 05:00 | `distill://<parent source_url>#<n>` | `get_processed_source_urls('distilled_chat')` split on last `#` | yes |
| `distilled_agent_task` | 113 | 09-24 | `src/capture/codex.py:50` (out of track) | codex-capture 6 h | `<codex identity>` | codex txn | yes |
| `claude_chat` / `chatgpt_chat` / `notion_page` | **0** | — | `migrate_{claude,chatgpt,notion}.py` via `migration_utils.run_migration` | manual | `claude://<uuid>`, `chatgpt://<id>`, `notion://<slug>[#row]` | `get_processed_source_urls(type)` | via `ingest_content` |

Non-`memories` writers in the chain: step #2 `enrich_qd_tags.py` (UPDATE `tags`, `metadata.qd_domains` on `qd://memory/*` rows; 933 lookups/hour, 0 changes for months), step #3 second half `enrich_with_interactions` (UPDATE `metadata.interactions` on `qd-feed://*`), step #4 `import_slack_graph.py` (UPSERT `entities` only — Channel/Person), step #1 phase 3 `import_kg_graph` (UPSERT `entities`, INSERT `entity_edges`, INSERT `memory_entities`).

**❌ Refuted — prior audit 02 ("No writer of quick_desktop_synth located despite a monitor test asserting it").** `quick_desktop_synth` is not a `source_type`; it is a liveness *label* whose predicate is `source_type LIKE 'quick_desktop_%' AND NOT IN ('quick_desktop_doc','quick_desktop_session_event')` (`verify_liveness.sh:66`). Its writers are the three memory writers above (steps #1, #3, #5). The monitor test seeds rows matching the predicate (`tests/test_monitor_jobs.py:281-286`).

**✅ Confirmed — prior audit 05:** `qd_sync.sh` has no wrapper, no notification, `set -euo pipefail` (`:5`); `logs/qd-sync-launchd.log` is 0 bytes (mtime 08-24). **❌ Partially refuted — 05 "own log, no prune":** `qd-sync-YYYYMMDD.log` files *are* pruned, but only as a side effect of `job_wrapper.sh:46` running for *other* jobs (`find logs/ -name '*.log' -mtime +30 -delete`); oldest surviving is `qd-sync-20260824.log`.

---

## 2. The knowledge graph — migration 008 (deliverable 2)

**Verdict: half-built feature that has decayed into dead weight with a live write load.** Not "keep the tables, drop two indexes" (`PLAN-refactor.md:34`) — the tables cost more than their indexes.

Measured (live):

| table | rows | last insert | `n_tup_upd` | readers in `src/` |
|---|---|---|---|---|
| `entities` | 29,642 | 2026-09-16 | **648,260** (`updated_at` max = today) | none |
| `entity_edges` | 45,776 | 2026-09-16 | 0 | none |
| `memory_entities` | 1,995 | 2026-09-02 | 0 | none |

- **Who writes it.** `migrate_quick_desktop.py:import_kg_graph` (`:159-262`, hourly, but only when QD has new entity ids — zero since 09-16) **and** `import_slack_graph.py:run_import` (`:114-155`, hourly, unconditionally). ❌ Refutes prior audit 04:65 "written only by `migrate_quick_desktop.py`".
- **Is it still running hourly.** Yes: `import_slack_graph` upserts 317 channels + 75 persons *every hour* (`logs/qd-sync-20260924.log`: `channels_upserted=317 persons_upserted=75`), each with `updated_at = now()` (`:137,:149`). That is the 648 k updates on a 29.6 k-row table, 2,012 dead tuples, `last_autovacuum` 2026-06-13, 14 MB for data that fits in ~3 MB. All `idx_scan` counts on the KG indexes come from the writers' own `ON CONFLICT` lookups.
- **Does anything read it.** No. `grep` over `src/` for `entities|entity_edges|memory_entities` → only `backup.sh:53-60` (dumps) and `cleanup_duplicate_sources.py:15` (FK note). No MCP tool, no search leg, no dream-cycle slice.
- **Can it ever be read semantically.** No, not without a migration: `entities.embedding vector(1536)` (`migrations/008:17`) vs the project embedder `EMBEDDING_DIMENSION = 1024` (`src/embeddings.py:13`); 0 of 29,642 entities have an embedding; `idx_entities_embedding` (808 kB, ivfflat) can never be populated by this codebase.
- **Quality.** 5,562 isolated entities (no edge, no memory link); 79 self-edges; QD upstream now has 25,007 entities / 38,289 edges vs our 29,642 / 45,776 → ≈4.6 k ghost entities and ≈7.5 k ghost edges (QD deletes; we never do). 213 entity names contain `%` or `_`.
- **The bridge is the crawlee bug shape.** `memory_entities` is filled by `m.content ILIKE '%' || e.name || '%'` for `Person` names longer than 5 chars (`migrate_quick_desktop.py:245-253`): substring containment as identity, `%`/`_` in names unescaped (LIKE metacharacters), recomputed as a 933 × 5,974 cross-ILIKE whenever a new entity arrives. Separately, 1,254 of the 1,995 bridge rows link `Product`/`Organization`/`Project`/`Decision` entities (all created 2026-04-21) — the code only ever links `Person`, so those came from a writer not in the repo.

---

## 3. Idempotency of the hourly chain (deliverable 3)

**Mid-write upstream.** Both QD stores are WAL-mode sqlite (`PRAGMA journal_mode = wal`, verified). Three of four readers open `mode=ro` (`migrate_quick_desktop.py:70`, `enrich_qd_tags.py:113`, `ingest_qd_chats.py:35`); each SELECT is its own implicit read transaction, so any single statement is consistent but the phases are not mutually consistent (memories → decisions → entities → edge chunks each see a different snapshot). Consequence in `import_kg_graph`: an edge whose far end was created between the entity SELECT and the edge SELECT gets `rowcount = 0` and is counted as `edge_failed` (`:241`) — it self-heals next hour only because the far entity is then "new". `ingest_doc_chunks.py:78` opens the *private* store read-write (`sqlite3.connect(db_path)`, no `mode=ro`) from 20 threads — the one place we can leave a `-shm`/checkpoint footprint on someone else's corpus.

**A step fails.** `set -e` (`qd_sync.sh:5`) aborts the chain and the summary line never prints — ✅ confirmed. But it rarely triggers: four of the six scripts catch per-item exceptions and exit 0 (`ingest_eventlog.py:143`, `ingest_doc_chunks.py:149-151`, `ingest_qd_chats.py:131-133`, `migrate_quick_desktop.py:311-313,:337-339`). Proof this hides real outages: `logs/qd-sync-20260826.log` contains **3,648** `TokenRetrievalError` tracebacks in one day (192 events × 19 runs, "Token has expired and refresh failed"), every run ending `QD sync complete`, exit 0. The 192 feed events dated 06-23…07-21 were finally written on **2026-09-16 06:26** (`processed=192`), i.e. the eventlog channel was dark for ~3 months behind a green chain, until commit `567e97e` switched `AWS_PROFILE`.

**Cursor advances past failures.** `max_mem_id = max(max_mem_id, row["id"])` (`migrate_quick_desktop.py:296`) runs *before* the `try`; `save_sync_state` (`:355`) runs regardless of `stats["failed"]`. A QD memory that fails once (Bedrock throttle, PG blip) is never retried. Same for decisions (`:317`). ✅ Confirms `CAPTURE-COMPONENTS.md:49`'s note; adds that the **confidence filter is applied at first sight only** (`:81` `effective_confidence >= ? AND id > ?`): a QD memory below 0.5 when first seen, whose confidence later rises, has `id ≤ cursor` forever.

**Edges have no cursor.** `import_kg_graph` fetches only edges touching *new* entities (`:196`). An edge QD later adds between two already-imported entities is never imported.

**Same session re-exported / appended.** `ingest_qd_chats.py:107` skips any `qd-chat://<id>` already present; there is no `updated_at` comparison. Measured against upstream metadata: of the 34 captured sessions that still exist, **18 have `updated_at` > our capture time** (frozen partial transcripts) and **8 are soft-deleted upstream** (QD purges their messages; we keep them). The two stores also disagree on liveness: `knowledge_v1.db` mtime 2026-09-16 06:27 but `sessions.db` mtime 2026-09-22 08:29 with an active `-wal`.

**Rowid reuse.** No QD table uses `AUTOINCREMENT` (verified via `sqlite_master`); 96 k of 172 k `nodes` ids and 34 k of 59 k `entities` ids are deleted. sqlite reuses `max(rowid)+1`, so deleting the highest row and inserting a new one yields an id `≤ cursor` (missed) or a `qd-doc://<node>` URL already in `already` (a different chunk silently skipped).

**Duplicate-row vectors.**
- `quick_desktop_doc`: identity is `node` id, which QD regenerates on re-index. Live: **205** `(source_file, chunk_index)` groups with > 1 row, **3,568** surplus byte-identical rows across 2,614 groups; `file_chunks.text_hash` exists upstream and is unused. 634 chunk-0 rows for 629 distinct files.
- `distilled_chat`: **10 duplicate `distill://…#n` URLs**, all created 2026-06-03 in pairs — two `distill_sessions.py` processes overlapped (no lock; `redistill_capped.py:208-221` has one, the scheduled sibling does not). `qd_sync.sh` has the same exposure whenever a backfill step runs longer than an hour (the `already` set is snapshotted at start of each script).
- Empty-key collapse: `fetch_sessions` (`distill_sessions.py:78-82`) does not require `source_url IS NOT NULL`, so a parent with a NULL URL becomes `distill://None#0`; two such rows exist live, and every further NULL-URL parent is then "already distilled".

---

## 4. The `migrate/` name (deliverable 4)

| script | actually is | scheduled? | last real work | disposition |
|---|---|---|---|---|
| `migrate/migrate_quick_desktop.py` | recurring incremental sync (+ KG writer) | hourly #1 | 09-05 (decision) / 09-16 (entities) | **recurring** → lives with channels (`src/capture/quick_desktop/`) |
| `migrate/enrich_qd_tags.py` | recurring enrichment | hourly #2 | months (0 changes) | recurring; fold into #1 (same sqlite, same rows) |
| `migrate/ingest_eventlog.py` | recurring ingest + enrichment | hourly #3 | 09-16 (backlog flush); upstream frozen 07-21 | recurring; upstream dormant |
| `migrate/import_slack_graph.py` | recurring KG upsert | hourly #4 | never produces change; churns 392 rows/h | recurring — **stop until the KG has a reader** |
| `migrate/ingest_doc_chunks.py` | recurring ingest, **retired channel** | hourly #6 | 06-04 | recurring in name only: full 23 k-row scan + 46 log lines/h for nothing → disable in chain |
| `scripts/ingest_qd_chats.py` | recurring ingest | hourly #5 | 09-03 | recurring, mis-located (outside `migrate/`) — the one QD writer that goes through `ingest_content` |
| `migrate/migrate_claude.py` / `migrate_chatgpt.py` / `migrate_notion.py` / `migration_utils.py` | one-shot importers | no | **never** (0 rows of their types live) | **dead on arrival** — see QD-13; `scripts/maintenance/` or archive |
| `scripts/redistill_capped.py` | one-shot backfill | no | 06 (last `redistilled=true` rows) | `scripts/maintenance/` |
| `scripts/backfill_projects.py` | one-shot backfill | no | unknown; 5,564/5,830 chat parents still `project IS NULL` | Strategy 1 dead (see QD-20) → fix or archive |
| `scripts/backfill_attention_ledger.py` | one-shot backfill | no | done — 0 runs need backfill (verified) | archive |
| `jobs/dedup_ide_backfill.sh` | one-shot cleanup | no | done — 0 `ide_%` rows (verified) | archive |
| `jobs/reindex_embedding.sh` | maintenance | no | — | `scripts/maintenance/`; fix QD-24 |

Recurring = 6, one-shot = 6 (of which 3 never ran, 3 finished). Nothing in `scripts/migrate/` is a migration.

---

## 5. `distill_sessions.py` / `redistill_capped.py` — model, metering, exposure (deliverable 5)

| | `distill_sessions.py` | `redistill_capped.py` |
|---|---|---|
| backend | `KiroInvoker(model=DISTILL_MODEL)` hardcoded (`:27,:150`) | `AgentInvoker()` = `KiroInvoker` (`:29,:126`) |
| model requested | `claude-sonnet-5` via env (`:41`) | `DEFAULT_MODEL = "claude-opus-4.8"` (`kiro.py:43`) |
| model served | kiro-cli 2.22 rejects `--model` ("Method not found"); server "Auto" served **claude-fable-5.1** — self-reported in 76 log lines 09-18…09-24 | same CLI, same behaviour |
| resolver / profile | bypassed; `SECOND_BRAIN_PROFILE` has no effect | bypassed |
| metered | no — `KiroInvoker._extract_usage` returns `None, "estimate"` (`kiro.py:215`); no `SpendTracker` | no |
| raw corpus sent | full transcript `content[:100000]` (`:34,:65`) per session, ≤ 40 sessions/night, **plus** stored decision bodies `content[:1500]` × 2 for the contradiction check (`:135-136`) | transcript `[:100000]` + all existing items (`:118-122`) |
| timeout | 600 s (`:43`) | default 300 s (`kiro.py:113`) — the value that killed the nightly job |
| profiles under which corpus reaches Kiro/Fable | **all** — profile is not consulted | all |

Current state: 09-23 `sessions=0 memories=0 failed=15`, 09-24 `failed=18`, wrapper `OK` both days. The pinning test `tests/test_distill.py:153` asserts the *requested* model, which is exactly the thing the CLI ignores.

➕ The prior audit (§1) named two unmetered corpus→Kiro paths (`express.py:483`, `codex.py`); `distill_sessions.py:150` and `redistill_capped.py:126` are the third and fourth, and the third is the only one scheduled nightly.

---

## 6. Upstream staleness (deliverable 6)

Confirmed by `stat` only: `…/profiles/federate-prod/knowledge_storage/knowledge_v1.db` mtime **2026-09-16 06:27 PDT**, 602 MB, no `-wal` → 8.0 d at check time; `sessions/sessions.db` mtime **2026-09-22 08:29** with `-wal` (chats still live); `eventlog/events.jsonl` **2026-07-21**; `slack_builtin_cache/…/users.jsonl` 09-01, `channels.jsonl` 08-06; legacy `~/.quickwork/{sessions,knowledge_storage}` frozen 2026-05-17.

What the pipeline does with a stale upstream: **silent zero.** `warn_if_stale` (`qd_profile.py:49`) is called by exactly one of six scripts (`migrate_quick_desktop.py:67`), twice per run (`open_qd_db` at `:275` and `:346`), and only writes `WARNING … STALE (8.0d old)` into `qd-sync-<date>.log`. `qd_profile.py:5-6` claims "All QD sync scripts import their source paths from here" — five import paths, one checks staleness. The chain ends `QD sync complete — captures_this_cycle=0`; no notification. The daily liveness job reports `⚠ quick_upstream STALE` as *advisory* (`verify_liveness.sh:132-134`), by design. `quick_desktop_synth` is green (last row 09-16, threshold 21 d). So: an alert exists, is advisory, and does not distinguish "KG store stale but chats live" — which is the actual state.

---

## 7. Duplication with `src/ingest.py` and `src/capture/*` (deliverable 7)

| concern | canonical | re-implemented in this track |
|---|---|---|
| metadata header (`# title` / `Source-Type:` / `Source-ID:` / `Date:` / `---`) | `src/parsers/cli_chat.py:139-152` | `migration_utils.format_markdown_header:26-38`, `ingest_qd_chats.format_session_as_markdown:41-55`, `ingest_eventlog.format_event_as_markdown:60-90` |
| write path (embed → `create_memory`) | `src/ingest.ingest_content` | **4 direct writers bypass it**: `migrate_quick_desktop.py` (no embedding at all), `ingest_eventlog.py:125-141` (1 embedding, no chunking, 7 kB docs), `ingest_doc_chunks.py:130-150`, `distill_sessions.py:184-192` / `redistill_capped.py:174-179`. Only `ingest_qd_chats.py` and `migration_utils.py` use `ingest_content`. |
| chunking | `ingest.chunk_by_section` | none in track — but `ingest_eventlog` embeds the whole rendered doc, and `ingest_doc_chunks` trusts QD's chunking (unbounded `text_content`) |
| dedup | `db.get_processed_source_urls` | same call everywhere, plus `migrate_quick_desktop.py:277-283` which calls it for `'quick_desktop'` (10 rows) and then re-queries `LIKE 'qd://%'` — the first call is dead |
| content filters | `cli_chat.MIN_*` | `migration_utils.py:21-23` copies the constants; `tests/test_migration.py:245-258` exists solely to keep the copies equal |
| JSONL reader | — | `parse_events`, `parse_interactions` (`ingest_eventlog.py:33-40,:151-158`), `parse_channels`, `parse_users` (`import_slack_graph.py:33-52`) — four copies |
| sqlite open | — | four copies, three `mode=ro`, one read-write |
| progress/ETA block | — | `ingest_doc_chunks.py:166-174`, `ingest_qd_chats.py:135-145` (prior audit D8) |
| dry-run shape | — | five shapes across six scripts (prior D10); `ingest_doc_chunks` dry-run does not consult `already`, so it reports 23,015 "would process" for a fully-ingested channel |
| `test_db` fixture | `tests/conftest.py:104` | shadowed by module-local copies in `test_enrich_qd_tags.py:225`, `test_ingest_doc_chunks.py:157`, `test_ingest_eventlog.py:157`, `test_ingest_interactions.py:122` |

`scripts/migrate/` re-implements every step of the capture contract except chunking, and does so with three different write paths. Estimated ≈ 45 % of the 1,350 lines in the track are duplicates of `src/` or of each other.

---

## 8. Findings

| id | sev | where | evidence | fix |
|---|---|---|---|---|
| QD-01 | **S1** | `scripts/distill_sessions.py:27,150,34,135` | Hardcoded `KiroInvoker`; transcripts ≤ 100 k chars + stored decision bodies sent nightly; served model = claude-fable-5.1 per 76 log lines (`logs/distill-2026091[89].log`, `-2026092[0-4].log`); one customer-data session re-sent 13 nights; unmetered (`kiro.py:215`). Prior audit §1 missed this path. | Route through `default_resolver().invoker_for(<extraction role>)` with `assert_backend_supports_role`; pass `SpendTracker`; under `controlled` → Bedrock Sonnet. Never fall back to Kiro when `--model` is unhonoured (`_note_model_honored` should raise for corpus roles). Add a refusal-detector: if output mentions "not approved for … PII", mark the session `do_not_send` instead of retrying. |
| QD-02 | **S1** | `distill_sessions.py:212-221,164,171,80-90` | `main()` returns `None` → exit 0 with `failed=18/18`; wrapper writes `OK` (09-23, 09-24). Failed and empty sessions leave no marker → refetched newest-first with `limit=40`, so the same ~18 sessions are retried every night and the backlog (17 CLI + 5 QD undistilled parents) never drains. | `sys.exit(1 if stats["failed"] else 0)`; persist an empty/failed receipt (`distill://<session>#empty` or a `distill_attempts` metadata field with backoff) so the cursor moves. |
| QD-03 | **S1** | `scripts/migrate/migrate_quick_desktop.py:120-146,26-28`; `docs/CAPTURE-COMPONENTS.md:13` | 1,069 / 1,069 rows across ten `quick_desktop_*` types have `embedding IS NULL` (live). Wrapper only pops `encoding_context`; doc claims embeddings are supplied. FTS-only reachability for the QD facts/decisions/people channel. | Generate an embedding in `map_memory`/`map_decision` (or route through `ingest_content`); one-off backfill of the 1,069 rows (~$0.01 at Titan v2); delete the wrapper. Fix the doc line. |
| QD-04 | **S1** | `scripts/migrate/ingest_eventlog.py:143`; `qd_sync.sh` (no wrapper); `logs/qd-sync-20260826.log` | 3,648 swallowed `TokenRetrievalError` tracebacks in one day; every run "complete", exit 0; 192 events dated 06-23…07-21 not written until 09-16 06:26. Same shape in `ingest_doc_chunks.py:149`, `ingest_qd_chats.py:131`, `migrate_quick_desktop.py:311,337`. | Every step returns non-zero when `failed > 0`; run `qd_sync.sh` under `job_wrapper.sh`; treat an embedding/auth failure as fatal for the run (it is never per-item). |
| QD-05 | S2 | `migrate_quick_desktop.py` (insert-only design); `ingest_doc_chunks.py`; `ingest_qd_chats.py:107` | Mirror drift measured against upstream counts/metadata: 634/933 QD memories below QD's live 0.5 threshold; QD has 893 memories vs our 933 (≥ 40 deleted upstream); entities 25,007 vs 29,642; edges 38,289 vs 45,776; doc chunks 23,015 vs 32,624; 8 captured chats deleted upstream; 18 chats appended after capture and frozen. | Decide the contract: mirror (propagate `confidence`, deletes/soft-deletes, `updated_at` re-capture) or snapshot (say so, stop calling it "sync"). For chats, capture on `updated_at` change and supersede the parent. |
| QD-06 | S2 | `migrate_quick_desktop.py:296,317,355,81` | Cursor advances past per-row failures; `save_sync_state` unconditional; confidence filter applied only at first sight (`id > cursor` excludes later-promoted memories). | Advance the cursor only to the last *successful* id; drop the id cursor for memories and filter by `already` + confidence each run (893 rows is trivial). |
| QD-07 | S2 | `migrate_quick_desktop.py:196` | Edges fetched only where an endpoint is a new entity; edges later added between old entities are never imported. `edge_failed` (`:241`) also counts `ON CONFLICT DO NOTHING`, so the stat cannot distinguish a lost edge from a duplicate. | Cursor on `edges.id` (or `updated_at`), or full-set diff; separate the two counters. |
| QD-08 | S2 | `import_slack_graph.py:114-155,137,149`; `migrations/008:17` vs `src/embeddings.py:13`; live stats | KG has zero readers but 648,260 updates on 29,642 rows (hourly upsert of unchanged Slack rows), 2,012 dead tuples, no autovacuum since 06-13; `vector(1536)` column can never be filled by the 1024-dim embedder; 5,562 isolated entities, 79 self-edges. ❌ Refutes PLAN 0.4 "keep tables, drop two indexes". | Either (a) remove step #4 from the chain and freeze the KG until a reader exists, or (b) make the upsert change-detecting (`WHERE entities.properties IS DISTINCT FROM EXCLUDED.properties`) and fix the dimension. Not both tables-kept *and* hourly-churned. |
| QD-09 | S2 | `migrate_quick_desktop.py:245-253` | Bridge = `content ILIKE '%'||name||'%'`: containment as identity (the crawlee family), `%`/`_` unescaped (213 names contain them), `length > 5` as the only guard; full cross-join each time a new entity arrives. 1,254 non-Person bridge rows exist that the code cannot have written. | Word-boundary regex with escaped names or, better, link from the QD `nodes`/`edges` relations you already import instead of text matching. Document the 04-21 out-of-repo writer or delete its rows. |
| QD-10 | S2 | `qd_sync.sh:5,24-29`; `distill_sessions.py` (no lock); live duplicates | No lock/singleflight on either scheduled job; 10 duplicate `distill://…#n` rows created in pairs on 2026-06-03 prove the overlap race fires. `set -e` drops the summary line on abort (✅ prior 05). | `flock` (or the `redistill_capped.py:208` pattern) in `job_wrapper.sh`; run `qd_sync.sh` through the wrapper. |
| QD-11 | S2 | `distill_sessions.py:78-82,87,152` | `fetch_sessions` allows `source_url IS NULL` → `distill://None#n` (2 live rows, `distilled_from = null`); after the first, every NULL-URL parent is "already distilled". Empty-key collapse. | `AND source_url IS NOT NULL`; key the receipt on `memories.id`, not `source_url`. |
| QD-12 | S2 | `tests/test_enrich_qd_tags.py:225`, `test_ingest_doc_chunks.py:157`, `test_ingest_eventlog.py:157`, `test_ingest_interactions.py:122` vs `conftest.py:104-133` | Module-local `test_db` fixtures shadow the session fixture and **do not redirect `src.db.DB_CONFIG`**; the scripts under test call `src.db.get_connection()`. A full run is safe only because an alphabetically earlier test activates the session fixture; `pytest tests/test_ingest_interactions.py` alone would `UPDATE` live `qd-feed://evt_0000{1,2,3}` metadata (those rows exist in production, created 05-17). `test_ingest_is_idempotent` variants leave rows behind (no cleanup). | Delete the four local fixtures; use `conftest.test_db` + `clean_tables`. Consider a conftest guard that refuses `dbname = memory_bank` in any test. |
| QD-13 | S2 | `migration_utils.py:12,14`; `migrate_claude.py:17,19`; `migrate_chatgpt.py:18,21`; `migrate_notion.py:19,22` | `sys.path.insert(0, dirname(dirname(__file__)))` = `scripts/`, then `from scripts.migrate…` / `from src.db…` → `ModuleNotFoundError` when invoked as documented (`python scripts/migrate/migrate_claude.py <dir>`; simulated, confirmed). 0 `claude_chat`/`chatgpt_chat`/`notion_page` rows live. Tests pass only under pytest's rootdir. | One more `dirname` (or `python -m scripts.migrate.migrate_claude`); add a subprocess smoke test; or archive the three importers. |
| QD-14 | S2 | `scripts/ingest_qd_chats.py:86-92` | Filters `message_count >= 2 AND deleted_at IS NULL` only; upstream `sessions` has `private`, `incognito`, `archived`, `session_type` columns (schema verified) that are ignored. Currently 0 flagged rows, so no leak *today*; the policy gap is code, not data. | Add `AND private = 0 AND incognito = 0` (and decide on `archived`/non-`main` types); test with a fixture row per flag. |
| QD-15 | S2 | `ingest_doc_chunks.py:41,116`; QD schema | Identity = `node` id (not content-stable): 205 `(source_file, chunk_index)` groups duplicated, 3,568 byte-identical surplus rows (11 %); `file_chunks.text_hash` is available and unused; no `AUTOINCREMENT` upstream → rowid reuse can alias two chunks under one URL. | Key on `text_hash` (or `file_id:chunk_index:text_hash`); one-off dedup of the 3,568 rows when the channel is next touched. |
| QD-16 | S2 | `ingest_doc_chunks.py:78,26,166` | Opens the private sqlite read-write, `DB_POOL_MAX=20` threads, hourly full scan of 23,015 rows and 46 progress lines for a channel retired since 06-04 (`verify_liveness.sh:43-53`). | `mode=ro`; remove from `qd_sync.sh` while retired; when re-armed, page by `file_chunks.id > cursor`. |
| QD-17 | S2 | `src/qd_profile.py:5-6,49`; `migrate_quick_desktop.py:67,275,346`; `verify_liveness.sh:132-134` | Stale check exists in 1 of 6 scripts, logs twice, log-only; chain reports `captures_this_cycle=0` with no alert; monitor advisory only; neither distinguishes the KG store (frozen 09-16) from chats (live 09-22). | Move `warn_if_stale` into a shared preflight that every step calls once and that returns a status the wrapper can surface; monitor both files. |
| QD-18 | S2 | `redistill_capped.py:66-75,126,141,185` | `HAVING count(*) = 5` selects sessions; a session that yields nothing stays at 5 and is re-LLM'd on every run (docstring `:9-10` claims "correct idempotency"); `AgentInvoker()` → Opus 4.8 default, 300 s default timeout, unmetered, raw transcript. | Record `metadata.redistill_attempted` on the session's items; route via resolver. Or archive — it is a finished one-off. |
| QD-19 | S2 | `import_slack_graph.py:131-137` vs `:144-149` | Channel upsert *replaces* `properties` and `source_type`; Person upsert *merges* `properties` and keeps `source_type`. On a name collision with a QD-imported Channel the `qd_entity_id` is destroyed. | One upsert helper, merge semantics for both. |
| QD-20 | S2 | `scripts/backfill_projects.py:95-107`; live | Strategy 1 keys on the `ide_<name>.md` URL scheme that `dedup_ide_backfill.sh` deleted (0 rows live) → dead path; 5,564 of 5,830 chat parents still `project IS NULL`. 752 legacy `cli_*.md` bare-filename rows remain (`kiro_cli_chat`), never cleaned like their IDE twins; `distill_sessions` derived items from them (`distill://cli_017e7a17-bc7.md#…`). | Rewrite Strategy 1 for `chat://<id>` or drop it; decide the fate of the 752 legacy rows (they are invisible to `chat_extract` dedup). |
| QD-21 | S3 | `scripts/migrate/` (directory) | Six recurring jobs and six one-shots share a directory named for neither (§4). | `src/capture/quick_desktop/` for the five QD steps + `ingest_qd_chats`; `scripts/maintenance/` for finished one-offs; delete the three importers or move them with a working entrypoint. |
| QD-22 | S3 | §7 table | Header rendering ×3, JSONL reader ×4, sqlite open ×4, progress block ×2, direct-write path ×4, threshold constants ×2 (+ a test to keep them equal), `test_db` ×4, dead `get_processed_source_urls("quick_desktop")` call (`migrate_quick_desktop.py:277`), dead `create_memory` wrapper (`:26-28`). | One `render_header()`, one `read_jsonl()`, one `open_qd_sqlite()`, all writers through `ingest_content` (or a `store_memory()` that embeds). |
| QD-23 | S3 | `docs/CAPTURE-COMPONENTS.md:13`; `src/prompts/explorer.py:91`; `src/mcp_server.py:365`; prior audit 02 §"NOT FOUND", 04:65 | "new embeddings now supplied by local wrapper" — false (QD-03). Explorer prompt advertises `quick_desktop_*` "documents, chats, decisions, people" as live (docs retired, people last 04-26). Prior audit: "no writer for quick_desktop_synth" (label, not type) and "KG written only by migrate_quick_desktop" (also `import_slack_graph`). | Fix the three lines; note the correction in the audit index. |
| QD-24 | S3 | `scripts/jobs/reindex_embedding.sh:8,14,30` | Docker `/dev/shm` rationale and `CON=second-brain-db` are vestiges (native Postgres); `ALTER DATABASE memory_bank SET hnsw.ef_search` hardcodes the DB name — same class as the `migrations/009:27` finding (prior audit §3). | `ALTER DATABASE %I` via `current_database()`; drop the Docker text. |
| QD-25 | S3 | `scripts/jobs/dedup_ide_backfill.sh`; `scripts/backfill_attention_ledger.py` | Both finished: 0 `ide_%` rows; 0 completed runs since 09-01 lack ledger slices (verified). Still in the live jobs directory. | Archive. |
| QD-26 | S3 | `migrate_claude.py:72`, `migrate_chatgpt.py:100` (`metadata = {}` unused); `migrate_notion.py:133,163` (`mtime_iso` unused); `tests/test_ingest_doc_chunks.py:148` (`assert len(text.strip()) >= 0` is vacuous); `whitelist_categories` ×62 in `tests/` | Dead locals; a property test that cannot fail; a deprecated Hypothesis kwarg (`categories`) that is also non-inclusive language. | Remove/rename. |
| QD-27 | S3 | `migrate_quick_desktop.py:236-241` | `edge_failed` conflates "duplicate" with "endpoint missing"; the printed summary omits it entirely (`:361-369`). | Two counters; print both. |
| QD-28 | S3 | `ingest_doc_chunks.py:109-113` | Dry-run ignores `already` and reports "would process 23,015" for a fully-ingested channel (D10 family: dry-run misreports work). | Compute `already` in dry-run too. |
| QD-29 | S3 | `qd_sync.sh:16-29`; `logs/` | Own date-stamped log (~110–130 kB/day, ~46 progress lines × 24 from a retired step); `qd-sync-launchd.log` designated by the plist is 0 bytes. Pruning happens only via other jobs' wrapper. | Wrapper-owned logging; quieten `ingest_doc_chunks` progress to every 5,000 or only when `processed > 0`. |

---

## 9. Refactor seams this track exposes

1. **One QD channel module, five steps → one preflight + one pass.** All five `migrate/` QD steps read the same two sqlite files and write the same rows; `enrich_qd_tags` is a second pass over rows `migrate_quick_desktop` wrote seconds earlier. A single `capture.quick_desktop.run()` with `(preflight: profile + stale + ro-open) → memories → tags/domains → decisions → feed → chats` removes four process launches, four pool warm-ups, four `already` snapshots and the overlap race.
2. **Every writer through `ingest_content` (or a thin `store_memory` that embeds).** QD-03 is the direct cost of bypassing it. `capture/codex.py`'s transactional pattern (prior audit §6) is the target.
3. **Kill or finish the KG.** Nothing reads it, the embedding column cannot be filled, and it is the only table with hourly write churn. Freezing it (remove step #4, `--no-kg` default) is a one-line change that removes 648 k updates/period and 25 % of the chain's runtime; finishing it needs a reader and a 1024-dim migration.
4. **Extraction role in the resolver.** Distill/redistill need a `distill` (or reuse `skim`) role pinned to a Bedrock model with `assert_backend_supports_role` — the same seam the skim stage already proved.
5. **Receipts, not URL presence, for idempotency.** Distill (empty/failed), QD memories (failed rows), chats (appended sessions) all need a per-item status the next run can read. `get_processed_source_urls` as the sole dedup primitive is what turns every partial failure into a permanent skip.

---

## Could not verify

- Whether any QD-originated `Channel` entity has actually had its `properties`/`source_type` clobbered by `import_slack_graph` (QD-19): the 346 `slack_cache` channels all lack `qd_entity_id`, but I cannot tell if any of them pre-existed under another `source_type`.
- Whether the isolated-run test hazard (QD-12) has ever fired against production; live `qd-feed://evt_0000{1,2,3}` metadata may contain either real or test-injected `interactions` — I did not read metadata content to avoid inspecting private data.
- The writer of the 1,254 non-Person `memory_entities` rows (all 2026-04-21); no version of `migrate_quick_desktop.py` in git history links non-Person categories.
- How many QD edges between already-imported entities are missing (QD-07) — computing it requires joining upstream edge endpoints, i.e. reading entity names.
- Whether rowid reuse (QD-15, cursor hazard) has actually occurred.
- Semantics of Quick Desktop's `private` / `incognito` / `archived` session flags (QD-14) — inferred from column names only.
- Why the 09-16 upstream freeze happened (Quick side); and why the eventlog file stopped at 07-21 while the QD app continued to write sessions and the KG until 09-16.
- Whether `qd_sync.sh` overlaps have actually produced duplicate `qd://` rows — none exist today, so either the race has not fired for this job or a cleanup removed them.
- Machine-sleep gaps (e.g. 4 runs on 09-05) vs job failures — `launchctl` was not consulted.
