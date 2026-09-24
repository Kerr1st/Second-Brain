# Track B — adversarial verification of the prior audit, sections 3–9

**Repo:** `/Users/kerrfir/second-brain` @ `6b8c2d2` (working tree has 87 uncommitted entries, none touched).
**Input under test:** `docs/AUDIT-2026-09-23.md` §3–§9 and the raw tracks `02-capture-and-ingest.md`, `04-data-layer-search-mcp.md`, `06-documentation.md`.
**Method:** every numbered/bolded claim re-opened at the cited file:line; every "dead" item grepped across `src/ scripts/ scheduling/ tests/ migrations/ config/`; live `memory_bank` queried read-only (SELECT / `\d` / catalog views only); `launchctl list`, `plutil`, `git log` read-only. No pytest, no jobs, no models, no writes except this file.
**Live corpus at time of check:** 124,257 `memories` rows (prior audit: 124,058), 23,383 `memory_relationships` (prior: 23,216).

Verdict vocabulary: **CONFIRMED** (claim and evidence hold), **REFUTED** (claim is wrong), **OVERSTATED** (mechanism real, consequence or severity too high), **UNDERSTATED** (worse than claimed), **UNVERIFIABLE** (could not be checked read-only).

Severity: S1 corrupts/hides data or spends unmetered · S2 rule unenforced or failure invisible · S3 duplication / dead weight / drift.

---

## 0. Headline

The prior audit's §3–§9 are **largely accurate on mechanism** (≈89 claims re-checked: 64 CONFIRMED, 7 REFUTED, 9 OVERSTATED, 6 UNDERSTATED, 3 unverifiable/not re-verified) but **unreliable on two things it did not check**: whether the "dead weight" it lists has *test* callers (7 of 24 items do), and whether its S1 identity bugs have *ever fired* (two have not; zero live rows). It also **missed the root cause behind its own §3 S1** — the 97 % expired-relationship figure is not "retired knowledge", it is `expire_stale_relationships` deleting edges minutes after creation because `last_accessed_at IS NULL` counts as stale — and missed that **the application role is a PostgreSQL superuser**, which is the actual enabling condition for the "test suite reconfigures production" S1. Two of the audit's headline S1s (migration 009, conftest) were already written down in `docs/VALIDATION.md:19,56` on 2026-09-10; the documentation track condemned that layer as "the source of essentially every false claim" without noticing it had correctly flagged them.

**New findings (M-1…M-14):** S1 × 5 (M-1, M-3, M-4, M-5, M-10) · S2 × 3 (M-2, M-6, M-9) · S3 × 6 (M-7, M-8, M-11, M-12, M-13, M-14). **Prior findings re-graded:** 1 upgraded to S1-understated (§9 fixture), 6 downgraded to S3 (4.1, 4.2, 4.5, 4.7, 3.8, 3.11), 1 kept S1 with the fix order inverted (3.2).

---

## 1. §3 — Retrieval and data-layer defects

