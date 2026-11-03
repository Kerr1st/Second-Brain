## review_capture

=== Context Gathering Summary ===
FOUND: All nine capture channels and both write paths: src/capture/{slack,outlook,browser,youtube,mcp_client,agent_tasks,codex}.py, src/capture/sources/codex.py (first 260 lines), src/parsers/{cli_chat,ide_chat,crawlee}.py, src/{ingest,capture_api,classify,depth,project,qd_profile}.py, all 13 in-scope scripts, src/db.py create_memory/get_processed_source_urls, all 19 launchd plists, and the scripts/jobs/*.sh wrappers that define exit-code handling.
NOT FOUND: src/capture/sources/codex.py lines 260-706 (attachments, enumerate_tasks, fetch_task) and src/capture/codex.py ~700-828 were not read due to output limits. No writer of quick_desktop_synth located despite a monitor test asserting it. scripts/monitor_jobs.* was out of scope, so monitor-visibility claims are inferred from job_wrapper.sh and the status file. No runtime/DB verification performed.
====================================

=== CONTEXT GATHERING COMPLETE ===

ANSWER:
# Capture & Ingestion Layer Review — /Users/kerrfir/second-brain @ 6b8c2d2

Read-only. No files were modified. Everything below is grounded in files I read; the closing section lists what I could not verify.

The layer is best understood as **seven independently-built channels plus two write paths**. Write path A is `src/ingest.py` (`ingest_content`/`ingest_batch`, parent + chunk + embedding). Write path B is raw SQL: `src/capture/codex.py` (`_capture_snapshot`, `_store_semantic_result`) and `src/capture_api.py` (`_store_memory`). `src/ingest.py:1` claims to be "the single path from content to PostgreSQL" — that claim is false as of this HEAD.

---

## 1. INCONSISTENCY ACROSS CHANNELS

### Comparison table

| Dimension | Slack `src/capture/slack.py` | Outlook `src/capture/outlook.py` | Browser `src/capture/browser.py` | YouTube `src/capture/youtube.py` | Crawlee/article `src/parsers/crawlee.py` + `scripts/crawlee_ingest.py` | Kiro chats `src/parsers/{cli,ide}_chat.py` + `scripts/chat_extract.py` + `scripts/batch_ingest_staged.py` | Quick Desktop `scripts/ingest_qd_chats.py` | Codex `src/capture/codex.py` | HTTP `src/capture_api.py` |
|---|---|---|---|---|---|---|---|---|---|
| **Exit codes** | 0/3/4/1, `main()->int`, `sys.exit(main())` `slack.py:337,348,350,354` | identical 0/3/4/1 `outlook.py:313,324,327,331` | **none** — `main()` returns `None`, always exit 0 `browser.py:346-357` | **none** — `main()` returns `None`, always 0 `youtube.py:214-222` | `sys.exit(1)` only if DB unreachable `crawlee_ingest.py:54`; else always 0 | always 0 (`chat_extract.py:102-108`, `batch_ingest_staged.py:1-108`) | `sys.exit(1)` if DB file missing `ingest_qd_chats.py:79`; else 0 | 0 / **1 if any task failed** `scripts/capture_codex.py:59` | HTTP 201/401/500; `sys.exit(1)` only for missing token `capture_api.py:203-205` |
| **Error taxonomy** | `CaptureFailure(kind)` auth/transport/error + `McpToolError` per-item `mcp_client.py:60-100` | same taxonomy, **no pre-flight probe** | none | none (subprocess rc 124 → `logger.warning` `youtube.py:56-58`) | none; per-item `except Exception` `crawlee_ingest.py:72-75` | none; per-phase `except Exception` `chat_extract.py:87-89,102-104` | none | `TaskFailure(task_id, stage, error_type)` + `CaptureReport.failures` `codex.py:110-140` | FastAPI `HTTPException(500)` |
| **Retry / reconnect** | 1 reconnect per **session** (`McpClient.call_json` `mcp_client.py:332-360`); probe never retried | same 1 reconnect; logs `reconnects_used` `outlook.py:299-301` | none | none | none | none | none | bounded semantic retry, 3 attempts, persisted in `metadata.semantic_attempts` `codex.py:471-520` | none |
| **Logging format** | `[%(asctime)s] %(levelname)s %(message)s`, `%H:%M:%S` `slack.py:345` | identical `outlook.py:321` | identical `browser.py:353` | identical `youtube.py:220` | `[%(asctime)s] %(message)s` + FileHandler to `logs/crawlee_ingest-<date>.log` `crawlee_ingest.py:27-38` | `[%(asctime)s] %(message)s` + FileHandler `chat_extract.py:31-40`; but `batch_ingest_staged.py:22-26` uses `%(asctime)s %(levelname)s %(message)s`, stderr only | `%(asctime)s %(message)s`, stderr only `ingest_qd_chats.py:25` | **stdout JSON only** `capture_codex.py:58`; module uses `logger` | uvicorn + middleware `capture_api.py:47-51` |
| **Dedup / idempotency** | `get_processed_source_urls("slack")` exact set + local `already.add` `slack.py:277,297` | two sets (mail/calendar) `outlook.py:281-282` | day-URL set; reading URLs deduped against Crawlee index `browser.py:295,321-328` | URL set + in-run `already.add` `youtube.py:171,205` | `get_processed_source_urls()` **with no source_type** → global cross-channel URL set `crawlee_ingest.py:57` | staging filenames + DB set, two key shapes, one of which never matches (see §3) | `qd-chat://<id>` set `ingest_qd_chats.py:96,104` | SQL lookup + **turn-id set difference** — the only channel that detects a *partial* prior capture `codex.py:~350-420` | **none** — repeat POST creates duplicates |
| **Chunking** | none locally; truncate 60 000 chars `slack.py:143-145`; then `chunk_by_section` | none locally; truncate 20 000 `outlook.py:131-132`; then `chunk_by_section` | `MAX_LOG_ENTRIES=400` cap `browser.py:59`; then `chunk_by_section` | none; then `chunk_by_section` | none; then `chunk_by_section` | none; then `chunk_by_section` | tool output truncated to 2 000 chars `ingest_qd_chats.py:60-62`; then `chunk_by_section` | **own unit** — Topic Segments from a model pass, not char-based `codex.py:~880-1000` | **no chunking at all** — one row, one embedding `capture_api.py:103-118` |
| **Metadata keys** | `conversation`, `kind`, `messages`, **`thread replies`** (space kept) `slack.py:149` | `from`, `to`, `folder`, `received`, `conversation`, `attachments` `outlook.py:136-149` | `pages`, `searches`, `attention`, **`raw visits`** `browser.py:255` | **`video id`**, `channel`, `duration`, `engagement` `youtube.py:150` | `source`, `type`, `date` | `source_type`, `source_id`, `date`, `model`, `workflow`, `project` (`Source-Type:` style, no `Source:` line) `cli_chat.py:139-147`, `ide_chat.py:151-160` | `source_type`, `source_id`, `agent_mode` `ingest_qd_chats.py:47-53` | structured JSON: `record_kind`, `native_task_id`, `turns`, `semantic_cursor` `codex.py:~300-345` | caller-supplied dict + `depth_score` |
| **source_url scheme** | `slack://<conv>/<YYYY-MM-DD>` `slack.py:88` | `outlook://mail/<received>/<sha1[:12]>`, `outlook://event/...` `outlook.py:106-110,152` | `browser://history/<YYYY-MM-DD>` `browser.py:214` | bare `https://www.youtube.com/watch?v=<id>` `youtube.py:90` | bare http(s) URL | `chat://<id>` (CLI id **truncated to 12 chars**) `batch_ingest_staged.py:67-69` | `qd-chat://<id>` `ingest_qd_chats.py:96` | `snapshot.source_identity`, children `...#segment-N` `codex.py:~960` | caller-supplied, may be `NULL` |
| **Embedding calls** | chunks only, plus 1 hidden full-body call in `_discover_relationships` `ingest.py:108` | same | same | same | same | same | same | 1 per segment + 1 per derived memory; task parent `embedding=NULL` `codex.py:829-833` | exactly 1, on the whole body |
| **Transaction boundary** | 1 auto-commit per row (`db.py:92-106`) | same | same | same | same | same | same | **explicit multi-statement txn, `FOR UPDATE`, `rollback()`, cursor-changed optimistic check, `fcntl` singleton lock** `codex.py:186-200`, `codex.py:~1010` | 1 row, 1 commit |
| **"Already captured" detection** | DB set, exact string | DB set, exact string on a content-derived hash | DB set on day URL | DB set on canonical watch URL | DB set, untyped | staging dir + DB set (two schemes) | DB set | SQL `metadata @> '{"record_kind":"captured_task"}'` + turn-id diff | none |
| **Pre-flight health check** | yes — `preflight()` probes `list_my_channels` before any capture `slack.py:170-190` | **no** (docstring at `outlook.py:20-24` explains reconnect instead) | file-existence only `browser.py:291` | `is_reachable()` `youtube.py:167` | `is_reachable()` `crawlee_ingest.py:53` | DB read wrapped in `except: pass` `chat_extract.py:61-65` | file-existence `ingest_qd_chats.py:78` | `fcntl` lock only | `/health` endpoint |

**The sharpest divergences**

- **Two channels (Slack, Outlook) have a failure contract; five have none.** `scripts/jobs/capture_slack.sh:11-27` and `capture_outlook.sh:5-29` document and translate exit 0/3/4/1. `capture_browser.sh:8` and `youtube_scrape.sh:11` use `exec` on a `main()` that can only ever return 0, so the wrapper's failure notification (`job_wrapper.sh:40-45`) can never fire for them. **High.**
- **Metadata key spelling is per-channel and partly broken.** `src/ingest.py:56` normalises only `-`→`_` after lower-casing, so `Thread replies:` becomes the key `"thread replies"` (with a space), `Video ID:` becomes `"video id"`, `Raw visits:` becomes `"raw visits"`. Meanwhile chat parsers emit `Source-Type:` which *does* normalise to `source_type`. There is no canonical header vocabulary. **Medium.**
- **Chat/QD documents carry no `Source:` line at all** (`cli_chat.py:139-147`, `ide_chat.py:151-160`, `ingest_qd_chats.py:47-53`), so `source_url or meta.get("source")` in `ingest.py:181` has no fallback for them; if a caller ever omits `source_url` the row lands with `source_url IS NULL` and is invisible to every dedup query. **Medium.**
- **Dedup scope differs.** `crawlee_ingest.py:57` calls `get_processed_source_urls()` with no argument, i.e. every `source_url` in the table (`db.py:304-312`). Every other channel scopes by `source_type`. The untyped variant happens to be protective today but silently couples channels.

---

## 2. DUPLICATION

| # | Duplicated logic | Instances | Severity |
|---|---|---|---|
| D1 | **Markdown header rendering** — `# title`, blank, `Source:`/`Type:`/`Date:`, `\n\n---\n\n`, body | `slack.py:144-158`, `outlook.py:136-149` and `outlook.py:161-175`, `browser.py:248-257`, `youtube.py:145-158`, `cli_chat.py:139-152`, `ide_chat.py:151-162`, `ingest_qd_chats.py:47-55`, `scrape_bookmarks.py:104` | Medium |
| D2 | **Metadata-header parsing** — three separate implementations | `ingest.py:27-60` (splits at `---`, does `-`→`_`), `crawlee.py:158-166` (no dash normalisation, no title), `crawlee.py:171-190` `_read_header` (bounded, adds title). The first two disagree on key normalisation. | Medium |
| D3 | **`completed_days(days_back, today)`** — byte-for-byte identical | `slack.py:160-163` and `browser.py:210-212` | Low |
| D4 | **Local-day → UTC bounds** | `slack.day_bounds_utc:166-172` and `browser.load_visits:224-227` build the same `datetime.combine(day, time.min, tzinfo=tz)` window | Low |
| D5 | **ISO-timestamp parsing with `Z` fix-up** | `slack._hhmm:92-100`, `slack._parse_ts:217-222`, `sources/codex.py:_record_timestamp:158-168` | Low |
| D6 | **HTML→text cleaning** | `outlook.html_to_text:84-96` (script/style strip, `<br>`→newline, unescape, whitespace collapse) vs `scrape_bookmarks.scrape_with_cookies:87-91` (a cruder copy: no `html.unescape`, no `<br>` handling) | Medium — the bookmark copy produces worse text for the same corpus |
| D7 | **Staged-file batch ingest** | `batch_ingest_staged.py:38-106` and `batch_ingest_parallel.py:39-110` are the same function, one sequential and one threaded; `ingest_chats.py:24-63` is a third copy with a different `source_url` scheme | High (see §4) |
| D8 | **Progress/ETA + summary printing** | `batch_ingest_staged.py:92-105`, `batch_ingest_parallel.py:94-110`, `ingest_qd_chats.py:135-147` — identical `processed=/skipped=/failed= | N files/s | ETA Nm` block | Low |
| D9 | **argparse + `logging.basicConfig` + `--dry-run` boilerplate** | `slack.py:337-345`, `outlook.py:313-321`, `browser.py:346-353`, `youtube.py:214-220`, `ingest_qd_chats.py:70-76`, `batch_ingest_staged.py:39-42`, `distill_sessions.py:214-224`. `chat_extract.py:52-62` doesn't use argparse at all — it scans `sys.argv` for `--dry-run`/`--backfill` by substring. | Medium |
| D10 | **Dry-run handling**, five different shapes | `slack.py:329-332` (`{"new": n}`), `outlook.py:305-309` (per-source `{"new": n}`), `browser.py:335-338` (`{"new","reading"}`), `youtube.py:187-190` (increments `processed`, **misreporting a dry run as work done**), `codex.py` (`CaptureReport(dry_run=True)`), `ingest_qd_chats.py:124-126`. Callers must then write `stats.get("processed", stats.get("new", 0))` — `slack.py:349`, `outlook.py:326`, `browser.py:355`. | Medium |
| D11 | **`sys.path` bootstrap** | `capture_codex.py:16-18`, `crawlee_ingest.py:21`, `chat_extract.py:19`, `ingest_chats.py:11`, `batch_ingest_staged.py:20`, `batch_ingest_parallel.py:20`, `ingest_qd_chats.py:20`, `distill_sessions.py:22`, `cleanup_duplicate_sources.py:29`, plus `browser.py:99` which mutates `sys.path` *inside a function* | Low |
| D12 | **URL skip/internal-domain filters** | `scrape_bookmarks.py:21-40` is imported by `browser.py:100`, but `browser.py:68-96` then re-declares its own overlapping `_NOISE_HOST_RE`/`_APP_HOST_RE`/`_PRIVATE_HOST_RE`. Two overlapping filter vocabularies for one decision. | Medium |

---

## 3. CONTRADICTIONS AND INACCURACIES

| # | Sev | Location | Statement vs reality |
|---|---|---|---|
| C1 | **High** | `src/ingest.py:1` | "the single path from content to PostgreSQL". `capture_api._store_memory` (`capture_api.py:103-118`) and Codex (`codex.py:~370-430`, `~1010-1090`) both write `memories` directly via SQL, bypassing chunking, `parse_metadata_header`, and relationship discovery. |
| C2 | Medium | `src/ingest.py:3-4` | "All source types (Crawlee markdown, CLI chats, IDE chats, notes)" — omits slack, outlook_email, outlook_calendar, browser_history, youtube, quick_desktop_chat, and mis-lists "notes", which no scoped module produces. |
| C3 | Medium | `src/capture/__init__.py:1-5` | "Each source produces metadata-headed markdown and feeds the shared ingestion pipeline… **Phase 0: `youtube`**." Five channels now live, and `codex` does *not* feed `ingest_content`. |
| C4 | Medium | `src/depth.py:3-5` | "Replaces the binary `_DEPTH_RE` regex check in mcp_server.py." `_DEPTH_RE` no longer exists anywhere in `src/` (only in stale `__pycache__`); the docstring points at a symbol that is gone. |
| C5 | **High** | `src/capture_api.py:157,171,187` | Writer/reader `source_type` disagreement. The API writes `"slack"`, `"browser_extension"`, `"email"`. `"slack"` collides with the real Slack channel's `SOURCE_TYPE` (`slack.py:41`) — an API-captured Slack row has no `slack://` URL and no chunk hierarchy, yet joins the same `source_type` bucket. `"browser_extension"` and `"email"` are in the documented taxonomy nowhere; the equivalents are `article` and `outlook_email`. |
| C6 | Medium | `src/mcp_server.py:131` | Documents `source_type` examples as `'distilled_chat', 'cli_chat', 'kiro_ide_chat', 'article'`. No writer emits `cli_chat`; the writer emits `kiro_cli_chat` (`cli_chat.py:141`, `batch_ingest_staged.py:65`). A reader following this doc retrieves nothing. |
| C7 | Medium | `src/prompts/explorer.py:91` | "`quick_desktop_*` — documents, chats, decisions, people from the Quick Desktop index" presents the retired `quick_desktop_doc` channel (dormant since 2026-06-04, written only by `scripts/migrate/ingest_doc_chunks.py:36`) as live, to the model that does retrieval. |
| C8 | Medium | `scripts/chat_extract.py:79,95` | `if f"chat://{filename}" in ingested or f"ide_{filename}.md" in ingested:` — `ingested` is built from `get_processed_source_urls("kiro_cli_chat"|"kiro_ide_chat")` (`chat_extract.py:62-63`), which contains only `chat://…` values. The second disjunct is dead in both branches. The comment "either source_url scheme" describes a scheme (`ingest_chats.py:41`, bare filename) that only a dead script ever wrote. |
| C9 | Medium | `scripts/chat_extract.py:1-8` | "Reads from **all three** chat sources" — it reads two (`ide_chat`, `cli_chat`, imported at `chat_extract.py:20-21`). Quick Desktop has its own separate script and its own `source_type`. |
| C10 | Medium | `scripts/jobs/ingest_staged.sh:22` | Comment "starting Docker container if needed" while the code runs `brew services start postgresql@17`. `job_wrapper.sh:16-18` explicitly records that the Docker path was removed. |
| C11 | Low | `src/capture/browser.py:16-20` | "mail/chat/calendar web apps (captured by their own collectors)" — `_CHAT_HOST_RE` (`browser.py:82`) covers chatgpt.com / claude.ai / gemini, and no collector in this repo captures those. Their content is counted and then discarded (`browser.py:238-241`). |
| C12 | Low | `src/capture/agent_tasks.py:1-6` | "Codex is the sole v1 integration; a shared adapter contract can be extracted when a second source is implemented." Nine capture channels now exist; the deferral rationale is stale even though the value objects are genuinely Codex-only. |
| C13 | Low | env var naming for one concept | Per-channel prefixes with no shared convention: `SLACK_CALL_TIMEOUT`/`OUTLOOK_CALL_TIMEOUT` (same concept, two names, `slack.py:45`, `outlook.py:60`); `SLACK_MAX_CONVERSATIONS` vs `OUTLOOK_MAX_MAIL_PER_FOLDER` vs `BROWSER_MAX_READ_URLS` (same "per-run ceiling"); `SLACK_MCP`/`GRASP_MCP`/`CODEX_HOME`/`CHROME_HISTORY` (same "where the source lives"); `DISTILL_TIMEOUT` vs `*_CALL_TIMEOUT`. YouTube and the chat parsers expose **no** env overrides at all, so their ceilings (`youtube.py:44`, `cli_chat.py:22-24`, `ide_chat.py:29-31`) are code-only. |
| C14 | Low | `scripts/distill_sessions.py:32,35` vs taxonomy | Writes/reads `quick_desktop_chat` and `distilled_chat`, neither of which appears in the documented `source_type` list for this layer. `ingest_qd_chats.py:30` is the sole writer of `quick_desktop_chat`. |

---

## 4. DEAD CODE

| # | Sev | Item | Evidence |
|---|---|---|---|
| X1 | Medium | **`scripts/ingest_chats.py` — superseded by `scripts/batch_ingest_staged.py`** | No caller anywhere: no plist, no `scripts/jobs/*.sh`, no test. The only mention is `backups/config/CAPTURE-COMPONENTS.md:11`, which states it "uses a different legacy URL scheme". It writes `source_url=fname` (`ingest_chats.py:41`, e.g. `cli_abc.md`) whereas the live path writes `chat://<id>` (`batch_ingest_staged.py:69`), so its rows are invisible to current dedup. It also contains a bare `except:` (`ingest_chats.py:58`). |
| X2 | Medium | **`scripts/batch_ingest_parallel.py` — superseded by `scripts/batch_ingest_staged.py`** | `scripts/jobs/ingest_staged.sh:41` runs `batch_ingest_staged.py`. `batch_ingest_parallel.py` has no caller and no test; the only reference is a backup of a GitHub README (`backups/kb-sources/…:244`). It is the same function with a thread pool, and it drops the `--dry-run` flag its sibling has. |
| X3 | Medium | **`src/parsers/crawlee.py:158-166` `parse_metadata_header`** — no caller | Repo-wide grep: every `parse_metadata_header` import resolves to `src.ingest` (`ingest.py:161`, `tests/test_migration.py:34`). Superseded in-module by `_read_header` (`crawlee.py:171-190`), which is what `build_source_map` and `find_unindexed_output_files` actually call. |
| X4 | Low | **`src/parsers/crawlee.py:149-155` `read_source_file`** — no caller | `parse_all` reads by full path from `source_map` (`crawlee.py:281-282`), not by filename. |
| X5 | Low | **`src/parsers/ide_chat.py:214` `return stats`** inside a generator | `parse_all` is a generator (`yield` at `ide_chat.py:212`), so the `return stats` value is only reachable via `StopIteration.value`. The sole caller iterates it (`chat_extract.py:78`) and never reads it, so `stats` (`ide_chat.py:198`) is computed and thrown away — while `chat_extract` maintains its own parallel counters. The CLI-side parser has no equivalent, so the two parsers have divergent, both-unused, stats contracts. |
| X6 | Low | **`src/capture/mcp_client.py:66-73` `McpAuthError` / `McpTransportError` never caught by name** | Both are raised (`mcp_client.py:300,340,357`) but no `except McpAuthError`/`except McpTransportError` exists; consumers re-classify from a broad `except Exception` (`slack.py:324-327`, `outlook.py:294-297`). The typed classes are decoration over string matching. |
| X7 | Low | **`src/capture/agent_tasks.py:54-57` `AgentTurn.turn_key`** | Self-described as a "compatibility alias local to the retained source parser"; I found no use of `.turn_key` in `src/capture/codex.py` or `src/capture/sources/codex.py` (both use `turn_id`). |
| X8 | Low | **`scripts/run_golden_capture.py`** — not a capture-layer script and has no caller | It runs `DreamCycleOrchestrator` (`run_golden_capture.py:23-25`), not a capture channel; superseded by the fixtures it was written to produce (`tests/fixtures/golden_run`). Its name is actively misleading inside this layer. |
| X9 | Info | **`scripts/cleanup_duplicate_sources.py`** — no caller, and that is correct | A one-shot remediation for the 6,163-row incident (`cleanup_duplicate_sources.py:1-12`), `--dry-run` by default, with backup tables. Keep, but it should be moved out of the live-capture surface so it is not mistaken for a scheduled job. |
| X10 | Medium | **`scripts/scrape_bookmarks.py` is both a live job and a de-facto library** | `scheduling/com.second-brain.bookmarks.plist` runs it as a job, *and* `browser.py:100,320` imports `SKIP_PATTERNS`, `INTERNAL_DOMAINS`, `load_indexed_urls`, `scrape_with_crawlee` from it via a `sys.path` mutation. Importing it does not execute `main()` (guarded at `scrape_bookmarks.py:165-166`), so this is not a live bug, but the shared filters belong in `src/`, not in a script. |

---

## 5. CORRECTNESS RISKS

### R1 — `chunk_by_section` emits a single unbounded chunk for Slack and YouTube documents. **High.**
`src/ingest.py:63-102`. Splitting is `re.split(r'\n(?=##\s)', body)`; with no `##` headers it falls back to `body.split("\n\n")`. Slack renders its day body with `"\n".join(lines)` (`slack.py:141`) and YouTube renders transcripts with `"\n".join(out)` (`youtube.py:138`) — **neither ever produces a blank line**. So `paragraphs` is a single element, the `if current_len + len(para) > max_chunk_chars and current` guard is False on the first iteration (`current` is empty), and the merge loop returns `[body]`. Result: one chunk of up to 60 000 chars for Slack (`MAX_DOC_CHARS`, `slack.py:57`) and of unbounded length for a YouTube transcript, handed straight to `generate_embedding` (`ingest.py:203`). `max_chunk_chars=4000` is silently not enforced for the two highest-volume channels. Fix: make `chunk_by_section` hard-split any section exceeding `max_chunk_chars` on line boundaries, and add a property test asserting `max(len(c) for c in chunks) <= max_chunk_chars * 1.1` for header-less, blank-line-free input.

### R2 — Partial document writes are committed and then permanently skipped. **High.**
`src/db.py:92-106` commits every `create_memory` in its own transaction. `ingest_content` (`ingest.py:174-209`) commits the parent, then commits each chunk in a loop. If chunk 3 of 9 raises (Bedrock throttle, oversized input per R1), the parent and chunks 1-2 are already durable, the `source_url` is now in the table, and `ingest_batch` counts `failed` (`ingest.py:236-238`) — but the **next** run reads that `source_url` from `get_processed_source_urls` and skips it (`ingest.py:226-228`). The document stays permanently half-embedded with no marker. Contrast `codex.py:186-200` / `_store_semantic_result`, which wraps everything in one transaction with `rollback()`. Fix: wrap parent+chunks in one transaction (a `create_memory_tree(conn, …)` taking an explicit connection), or write the parent last, or record `metadata.chunks_expected` and treat a mismatch as not-yet-captured.

### R3 — Empty-timestamp collapse in Outlook source URLs — same shape as the crawlee `"" in text` bug. **High.**
`outlook.py:106-110`: `received = (msg.get("receivedDateTime") or msg.get("sentDateTime") or "")[:19]` → when both are absent the URL is `outlook://mail//<hash(subject|sender)>`. Every body-less-timestamp message with the same subject+sender collapses onto **one** `source_url`; the first wins and the rest are silently dropped as "already captured" (`outlook.py:216-218`). `event_source_url` (`outlook.py:152-154`) has the identical `or ""` on `ev["start"]`. Additionally, the hash has no body component, so two genuinely different mails sent in the same second by the same sender with the same subject collide by design. Fix: refuse to build a URL when the timestamp is empty (raise/skip and count it), and include a body digest in `_hash`.

### R4 — Identity by truncated prefix: `conv_id[:12]`. **High.**
`chat_extract.py:94` truncates the CLI conversation id to 12 characters for the staging filename; `batch_ingest_staged.py:67-69` then derives `source_url = f"chat://{f.stem minus prefix}"` from that filename. Two CLI sessions sharing a 12-char prefix produce the same `source_url`; the second is counted as `skipped` and its file is moved to `staging/ingested` (`batch_ingest_staged.py:71-77`), i.e. deleted-by-move with no error. The IDE side does not truncate (`chat_extract.py:83`), so the two halves of one channel have different identity strength. Fix: use the full id in the filename (or a hash of it) and assert `len(set(source_urls)) == len(files)` before ingesting.

### R5 — Containment tests used for identity/eligibility decisions. **Medium-High.** (The crawlee-bug family.)
- `scripts/scrape_bookmarks.py:21-30`: `SKIP_PATTERNS.search(u)` applies bare substrings `login`, `signin`, `isengard`, `phonetool`, `sim.amazon`, `issues.amazon`, `cr.amazon`, `code.amazon` **against the whole URL**, unanchored. Any URL containing "login" in a path or query is dropped — e.g. an article at `…/blog/how-single-signin-works`. `.` is unescaped throughout, so `sim.amazon` also matches `simXamazon`. This filter is then imported and applied to *all* browser history by `browser.py:100,150`, so the over-skip propagates to a second channel.
- `browser.py:200-203` `canonical_url`: `k.lower().startswith((… "ref" …))` drops any parameter whose name *begins with* `ref` — `refresh`, `reference`, `referrer`, `refinement`. Two distinct pages differing only in such a parameter collapse to one `source_url`, so one of them is never captured.
- `scrape_bookmarks.py:51-55` `load_indexed_urls`: `re.finditer(r"\((https?://[^)]+)\)", f.read())` over the **entire** `index.md` text — not per row and not scoped to the source cell. Any URL appearing inside a summary link or a note is treated as already-indexed, so it is never scraped. This is the inverse failure of the crawlee bug: the fixed parser (`crawlee.py:80-105`, `build_source_map` at `crawlee.py:193-222`) is now rigorous, but the *bookmark* side still reads the same file with the old loose regex. **These two readers of `index.md` must be unified.**
- `slack.py:243-249` `_dm_inactive_for`: `return la < since` compares a server-supplied `lastActivity` string lexicographically against a `%Y-%m-%dT%H:%M:%SZ` string. Any format difference (offset instead of `Z`, epoch ms, missing seconds) makes the comparison meaningless and silently skips the DM for the whole window, with no log line.
- `cli_chat.py:130-134` `is_automated_conversation`: `any(m in first[:120] for m in _AUTOMATED_MARKERS)` — a human message that merely quotes `"Run type:"` in its first 120 chars is discarded as an automated sub-agent conversation.
- `distill_sessions.py:222` `{u.rsplit("#", 1)[0] for u in already}` — recovers the session key by splitting on the last `#`; any `source_url` containing `#` (Codex segments use `#segment-N`, `codex.py:~960`) would be mis-keyed if ever routed here.

### R6 — Swallowed exceptions that hide real failures. **Medium-High.**
- `ingest.py:105-146` `_discover_relationships`: four bare `except Exception: pass`. The first block calls `generate_embedding(body)` on the **entire document body** (`ingest.py:108`) — a second, larger embedding call per document that no channel knows about. For an oversized body (R1) it raises, the exception is swallowed, and relationship discovery silently never runs for exactly the largest documents. Invisible to every monitor. Fix: `logger.warning` with the source_url, and count discovery failures in the stats dict.
- `browser.py:99-103` `_import_bookmark_filters`: a broad `except Exception` returns a drastically weaker fallback (`INTERNAL_DOMAINS = re.compile(r"\.amazon\.com")`) with the `# pragma: no cover` comment "the scraper is part of this repo". If the import ever breaks, internal `*.a2z.com` / `*.aws.dev` URLs are reclassified `public` (`browser.py:156-158`) and sent to Crawlee (`browser.py:330`) — a confidentiality regression, silently. **This is the highest-consequence swallowed exception in the layer.**
- `chat_extract.py:61-65` `except Exception: ingested = set()`: a DB hiccup means the entire chat history re-stages. The comment calls `batch_ingest_staged` the "downstream dedup backstop", which is true for the DB, but the staging directory fills with thousands of files first.
- `ingest_chats.py:56-59`: bare `except:` around the rename (dead script, but it is a bare except).
- `crawlee.py:203-217` / `find_unindexed_output_files`: file reads are unguarded inside a generator (`crawlee.py:281`, `crawlee.py:322`) — one unreadable file aborts the whole `parse_all` iteration mid-stream, and `crawlee_ingest.py`'s per-item `try` (`:63`) does not cover the generator's own `next()`.

### R7 — Silent no-ops that exit 0 and are invisible to monitors. **High.**
- `browser.py:291-293`: Chrome History missing → `logger.error` + zeros; `main()` (`browser.py:346-357`) returns `None`; `capture_browser.sh:8` `exec`s it → **exit 0**. `job_wrapper.sh:40-45` only notifies on non-zero, so a permanently relocated Chrome profile produces a green job forever.
- `youtube.py:167-169`: DB unreachable → `logger.error` + `return stats`; `main()` discards the return (`youtube.py:221-222`) → exit 0.
- `youtube.py:184-190`: in `--dry-run`, `stats["processed"] += 1` — a dry run reports the same counter a real run does, so a log line cannot distinguish them.
- `scripts/jobs/ingest_staged.sh:46-49,80`: the crawlee failure is logged (`Crawlee ingest FAILED`) but the script ends with an unconditional `exit 0`, and the status file it writes for monitoring (`ingest_staged.sh:65-74`) contains no crawlee-failure field — only a before/after row count (`ingest_staged.sh:43-52`). A crawlee ingest that fails every night looks identical to one that finds nothing new.
- `chat_extract.py:69,102-108`: `errors` is incremented and printed, then the process exits 0.
- `scrape_bookmarks.py:126-163`: `main()` prints and never exits non-zero; the plist routes it through `job_wrapper.sh`, whose notification therefore can never fire. A 1-hour Crawlee timeout (`scrape_bookmarks.py:120-121`) prints `⚠` and returns normally.
- `scripts/jobs/qd_sync.sh:19-24`: `set -euo pipefail`, six python scripts chained with no wrapper. The first failure aborts the rest of the chain, and `com.second-brain.qd-sync.plist` does **not** use `job_wrapper.sh` (unlike every other job), so there is no notification — only a line in `logs/qd-sync-<date>.log`. The completion line (`qd_sync.sh:27`) is never reached, which is the only signal.
- **Monitor/writer mismatch:** `tests/test_monitor_jobs.py:283,286` asserts freshness checks for `quick_desktop_synth`, but I found **no writer** of `quick_desktop_synth` in `src/` or `scripts/` (the QD writers emit `quick_desktop_chat`, `quick_desktop_doc`, `quick_desktop_decision`, `quick_desktop_feed`, …). Either the monitor watches a source_type nothing produces (permanently red or permanently ignored) or the writer lives outside this repo. Flagged for verification.

### R8 — Timezone handling is inconsistent and in places unsound. **Medium.**
- **Local vs UTC day boundaries differ by channel.** Slack and Browser use completed *local* days (`slack.py:160-172`, `browser.py:210,224-227`); Outlook uses a rolling *UTC* window (`outlook.py:277-279`); the chat parsers stamp `Date:` from a *UTC* conversion of an epoch (`cli_chat.py:133-136`, `ide_chat.py:143-149`). The same afternoon can therefore be filed under two different dates depending on which channel captured it, which breaks any date-joined retrieval.
- **Fixed-offset tz captured at call time.** `slack.py:168` and `browser.py:224` both do `tz = datetime.now().astimezone().tzinfo`, producing a *fixed-offset* tzinfo, then `datetime.combine(day, time.min, tzinfo=tz)`. On a DST-transition day the computed midnight boundary is off by one hour, so one hour of messages/visits is attributed to the wrong day — and because both channels key dedup on the day URL, the lost hour is never re-read.
- **Unit-guessing heuristic.** `ide_chat.py:143-146`: `file_timestamp / 1000 if file_timestamp and file_timestamp > 1e12 else file_timestamp or 0` — a milliseconds/seconds guess by magnitude. A missing `start_time` silently falls through to `datetime.now()`, dating an old chat as today.
- `ingest_qd_chats.py:43` assumes `session["created_at"]` is seconds with no validation.
- **Codex is the only channel that enforces awareness** (`agent_tasks.py:14-17` `_aware`, raising on naive datetimes; `codex.py:210-212` rejects a naive `now`). Every other channel accepts whatever the source provides.

### R9 — Cursor-loop spin risk. **Low.**
`slack.fetch_day:227-241` and `outlook.collect_mail:206-222` / `collect_calendar:225-247` advance on a server-supplied token and break only when the token is absent. A server that echoes the *same* token with `hasMore: true` re-fetches the same page; the loops terminate only because of the `MAX_MESSAGES_PER_DAY` / `max_per_folder` / `max_events` counters. There is no "token did not change" guard and no log when the ceiling is hit, so a truncated capture is indistinguishable from a complete one. I found **no genuinely unbounded retry loop** in the layer; Codex's semantic retry is explicitly bounded and persisted (`codex.py:471-520`).

### R10 — `capture_api` has no dedup and no chunking. **Medium.**
`capture_api.py:103-118` `_store_memory` calls `create_memory` unconditionally. Two identical POSTs create two rows; there is no `source_url` uniqueness check and `source_url` may be `NULL` (`capture_api.py:63`). Content up to 100 000 chars (`capture_api.py:59`) is embedded as a single vector with no chunking, so an API-captured document is structurally unlike the same document captured by any collector, and is unreachable by chunk-level vector search.

### R11 — `preflight` exists for Slack only. **Medium.**
`slack.py:170-190` is the documented fix for the 2026-09-20/21 silent auth failure, and `slack.py:320-322` correctly refuses to attempt the capture when it fails. Outlook has no equivalent: a stale Graph token is only discovered after `collect_mail` has begun (`outlook.py:291`), so mail pages may already be half-processed when the run aborts. Because `ingest_batch` is called only after `collect` returns (`outlook.py:304-311`), a mid-collection auth failure discards **all** rendered items for that run rather than ingesting what was collected.

---

## 6. REFACTOR SEAMS

The code already converged on a de-facto contract. Nine channels independently implement the same five steps: **preflight → enumerate a window → render `(source_url, markdown)` → ingest via `ingest_batch` → map an outcome to an exit code.** Slack and Outlook have all five; the others have a subset.

### Proposed interface

```python
# src/capture/framework.py  (proposed — does not exist)
class CaptureChannel(Protocol):
    source_type: str                     # canonical taxonomy value, single definition site
    def preflight(self) -> tuple[str, str]:      # ("ok"|"auth"|"transport"|"error", detail)
    def window(self, days_back: int) -> Window   # completed-local-days OR rolling-UTC, declared explicitly
    def collect(self, w: Window, already: set[str]) -> Iterable[CapturedDoc]
    # CapturedDoc = (source_url: str, markdown: str); source_url validated non-empty + scheme-checked

def run_channel(ch, *, days_back, dry_run) -> int:   # the shared runner
    # 1. already = get_processed_source_urls(ch.source_type)
    # 2. kind, detail = ch.preflight();  non-ok -> CaptureFailure -> report_failure -> exit 3/4/1
    # 3. items = ch.collect(...);  assert every source_url non-empty, scheme-valid, unique in batch
    # 4. dry_run -> uniform {"dry_run": True, "would_ingest": n};  else ingest_batch (one txn per doc)
    # 5. uniform summary line + uniform "captures_this_cycle: <channel>=<n>" + exit code
```

Three pieces move out of the channels into shared modules:
- `src/capture/render.py` — `header(title, source_url, type, date, **extra)` with a **canonical, underscore-only** key vocabulary (fixes the `thread replies` / `video id` / `raw visits` spellings) and one `html_to_text` (retiring D6's crude copy).
- `src/capture/window.py` — `completed_local_days()` and `rolling_utc_window()`, DST-correct via a real zoneinfo tz rather than a snapshotted fixed offset (fixes R8, retires D3/D4).
- `src/ingest.py` — `ingest_document(conn, …)` writing parent+chunks in **one** transaction (fixes R2) and a `chunk_by_section` that hard-splits oversized sections (fixes R1).

### Per-channel disposition

| Channel | Distance | Keeps (genuinely channel-specific) | Drops to the framework |
|---|---|---|---|
| **Slack** — **closest** | Already the reference: `preflight` (`slack.py:170-190`), `CaptureFailure`→exit mapping (`slack.py:337-350`), clean `collect`/`render_day` split, injectable `Call` type for tests (`slack.py:62`) | `clean_text` datamark/tag undo (`slack.py:69-78`), conversation-day grouping, thread rendering | header building, `completed_days`, dry-run shape, exit mapping, stats logging |
| **Outlook** | Very close; only `preflight` missing | Graph pagination, content-derived URL hashing, envelope stripping (`outlook.py:74-80`) | same as Slack, **plus gains `preflight`** (fixes R11) |
| **Browser** | Medium — has `collect`/`render` but no taxonomy or exit code | Chrome SQLite snapshot + `chrome_time_to_dt` (`browser.py:127-131,218-234`), dwell/attention scoring, URL classification and secret-param stripping | exit codes (fixes R7), header building, `completed_days`; the filter regexes move to `src/capture/filters.py` so `scrape_bookmarks` and `browser` stop disagreeing (fixes D12/R5) |
| **YouTube** | Medium | yt-dlp invocation, `vtt_to_text`, playlist→engagement mapping | switch `ingest_content`→`ingest_batch`, exit codes, honest dry-run counter (fixes R7), chunking (fixes R1) |
| **Crawlee/article** | Medium; the parser is the best-tested piece in the layer after the fix (`crawlee.py:80-105`, `build_source_map`) | index/source-file resolution, `last_parse_stats` | the runner in `crawlee_ingest.py` collapses into `run_channel`; `load_indexed_urls` moves in so bookmarks and crawlee share one reader (fixes R5) |
| **Kiro chats** | **Far** — two-phase staging is a different architecture | stripping/filter rules (`ide_chat.py:75-118`, `cli_chat.py:99-134`) | the staging hop is the thing to delete: parse → `run_channel` directly, which removes `chat_extract.py`, `batch_ingest_staged.py`, `batch_ingest_parallel.py`, `ingest_chats.py`, the `conv_id[:12]` truncation (fixes R4) and the dead dedup branch (C8) |
| **Quick Desktop** | Far | SQLite session read, tool-output truncation | same as Kiro chats; header keys align to the canonical vocabulary |
| **Codex** | **Furthest, and deliberately so** | Everything below `collect`: monotonic turn capture, `fcntl` singleton (`codex.py:186-200`), model-driven Topic Segments, bounded semantic retry, transactional writes with `FOR UPDATE` and an optimistic cursor check. **Codex is the only channel that is transactionally correct, the only one that detects partial prior capture, and the only one that enforces tz-awareness** — the framework should adopt its transaction and progress-cursor patterns, not the reverse | only the outer shell: exit-code mapping and the summary line |
| **capture_api** | Outside the model | HTTP/auth surface | its `_store_memory` (`capture_api.py:103-118`) should call the shared `ingest_document` so API captures get chunking and dedup (fixes R10), and its `source_type` values align to the taxonomy (fixes C5) |

**Sequencing recommendation.** R1, R2, R3, R4 and R7 are correctness bugs that do not need the framework — fix them in place first (they are five localized edits). Then extract `render.py`/`window.py` (mechanical, high duplication payoff). Then introduce `run_channel` and move Browser and YouTube onto it, which is where the exit-code gap closes. Delete X1/X2/X3/X4/X8 at any point. Leave Codex alone until the framework has adopted its transaction semantics.

---

## WHAT I COULD NOT VERIFY

1. **Runtime behaviour.** I executed no Python, no builds, and no tests, and made no database queries. Every claim is static reading. Actual row counts, duplicate distributions, and whether R1/R3 have already produced bad rows in `memory_bank` are unconfirmed.
2. **`quick_desktop_synth`'s writer.** `tests/test_monitor_jobs.py:283,286` expects it; I found no producer in `src/` or `scripts/`. The QD writers under `scripts/migrate/` are outside my scope, so the writer may exist there under a code path I did not read, or the monitor may be watching a dead source_type.
3. **The monitor itself.** `scripts/monitor_jobs.*` was not in scope, so "invisible to monitors" in R7 is inferred from `job_wrapper.sh:40-45` (notifies only on non-zero exit) and `ingest_staged.sh:65-74` (status-file fields). Which channels the monitor actually polls, and whether it reads `logs/last_ingest_status.json`, is unconfirmed.
4. **Codex `src/capture/sources/codex.py` lines 260-706.** I read the first 260 lines (SQLite compatibility gates, ownership classification, rollout JSONL fingerprint-based change detection). Attachment descriptor handling, `enumerate_tasks`, and `fetch_task` were not read, so the Codex row in the table describes `codex.py`'s orchestration and the adapter's front half only.
5. **`src/capture/codex.py` lines ~700-828.** Truncated in my read between `_render_turn`/`_capture_snapshot` and `_process_semantics`; the semantic-pass wiring I describe is from the surrounding code I did read.
6. **External source formats.** Whether `lastActivity` (R5), `receivedDateTime` (R3), QD `created_at` units (R8), and Graph `nextPageToken` echo behaviour (R9) actually take the problematic shapes depends on `slack-mcp` / `grasp-mcp` / Quick Desktop output I cannot inspect. Each is a latent risk in the code's handling, not a confirmed live failure.
7. **`docs/` and `.kiro/specs/`** were not read or touched, per instruction. Some "inaccuracy" findings (C6, C7) compare code against *code-embedded* documentation only; the authoritative specs may already say something different.
8. **Git history.** I did not run `git log`/`blame`, so "superseded by" claims in §4 rest on caller evidence (plists, `scripts/jobs/*.sh`, tests, grep) rather than commit chronology.

RELEVANT FILES (ranked by importance):
  1. src/ingest.py — The shared write path and the origin of the two highest-severity risks: chunk_by_section returns one unbounded chunk for blank-line-free bodies (Slack, YouTube), and parent+chunk writes are committed per-row with no transaction, so a mid-document failure is permanent and silently skipped thereafter. Also the site of the four swallowed exceptions in _discover_relationships and of the docstring claiming to be "the single path to PostgreSQL".
  2. src/capture/slack.py — The reference implementation for the proposed framework: the only channel with a pre-flight auth probe, a classified failure taxonomy mapped to exit codes, and an injectable call type for tests. Also carries the lexicographic timestamp comparison in _dm_inactive_for and the fixed-offset DST bug in day_bounds_utc.
  3. src/capture/browser.py — Furthest-from-contract of the in-src collectors: no exit codes (missing Chrome History exits 0 forever), a broad except that silently weakens internal-domain filtering, a sys.path mutation to import filters from a script, and startswith("ref") param dropping that collapses distinct URLs.
  4. src/capture/outlook.py — Contains the empty-timestamp source_url collapse (outlook://mail//<hash>) — the same empty-string identity bug shape as the crawlee incident — in both mail and event URL builders, and is the channel that has the reconnect fix but lacks Slack's pre-flight probe.
  5. src/capture/mcp_client.py — Defines the only real error taxonomy and exit-code contract in the layer (EXIT_AUTH=3 / EXIT_TRANSPORT=4, CaptureFailure, classify_error, bounded single reconnect) — the piece a shared framework would generalize to all channels. Also shows McpAuthError/McpTransportError are never caught by name.

NOTE: Line ranges have been extended by ~50 lines to provide additional surrounding context.

---