| # | Claim (prior) | Prior sev | Verdict | My evidence | Corrected sev |
|---|---|---|---|---|---|
| 3.1 | `memory_graph` emits `r["target_id"]` unconditionally; inbound edges become self-links | S1 | **CONFIRMED** | `src/mcp_server.py:404` `{"target_id": str(r["target_id"]), …}`; `src/db.py:294-297` selects `m.title, m.type` via `CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END` and `WHERE r.source_id = %s OR r.target_id = %s` — both joined columns discarded. | S1 |
| 3.2 | 97 % of relationships expired; `get_relationships` serves them anyway | S1 | **CONFIRMED, cause UNDERSTATED** | Live: 22,461 / 23,383 expired (96.1 %). No `expired_at` in `db.py:289-299`. Only filter in `src/`: `express.py:242`. **But see M-1**: 22,163 of 22,300 expired `related_to` edges were expired < 7 days after creation (min age at expiry 12 min 28 s). Filtering `expired_at IS NULL` in `get_relationships` *as the prior fix recommends* would leave `memory_graph` with 922 live edges of 23,383 until the expiry rule is fixed first. | S1 (fix order matters) |
| 3.3 | Migration 009 hard-codes `memory_bank`; conftest applies it; test suite ALTERs production | S1 | **CONFIRMED, mechanism UNDERSTATED** | `migrations/009_hnsw_recall.sql:27` literal; `tests/conftest.py:89-101` applies `[0-9]*.sql` unconditionally. Live `pg_db_role_setting`: `memory_bank` and `second_brain_codex_test` have `hnsw.ef_search=200`, `memory_bank_test` does not, though it has `idx_memories_embedding … ef_construction='200'`. **The statement only succeeds because the `memory_bank` role is a superuser** (`pg_roles.rolsuper = t`) — see M-2. Also already documented in `docs/VALIDATION.md:19,56` and `docs/ARCHITECTURE.md:65` on 2026-09-10. | S1 |
| 3.4 | 10 live `correction_episode` rows unreachable via MCP type filter; writer unknown | S2 | **CONFIRMED; writer resolved** | Now **13** rows (2026-09-22..23), all `source_type='distilled_agent_task'`. Writer: `src/capture/codex.py:997-1005` via `agent_tasks.py:145` (`kind ∈ {decision, insight, correction_episode}`). `validation.py:20-23` omits it. Scheduled 6-hourly, so the unreachable set grows. Also `mem_class='source'` (128 rows) is `codex.py:552` (task parents). | S2 |
| 3.5 | `type='schema'` has 0 rows so `schema_context` can never fire | S2 | **CONFIRMED** | Live type distribution has no `schema`; `mcp_server.py:193-208` still runs `find_schemas_for_memories` on every search. | S2 |
| 3.6 | `memory_recent_activity` bypasses `validation.py`; different bounds; raw `int()` | S2 | **CONFIRMED** | `mcp_server.py:369,371`. `clamp_limit`/`clamp_days` `lo`/`hi` params (`validation.py:118,124`) have no caller. | S2 |
| 3.7 | `memory_brief` no `try/except ValidationError`; `memory_learn` unbounded topics | S2 | **CONFIRMED** | `mcp_server.py:504` raises through; `:448` `for topic in topic_list` with one `generate_embedding` + one `hybrid_search` each, no cap. | S2 |
| 3.8 | `_guarded` leaks `message_primary`; `OperationalError` text leaks host:port | S2 | **CONFIRMED** (low practical impact — local stdio client) | `mcp_server.py:53-54`. | S3 |
| 3.9 | 77,413 rows in `entities`/`entity_edges`/`memory_entities` with zero readers; ~16 MB of `idx_scan=0` indexes; no `parent_id` index | S2 | **CONFIRMED, slightly UNDERSTATED** | `pg_stat_user_indexes`: `memories_metadata_idx` 13 MB / 0, `idx_entities_name` 1.9 MB / 0, `idx_entities_embedding` 808 kB / 0, `idx_memories_encoding_context` 504 kB / 0, `idx_relationships_derived_from` 16 kB / 0. `pg_stat_database.stats_reset` is **NULL** for `memory_bank`, so "0 since reset" means 0 since statistics began — the prior hedge was unnecessary. `\d memories` shows no `parent_id` index. `entities` 14 MB + `entity_edges` 11 MB total relation size. | S2 |
| 3.10 | Live FTS index name `idx_memories_search_vector` exists in no file; test DB has `memories_search_vector_idx` | S2 | **CONFIRMED** | `\d memories` live vs `pg_indexes` on `memory_bank_test`. | S2 |
| 3.11 | `DB_POOL_MIN=1` defeats pooling | S2 | **CONFIRMED** | `db.py:38`; psycopg2 `_putconn` keeps at most `minconn` idle connections. | S3 (perf, not correctness) |
| 3.12 | `ingest_content` commits parent and chunks in separate transactions; partial doc is permanently skipped | S2 | **CONFIRMED** | `ingest.py:177,196` each call `create_memory` (`db.py:92-106`, own commit). `ingest_batch:222-226` skips on `source_url in already`. | S2 |
| 3.13 | `get_memories_by_ids` order-preservation only for canonical lowercase str ids | (04 track) | **CONFIRMED** | `db.py:137-138` keys on `str(r["id"])`. | S3 |
| 3.14 | `memory_bank_test.schema_migrations` empty; conftest half-applies non-idempotent files | S2 | **CONFIRMED** | `SELECT count(*) FROM schema_migrations` on test DB = 0. `conftest.py:91-101` one `execute` per file, swallows only `DuplicateTable`/`DuplicateObject`. | S2 |
| 3.15 | No SQL CHECK constraints / enums on `type`/`status`/`relation_type` | S2 | **CONFIRMED** | `\d memories`, `\d memory_relationships`: only PK/FKs and the search-vector trigger. | S2 |

**§3 missed (in scope): see M-1, M-2, M-3, M-6, M-7, M-8 below.**

---

## 2. §4 — "The crawlee bug has relatives"

| # | Claim | Prior sev | Verdict | My evidence | Corrected sev |
|---|---|---|---|---|---|
| 4.1 | `outlook.py` empty-timestamp collapses many mails onto `outlook://mail//<hash>`; "all but the first dropped" | S1 | **OVERSTATED** | Code confirmed: `outlook.py:109-110` `(… or "")[:19]`, `:153` same for events; `_hash` (`:105`) has no body. **Live: 0 rows** match `outlook://mail//%` or `outlook://event//%` (221 mail, 110 calendar rows). Graph always returns `receivedDateTime`. Latent handling defect, has never fired. Also: the prior says colliding mails are "dropped as already captured" — within a run that is via `already.add` (`outlook.py:227`), across runs via the DB set; correct but not data loss observed. | S3 → add guard, count skipped |
| 4.2 | `chat_extract.py:94` `conv_id[:12]` — prefix collision loses a session silently | S1 | **OVERSTATED, one aspect UNDERSTATED** | Code confirmed (`chat_extract.py:94,100`, `batch_ingest_staged.py:65-67`). CLI ids are UUID filenames (`cli_chat.py:85`); 12 hex chars = 48 bits; with ~3,400 sessions P(collision) ≈ 2×10⁻⁸. **Understated aspect**: the collision would already happen at `open(out_path, "w")` (`chat_extract.py:101-102`) — the second session *overwrites the staging file* before `batch_ingest_staged` ever sees it, so the "counted skipped / moved to ingested" narrative is the wrong failure path. Also `batch_ingest_staged.py:66` `f.stem.replace("cli_", "").replace("ide_", "")` strips *every* occurrence, not the prefix. Live: 3 duplicate `kiro_cli_chat` parent URLs exist but all are legacy-scheme (`cli_*.md`) rows from 2026-03-09, not prefix collisions. | S3 |
| 4.3 | `SKIP_PATTERNS` unanchored substrings (`login`, `signin`, `sim.amazon`…) applied to whole URL in two channels | S2 | **CONFIRMED** | `scripts/scrape_bookmarks.py:21-29`; `browser.py:106,132`. `browser.py:77-79` adds its own `_AUTH_PATH_RE` on top, so the over-skip is doubled. | S3 (drops articles; nothing corrupts) |
| 4.4 | `load_indexed_urls` regexes the whole `index.md`; must be unified with `crawlee.build_source_map` | S2 | **CONFIRMED** | `scrape_bookmarks.py:51-55` `re.finditer(r"\((https?://[^)]+)\)", f.read())`; `crawlee.py:113-137` parses per row with a `seen` set. | S3 |
| 4.5 | `canonical_url` drops any param whose name starts with `ref`; "two pages collapse to one `source_url`" | S2 | **OVERSTATED** | Mechanism at `browser.py:152` confirmed (`startswith((…, "ref", …))`). **Consequence wrong**: browser `source_url` is `browser://history/<day>` (`browser.py:214`); `canonical_url` only keys the per-day `pages` aggregation (`:220`) and the Crawlee read list. Nothing collapses at the identity layer. | S3 |
| 4.6 | `slack.py` compares `lastActivity` lexicographically to a formatted timestamp | S2 | **CONFIRMED (line drift)** | Now `slack.py:266-272` (`return la < since`). Prior cited 243-249. | S3 (latent; format unverified) |
| 4.7 | `browser.py:99-103` broad `except` falls back to `\.amazon\.com`; `*.a2z.com`/`*.aws.dev` reclassified `public` — "highest-consequence swallowed exception" | S2 | **REFUTED on consequence** | `browser.py:93` `_INTERNAL_SUFFIXES = (".amazon.com", ".a2z.com", ".aws.dev", ".amazon.work", ".amazon.dev", ".amazon-corp.com")` and `browser.py:142` `INTERNAL_DOMAINS.search(url) or host.endswith(_INTERNAL_SUFFIXES)`. Under the fallback, a2z/aws.dev hosts stay `internal`. Residual exposure is limited to the non-suffix entries: `quip-amazon.com`, `highspot`, `ivy-help-center`, `amazon-mentoring` (`scrape_bookmarks.py:33-40`); `amazon.awsapps` is caught by `_NOISE_HOST_RE` (`:61`). | S3 |
| 4.8 | `ingest.py:1` "single path" is false; `capture_api` writes `source_type` `"slack"`/`"email"`/`"browser_extension"` | S2 | **CONFIRMED** (not re-derived; live `source_type` distribution shows none of `email`/`browser_extension` yet — the API path has never been exercised in production) | Live GROUP BY source_type. | S3 until the API is used |

**§4 missed: see M-4 (in-run dedup gap that *did* produce live duplicates — the actual living relative of the crawlee bug), M-5.**

---

## 3. §5 — "One concept, many implementations"

| Row | Verdict | Evidence / correction |
|---|---|---|
| 19-column list ×3 | **CONFIRMED** | `db.py:127-132`, `db.py:163-167`, `search.py:22-27`. |
| Metrics dict `latency_s` vs `duration_s` | **CONFIRMED** | `agentic_cli.py:199`, `direct_api.py:167,201`. |
| Markdown header ×8; `ingest.py:56` only `-`→`_` | **CONFIRMED** | `ingest.py:55-56`. |
| Truncation ×6 | **UNVERIFIABLE as counted** | Three ellipsis idioms found by grep across `allocation/packet/schema/feedback`; the "six" depends on how one counts; not material. |
| Cost/price computation ×4 (`SpendTracker.record`, `runner.py`, `skim_probe`, `panel_replay`) | **CONFIRMED count, wrong list** | `grep price_for\|estimate_cost_usd`: `cost.py`, `src/skim/runner.py`, `scripts/skim_probe.py`, **`scripts/weekly_digest.py`**. `panel_replay.py` prices through `orch._spend` (`:213,225`), not its own arithmetic. |
| Arrivals filter SQL ×4; only one excludes dream-cycle output | **CONFIRMED** | `skim/store.py:29-38` excludes `dream-cycle` tag / metadata; `dream_cycle_db.py:978-993` does not; `skim_probe.py:59` does not. |
| Batch staged ingest ×3; "one writes a legacy URL scheme invisible to current dedup" | **UNDERSTATED** | It did not stay hypothetical: live `kiro_cli_chat` has **71 legacy-scheme parents** (`cli_*.md`, all 2026-03-09, 752 rows with chunks) and **68 of them have a `chat://<same id>` twin** — i.e. 68 CLI sessions are stored twice. See M-5. |
| Job wrapper ×17 | not re-verified (ops track). | — |
| Row→dict ×6, three date idioms | **CONFIRMED** | `mcp_server.py:158-164,180-186,200-206,349-355,380-383`; `db.py:236-238,329-334`. |
| `_record_call_metrics` ×3 (base, agentic_cli, kiro) | **OVERSTATED** | `base.py:44` defines `_write_metrics_line` (the implementation); `agentic_cli.py:55` and `kiro.py:59` are two thin re-exports kept for monkeypatch targets. Two wrappers over one impl, not three implementations. |
| Test doubles ×11 files | not recounted; plausible from grep of `Fake*` class names (`FakeStore` 3, `FakeInvoker` 3). | S3 |
| Timezones / DST fixed-offset | **CONFIRMED** | `slack.py:168`, `browser.py:224` `datetime.now().astimezone().tzinfo`. | S3 |

---

## 4. §6 — Structure

| Claim | Verdict | Evidence |
|---|---|---|
| LOC: orchestrator 1,201 / dream_cycle_db 994 / db 438; 64 src files 17,513 LOC; 79 test files 27,327 LOC | **CONFIRMED exactly** | `wc -l`. |
| `capture/*` ≈ 2,900 LOC | **UNDERSTATED** | `src/capture/*.py` + `sources/*.py` = 3,630. |
| Orchestrator imports `src.skim.store`/`cluster` directly, bypassing `runner`; `skim/__init__.__all__` exports neither | **CONFIRMED** | `orchestrator.py:46-48`; `skim/__init__.py:24-30`. |
| `panel_replay` mutates four orchestrator privates | **CONFIRMED** | `panel_replay.py:197,198,311,312`. |
| Five DB access styles; `db.py:155` `set_clause` interpolation behind allowlist | **CONFIRMED** | `db.py:141-159`; `conftest.py:65-67` raw autocommit; `codex.py` injected factory + `FOR UPDATE`; `express.py` cursor injection + second connection at `:385`. |

---

## 5. §7 — Dead weight, re-verified for callers **and tests**

Method: `grep -rn <symbol> src scripts scheduling tests migrations config` (excluding `.pyc`), then read the test hit to decide whether deletion breaks it.

| Item | Prior claim | Live callers (src/scripts/scheduling) | Tests that break on deletion | Verdict |
|---|---|---|---|---|
| `src/backends/codex.py` (620 LOC) | reachable only from tests | none: no profile uses `backend = "codex"`; `codex_local` absent from `backends.toml` | `tests/test_codex.py` (imports module), `tests/test_backends.py` | **CONFIRMED** (delete with its tests; keep `DEFAULT_ADAPTERS` consistent) |
| Bedrock `use_invoke_model` / `_invoke_model_fallback` / `system_prompt_delivery="prepend"` | unreachable via `ADAPTER_OPTIONS` | `resolver.py:51-53` exposes only `temperature`, `service_tier`, `max_tokens` of 8 kwargs (`bedrock.py:132-144`) | `tests/test_bedrock.py` constructs directly | **CONFIRMED** |
| Gemma branch + 2 price rows | no profile | none | `test_bedrock.py:129`, `test_skim_probe.py:258` | **CONFIRMED** |
| `_LEGACY_THINKING_BUDGETS` + Claude-4.x-on-Bedrock branch | dead | `bedrock.py:81,450`; no profile pins a 4.x model on `bedrock` | `test_bedrock.py` likely | **CONFIRMED** |
| `claude-opus-4` / `claude-sonnet-4` price rows | dead | **`claude-opus-4` is LIVE**: `price_for` is substring-match (`cost.py:44-52,108`); the `mini` profile (`backends.toml:53-58`) runs `claude_code` with `global.anthropic.claude-opus-4-8`, and Claude Code reports real usage (`claude_code.py:342-347`), so `SpendTracker.record` prices it from that row. `claude-sonnet-4` has no profile. | — | **REFUTED** for `claude-opus-4`; CONFIRMED for `claude-sonnet-4` |
| `DEFERRED` verdict machinery | 3 columns + `candidates_deferred=0` | `consensus.py:63,74` and `orchestrator.py:551` only *assert absence*; `migrations/003:42` comment | `test_consensus.py` (asserts never returned), `test_orchestrator_properties.py` ×6, `test_dream_cycle_db.py` ×2 reference the string | **CONFIRMED** dead as a state; column drop needs a migration + those tests updated |
| `panel_replay --prompt-variant standard` | both branches identical | `panel.py:159` `EVALUATOR_VERDICT_SCHEMA_V2 = EVALUATOR_VERDICT_SCHEMA`; `panel.py:253-263` returns `get_evaluator_prompt(...)` verbatim | `tests/test_prompts.py` ×2 | **CONFIRMED** |
| `scripts/ingest_chats.py` | no caller | none anywhere | none | **CONFIRMED** (and it produced the 68 live duplicates — M-5) |
| `scripts/batch_ingest_parallel.py` | no caller | none | none | **CONFIRMED** |
| `crawlee.parse_metadata_header`, `crawlee.read_source_file` | no caller | none (all `parse_metadata_header` imports resolve to `src.ingest`) | none | **CONFIRMED** |
| `scripts/run_golden_capture.py` | no caller | none | none | **CONFIRMED** |
| `db.get_schema_with_constituents` | dead fn + dead import | imported `mcp_server.py:28`, never called | none | **CONFIRMED** |
| `db.find_schemas_for_memory` | 0 callers | 0 in src | **`tests/test_db.py:186-187`** uses it as the oracle for `find_schemas_for_memories` | **OVERSTATED** — deleting it breaks a test; inline the oracle first |
| `dream_cycle_db.get_memory_provenance` (shim) | dead shim | **LIVE**: `src/dream_cycle/ledger.py:194` `dcdb.get_memory_provenance(all_ids)` | `test_allocation.py`, `test_skim_orchestrator.py` patch it | **REFUTED as dead** — it is a shim *with a caller*; retarget `ledger.py` then delete |
| `dream_cycle_db.get_previous_run_id` | superseded | none in src | `test_dream_cycle.py` ×10, `test_dream_cycle_db.py` ×8 (`mock_db.get_previous_run_id.return_value`) — MagicMock attributes, would not fail | **CONFIRMED** |
| `allocation.build_arrivals_inventory` / `compose_arrivals_slice` / `build_arrivals_slice` | test-only | none in src | `tests/test_allocation.py` ×14 | **CONFIRMED** test-only; deleting needs test deletion |
| `store.fetch_records`, `store.run_summary` | dead | none in src | `tests/test_skim_store.py:113-130` (`fetch_records`), `run_summary` ×4 + `test_codex_capture.py` | **OVERSTATED** — both are directly tested |
| `SkimResult.attempted`, `SkimResult.to_dict` | dead | `runner.py:73` property; `to_dict` used by `runner.py`, `faithfulness.py`, `skim_probe.py` (name shared with `schema.py`) | `test_skim_runner.py` ×2 | `.attempted`: CONFIRMED (0 refs); `.to_dict`: **REFUTED** — name is live in three modules (may be the schema's method; ambiguous) |
| `get_memory_stats()["type_distribution"]` | computed, never read | `dream_cycle_db.py` ×3 | `test_dream_cycle.py` ×17 build stats dicts with the key | **CONFIRMED** unread in src; tests carry the key |
| `codex_capture_canary.sh` + plist | "Referenced nowhere else in the repo" | plist has `/path/to/second-brain/…` at lines 10, 26, 28 (confirmed); not installed (17 of 18) | **`tests/test_codex_canary_job.py`** runs the script via `subprocess` and asserts exit 2 / stderr text | **REFUTED** on "nowhere" — delete the test with it |
| `scripts/backups/` 62 MB, `scripts/logs/` 65 files newest 2026-06-02 | orphaned, hidden by unanchored `.gitignore` | `git check-ignore -v` → `.gitignore:32 backups/`, `:33 logs/` | — | **CONFIRMED**; **plus `src/logs/llm_metrics/` (9 files, Jun 15–17, 1.9 MB) hidden by the same rule** — M-12 |
| `CONTAINER=` in 3 shell scripts | dead Docker vestige | `verify_liveness.sh`, `backup.sh`, `migrate.sh:7` | — | **CONFIRMED** |
| `BackendCapabilities.structured_output` never read | dead | `base.py:142,161-171`, `direct_api.py:124,289` reads `_shape_structured_output`, not the capability flag | `test_bedrock.py` ×16 | **CONFIRMED** (flag unread; tests assert it) |
| `src/backends/__init__.py` `__all__` "nobody imports it" | dead | **`tests/test_backends.py:11-16`** `from src.backends import (BACKEND_CAPABILITIES, …)` | breaks | **REFUTED** |
| `if TYPE_CHECKING: pass` in `bedrock.py:70` | dead | confirmed present | — | CONFIRMED |
| Non-executable `dedup_ide_backfill.sh`, `reindex_embedding.sh` in `scripts/jobs/` | move to maintenance | `-rw-r--r--` confirmed | — | CONFIRMED |
| 18 plists resolve; 17 installed; `launchctl` shows 17; `.dream/` neither tracked nor ignored | hygiene | `ls scheduling/*.plist`=18; `~/Library/LaunchAgents/com.second-brain.*`=17; `launchctl list` 17 labels, `weekly-digest` last exit 1; `git status` `?? .dream/` | — | **CONFIRMED** |

Net: **7 of 24 "no live caller" items have test callers the prior audit did not check** (`find_schemas_for_memory`, `get_memory_provenance` shim — which also has a *src* caller —, `fetch_records`/`run_summary`, `codex_capture_canary.sh`, `backends.__all__`, `build_arrivals_*`), and **one price row is live**. The dead-weight list is a good *candidate* list, not a *verified* one.

---

## 6. §8 — Documentation contested-fact table, cell by cell

| Fact | Layer-A cell | Verdict | Evidence (my read) |
|---|---|---|---|
| Adapters "3, Bedrock not in `DEFAULT_ADAPTERS`" | START-HERE:**18** (prior cited 19), SPEC-INDEX:20,28 | **CONFIRMED** (1-line drift) | `resolver.py:42-47` registers four incl. `"bedrock": BedrockInvoker`. MODEL-BACKENDS:21 correct. |
| Profiles "2" | START-HERE:**19-20** (prior 20-21) | **CONFIRMED** | `backends.toml` has `laptop, mini, bedrock, hybrid, controlled`; MODEL-BACKENDS:23 correct. |
| Roles "6" | START-HERE:19,**36** (prior 33); ARCHITECTURE:42,93 | **CONFIRMED** | `resolver.py:57-75` seven roles; `_validate_profile` requires all. |
| Plists "12" | OPERATIONS:56; START-HERE:**39,45** (prior 36,44); scheduling/README:5 ("all12") | **CONFIRMED** | 18 templates. |
| Dream cadence "Daily 12:00" | OPERATIONS:70 | **CONFIRMED** | plist `StartCalendarInterval` Weekday 1/3/5 Hour 2. |
| Run cap $5 / $6 / $9 | MODEL-BACKENDS:62 / backends.toml:119 / SKIM-STAGE:125, PLAN-panel-acceptance:284 | **CONFIRMED** | `dream_cycle_scheduled.sh:33` `:-9`; `cost.py:39` 5.0; `backends.toml:71` "default 5.00"; `PLAN-panel-acceptance:77` still says `=6`. |
| "no native Codex/Outlook collector" | CAPTURE-COMPONENTS:23 | **CONFIRMED false** | `src/capture/{codex,outlook}.py`; both plists installed and loaded. |
| "do not merge" remote lineage | README:**5** (prior said :12 — line 12 is the Model-backends table row) | **CONFIRMED** (line drift) | `git log`: `5386c2b` merge of `codex-capture-port` from `origin/main`. |
| Pipeline flow missing skim + grounding | ARCHITECTURE:17-20 | **CONFIRMED** | mermaid has no skim node. |
| Migration high-water 005 / 011 / 014 | START-HERE:45 / ARCHITECTURE:58-67 / code | **CONFIRMED, with nuance** | Live ledger: 001–014, and 006–011 were all applied **2026-09-14 14:57** — so START-HERE's "only 001–005" was *true when written on 09-10* and went stale four days later. ARCHITECTURE's table ends at 011 (line 67). |
| Consensus 3-of-4 | everywhere | **CONFIRMED** | `consensus.py:27`. |
| "27 redirect stubs (15 docs/, 18 user-guide, 1 scheduling)" | 06-track §4.2 | **REFUTED arithmetic** | 15 + 18 + 1 = 34, not 27; the track's own list names **16** `docs/` stubs. Measured: **35** files < 700 bytes carrying "Historical document preserved". |
| "~50 live docs, ~13 with content" | 06-track | roughly CONFIRMED | 51 `.md` in docs/ + user-guide (now incl. the 2 audit docs + PLAN-refactor) + README + scheduling/README; 35 stubs → 18 with content. |
| "33 orphans incl. PLAN-capture-repair" | 06-track §5 | **CONFIRMED (as of 09-23)** | Only `AUDIT-2026-09-23.md` and `PLAN-refactor.md` now link to it. |
| "zero broken internal links" | 06-track | **CONFIRMED** | 137 relative `](…)` targets across live docs (excluding audit files) all resolve. |
| "Layer A is the source of essentially every false claim" | 06-track §0 | **OVERSTATED** | Layer A **correctly** documents the two S1s the code audit "found": `VALIDATION.md:19` ("Migration 009 also targets `memory_bank` explicitly … run DB tests in a separate disposable PostgreSQL cluster"), `VALIDATION.md:56` ("test fixture has no production guard"), `ARCHITECTURE.md:65` ("hardcoded `ALTER DATABASE memory_bank`"). The failure is that documented hazards were never turned into fixes — a tracking gap, not a doc-accuracy gap. See M-9. |
| SPEC-INDEX "12 families" vs 13 dirs | 06-track §5 | **CONFIRMED** | `ls .kiro/specs` = 13; `direct-api-backends` untracked. |

---

## 7. §9 — Tests

| Claim | Verdict | Evidence |
|---|---|---|
| `test_db` session-scoped, not autouse; four module-level shadows never touch `DB_CONFIG`; code under test resolves `src.db.get_connection` → live DB when a file runs alone | **CONFIRMED, UNDERSTATED** | `conftest.py:103-104`; shadows at `test_enrich_qd_tags.py:225`, `test_ingest_eventlog.py:157`, `test_ingest_doc_chunks.py:157`, `test_ingest_interactions.py:122`; `scripts/migrate/ingest_eventlog.py:204` `with get_connection()`. **Worse**: `test_ingest_interactions.py:143-150` seeds `qd-feed://evt_00001..3` with `ON CONFLICT DO NOTHING` — there is **no unique constraint on `source_url`**, so that clause never fires and the fixture would *insert* into whichever DB it hits; and `qd-feed://evt_00001..8` are **real production event ids** (live titles: [real meeting title, redacted]). The E2E assertion at `:165-168` is `if row: assert …` — it passes even when the row is absent, i.e. even when the write went to the other database. The prior attribution note stands: prod `evt_00001..3` `interactions` metadata equals `SAMPLE_INTERACTIONS` verbatim, but `enrich_with_interactions`'s `UPDATE memories SET metadata` (`ingest_eventlog.py:228`) does not bump `updated_at`, so timestamps cannot distinguish a test write from the 2026-05-17 production run. **Unattributable either way.** | S1 |
| `_apply_migrations` three defects | **CONFIRMED** | see 3.14. |
| `chunk_by_section` zero test refs | **CONFIRMED** | `grep -rn chunk_by_section tests` → 0. |
| "zero skips, zero xfails" | **OVERSTATED** | No `skip`/`xfail` *markers*; but four runtime `pytest.skip("PostgreSQL not available")` paths exist in exactly the four shadowing files — a DB outage turns the shadow files green-by-skip. |
| `test_rerank.py:214-219` docstring omits `context_overlap`, `superseded_penalty`, `staleness_penalty` | **CONFIRMED** | read in place. |
| `test_rerank_drift.py:29` imports two constants it should pin | **CONFIRMED** | `from src.rerank_weights import TYPE_BOOST_TYPES, MEM_CLASS_BOOST`. |
| `validation.py` referenced by one test file | **CONFIRMED** | only `tests/test_mcp_server.py` imports it. |

**§9 missed: M-10 (`TEST_DB_NAME` has no production guard; `clean_tables` is `DELETE FROM memories`).**

---

## 8. What §3–§9 missed, in their own scope

Each: severity · location · one-line evidence · fix.

**M-1 — S1 — `expire_stale_relationships` destroys the relationship graph as fast as it is built.**
`src/dream_cycle_db.py:835-858`, called every run at `orchestrator.py:566-567`. Rule: expire when *neither* endpoint has `last_accessed_at > now() - 90d`; `last_accessed_at` is set only by `search.increment_access_count` (MCP search), so a **NULL** counts as stale with no `created_at` grace. Live: `related_to` 22,300 expired, **22,163 within 7 days of creation, min age 12 min 28 s**; `derived_from` 88 expired, 81 within 7 days, **79 of them Codex `distilled_agent_task` provenance edges** (`codex.py` `derived_from`, which the codex tests assert `expired_at IS NULL` on). Every ingest-time `related_to` from `_discover_relationships` is expired on the next Mon/Wed/Fri. This, not "retired knowledge", is the 97 %. *Fix:* require `created_at < now() - threshold` **and** both endpoints unaccessed; never expire `derived_from` / `superseded_by` (provenance); then — and only then — filter `expired_at IS NULL` in `get_relationships` (prior fix 3.2). A backfill un-expiring edges whose `expired_at - created_at < threshold` is cheap and reversible.

**M-2 — S2 — The application role is a PostgreSQL superuser.**
`SELECT rolsuper FROM pg_roles WHERE rolname='memory_bank'` → `t`; it also owns all three databases. Every job, the MCP server, `capture_api`, and pytest connect as it (`db.py:16-22`, `conftest.py:52-58`). This is *why* 3.3 works (`ALTER DATABASE` needs ownership or superuser) and why any SQL-shaped mistake anywhere is unbounded. Not mentioned in any audit track. *Fix:* `ALTER ROLE memory_bank NOSUPERUSER` after confirming migrations run as an owner role; give tests their own role that owns only `memory_bank_test`.

**M-3 — S1 — `memory_brief(use_llm=True)` is a client-triggerable, unmetered Kiro call over corpus content.**
`mcp_server.py:481-507` passes `invoker=None` when `use_llm` is true → `express.py:480-483` `AgentInvoker()` (Kiro, `DEFAULT_MODEL = "claude-opus-4.8"`, `kiro.py:43`) with briefing titles/content in the prompt; no `SECOND_BRAIN_PROFILE`, no `SpendTracker`, `except Exception` at `:496` degrades silently. The prior audit §1 attributed this to `express.py`; §3's MCP validation table (04-track §5, "all 10 tools") lists `memory_brief` as merely "no error handling" and misses that the tool exposes the corpus-to-Kiro path to *any attached agent*. *Fix:* route through `default_resolver()` with a role, or drop `use_llm`.

**M-4 — S1 — `ingest_batch` / `ingest_content` have no in-run dedup; the same URL twice in one run is ingested twice — and it happened.**
`ingest.py:222-238`: `already` is read once and never extended. Callers that dedupe in-run (`youtube.py:203`, `outlook.py:227`) are safe; `crawlee_ingest.py:57-73` relies on `crawlee.parse_all`'s row-level `seen` set (added 2026-09-22). Live residue: `https://www.youtube.com/watch?v=PSROJpBIZ4w` has **10 parent rows as `source_type='article'`** (30 rows with chunks), **7 created 2026-09-20 03:00–03:01** in one bookmarks/crawlee run; a second video ×2; `distilled_chat` 10 URLs ×2. `cleanup_duplicate_sources.py` was applied (HEAD message; no `memories_dedup_backup_*` tables remain) yet these 10 survive its own `GROUP_SQL` (one row is cited by a `dream_cycle_candidates` row). *Fix:* `already.add(source_url)` after a successful `ingest_content`; a `UNIQUE (source_type, source_url) WHERE parent_id IS NULL` partial index would have made every dedup bug in this audit impossible — nothing in `\d memories` enforces identity today.

**M-5 — S1 (historical, still live) — 68 CLI chat sessions are stored twice under two URL schemes.**
Live: 71 `kiro_cli_chat` parents with `source_url` like `cli_<id>.md` (all 2026-03-09, written by the dead `scripts/ingest_chats.py:41`), and **68** of them also exist as `chat://<id>` (752 rows including chunks). Prior §5 called the legacy scheme "invisible to current dedup" as a risk; it is a realised duplicate set that inflates retrieval for those sessions and was outside `cleanup_duplicate_sources.py`'s `('article','youtube')` scope. *Fix:* extend the cleanup to `kiro_cli_chat` keyed on normalised id; then delete `ingest_chats.py`.

**M-6 — S2 — `memory_read` returns the `search_vector` tsvector to the client.**
`mcp_server.py:228` skips only `"embedding"`; `get_memory` is `SELECT *` (`db.py:113`), so every `memory_read` ships the weighted tsvector (kilobytes of lexemes) into the agent's context. *Fix:* use the shared scalar column list (prior 3.5 fix) in `get_memory`.

**M-7 — S3 — 4 `kiro_cli_chat` parents have `source_url IS NULL`.**
Live count; the 02-track predicted this ("if a caller ever omits `source_url` the row lands NULL") as Medium/latent. It has happened; those four are un-deduplicable forever. *Fix:* `ingest_content` should refuse `source_url is None` for channel sources.

**M-8 — S3 — `enrich_with_interactions` writes `metadata` without bumping `updated_at`.**
`scripts/migrate/ingest_eventlog.py:228` `UPDATE memories SET metadata = %s WHERE id = %s`; the only trigger (`trg_memories_search_vector`) fires on `title, content`. Consequence: no audit trail for metadata edits (this is what made the §9 attribution question unanswerable). *Fix:* route through `db.update_memory`, which sets `updated_at = now()`.

**M-9 — S2 — Documented hazards were not tracked to closure; the doc audit blamed the wrong layer.**
`docs/VALIDATION.md:19,56` (2026-09-10, layer A) already states migration 009 targets `memory_bank`, that the test fixture has no production guard, and prescribes a disposable cluster. `docs/ARCHITECTURE.md:65` records the hardcoded `ALTER DATABASE`. Thirteen days later both are still open and were re-discovered as S1s. The 06-track's thesis ("Layer A is the source of essentially every false claim") is therefore overstated, and PLAN-refactor should credit and close these rather than re-file them. *Fix:* a "known hazards" list with owners in VALIDATION.md, checked by `test_repo_hygiene.py`.

**M-10 — S1 (latent) — `TEST_DB_NAME` has no production guard and `clean_tables` is `DELETE FROM memories`.**
`conftest.py:48` `TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "memory_bank_test")`; `:143-146` `DELETE FROM memory_relationships; DELETE FROM memories …; DELETE FROM memories` after every test using the fixture. `TEST_DB_NAME=memory_bank pytest` (or a `.env` that sets it) would truncate the live corpus with the superuser role (M-2). VALIDATION.md:19 knew. *Fix:* `assert TEST_DB_NAME != db.DB_CONFIG["dbname"]` and `assert TEST_DB_NAME.endswith("_test")` at import.

**M-11 — S3 — `memory_relationships` PK is directional; `related_to` is stored both ways.**
`\d memory_relationships`: PK `(source_id, target_id, relation_type)`, so A→B and B→A `related_to` are distinct rows and `memory_graph` (once fixed per 3.1) will return the same neighbour twice. *Fix:* canonical ordering for symmetric types at write time.

**M-12 — S3 — `src/logs/llm_metrics/` is a stray output directory inside the source tree.**
9 `.jsonl` files (Jun 15–17, incl. a 917 kB `adhoc.jsonl`), untracked, hidden by the unanchored `.gitignore:33 logs/`. Same class as `scripts/logs/` and `scripts/backups/`, not listed by §7. Origin: a `_REPO_ROOT` computed from a different cwd. *Fix:* delete; anchor `.gitignore` patterns with a leading `/`.

**M-13 — S3 — 02-track's open question `quick_desktop_synth` is resolved: it is a monitor *label*, not a `source_type`.**
`scripts/jobs/verify_liveness.sh:66` maps `quick_desktop_synth` to the predicate `source_type LIKE 'quick_desktop_%' AND source_type NOT IN ('quick_desktop_doc', …)`; `tests/test_monitor_jobs.py:283,286` test that label. No writer is missing. Close the item.

**M-14 — S3 — `get_arrivals_summary` counts every `distilled_agent_task` memory as a "document".**
`db.py:231` `count(*) FILTER (WHERE parent_id IS NULL)`; Codex derived memories are all `parent_id IS NULL` sharing one `codex://<task>` URL (one task → up to 11 rows, live). `memory_recent_activity` therefore over-reports Codex "documents" ~5–10×. *Fix:* count `DISTINCT source_url` for that channel, or give derived memories a parent.

---

## 9. Corrected severities — where I moved the needle

| Prior finding | Prior | Corrected | Why |
|---|---|---|---|
| Outlook empty-timestamp URL collapse | S1 | S3 | 0 live rows in 331; Graph always supplies the field. Latent guard, not data loss. |
| `conv_id[:12]` identity | S1 | S3 | UUID prefix, P ≈ 2×10⁻⁸ at current volume; 0 live collisions. Still worth fixing as a smell. |
| `browser.py` broad-except → internal hosts go public | S2 | S3 | `_INTERNAL_SUFFIXES` fallback at `:93,142` covers a2z/aws.dev/amazon.com. |
| `canonical_url` `ref*` → "collapse to one source_url" | S2 | S3 | Wrong layer; source_url is per-day. |
| `_guarded` / `OperationalError` leaks | S2 | S3 | Local stdio; nothing an attached agent could not already run. |
| `DB_POOL_MIN=1` | S2 | S3 | Performance; correctness unaffected. |
| "97 % expired, served anyway" | S1 | S1 **but fix order inverted** | Root cause is M-1; filtering first would blank the graph. |
| Dead-weight list | — | 7 of 24 items need test changes; 1 item live | See §5. |
| "27 stubs" | — | 35 | Arithmetic. |
| "zero skips" | — | 4 runtime skips | In exactly the four unsafe files. |

New S1s: M-1, M-3, M-4, M-5, M-10, and §9's fixture problem UNDERSTATED (real production ids in the seed).

---

## Could not verify

1. **Whether any test run has ever written to `memory_bank`.** The `qd-feed://evt_00001..3` rows carry `interactions` identical to `tests/test_ingest_interactions.py:14-36`, but `updated_at == created_at` for all 323 enriched rows because the enrich `UPDATE` does not touch `updated_at` (M-8); the sample may simply have been copied from the real interactions log. Unattributable without `pg_stat_statements` or WAL history.
2. **Why the 10 residual `PSROJpBIZ4w` parents survived `cleanup_duplicate_sources.py --apply`.** One row is cited by a `dream_cycle_candidates` row; the script's exemption is per-row, so 8 others should have gone. Backup tables were dropped (HEAD `6b8c2d2`), so the run's actual `doomed` set cannot be reconstructed.
3. **Runtime behaviour of `memory_graph`, `memory_brief(use_llm=True)`, and the fixture shadowing** — all inferred from code and catalog state; invoking any of them writes (`access_count`, Kiro spend, or DB rows).
4. **Whether `origin/main` has further unported divergence**; not fetched.
5. **The provenance of `second_brain_codex_test`** (owned by `memory_bank`, carries `hnsw.ef_search=200`); no reference in the repo.
6. **Slack `lastActivity` format and Graph `receivedDateTime` omission rates** — depend on MCP server output not inspectable here; both 4.1 and 4.6 remain latent-by-code.
7. **§5 job-wrapper (×17) and §7 "all 18 plists resolve"** were spot-checked via `plutil` for the `ProgramArguments` path only, not for `WorkingDirectory`/env correctness.
8. **`pg_stat_user_indexes` counters** are cumulative since the database was created (`stats_reset IS NULL`), but a crash-recovery would also zero them; I found no evidence of one.
