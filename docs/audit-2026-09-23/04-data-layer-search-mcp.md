## review_data_layer

=== Context Gathering Summary ===
FOUND: Live schema of memory_bank (11 tables, memories 21 columns / 13 indexes, memory_relationships) and memory_bank_test; schema_migrations ledger state for both databases; all 15 migration files plus migrate.sh; src/db.py, src/search.py, src/validation.py, src/models.py, src/rerank_weights.py, src/embeddings.py, src/mcp_server.py (all 10 tools), src/classify.py; express.py DB access surface; tests/conftest.py migration runner; tests/test_rerank.py weight oracle and tests/test_mcp_server.py coverage map; live vocabulary distributions for type/status/relation_type/mem_class; pg_stat_user_indexes scan counts; pg_db_role_setting hnsw configuration; scripts/jobs/mcp_serve.sh and reindex_embedding.sh.
NOT FOUND: Origin of the 10 correction_episode rows and 117 mem_class='source' rows (writer lives outside the assigned scope); pg_stat reset timestamp, so index scan counts are only "since reset"; src/ingest.py chunk_by_section (out of scope, needed to confirm the OVERLAP_WINDOW_CHARS comment); docs/ and .kiro/specs/ content (excluded by instruction), so no code-vs-design cross-check; provenance of the third database second_brain_codex_test; runtime confirmation of the memory_graph inbound-edge bug and of conftest re-issuing ALTER DATABASE (both would require writes).
====================================

=== CONTEXT GATHERING COMPLETE ===

ANSWER:
# Storage / Search / Interface Layer Review — second-brain @ 6b8c2d2 (read-only)

Verified against live `memory_bank` (124,058 memory rows), `memory_bank_test`, migrations 000–014, and the code. No files, git state, or database rows were modified; all SQL was SELECT / `\d` / `pg_settings` / `pg_stat_user_indexes`.

Three premises in the brief turned out to be wrong and are corrected below, because they change where the real problems are:
- `src/validation.py` exports **`uuid_or_error` / `clamp_limit` / `clean_tags`** (no leading underscores) and **does not contain `_guarded()`** — `_guarded` lives at `src/mcp_server.py:45`.
- `src/models.py` declares **no** type/status/relation vocabulary; it is dream-cycle dataclasses. The competing vocabulary is in **`src/classify.py:10-13`**.
- The migration **ledger is healthy** in `memory_bank`. The ledger problem is in `memory_bank_test`.

---

## 1. SCHEMA VS CODE DRIFT

### 1.1 Live FTS index name exists nowhere in the repo — HIGH
`migrations/001_initial_schema.sql:36` is `CREATE INDEX ON memories USING gin (search_vector)` with no name, so Postgres auto-names it `memories_search_vector_idx`. That is exactly what `memory_bank_test` has. Live `memory_bank` instead has **`idx_memories_search_vector`** (100 MB, 94 scans). The string `idx_memories_search_vector` appears in **no** file under `migrations/`, `src/`, `scripts/`, `tests/`, or `docs/` — it was created or renamed out-of-band.

Evidence: `\d memories` on `memory_bank` vs `pg_indexes` on `memory_bank_test`; `grep -rn idx_memories_search_vector migrations scripts src tests docs` → no matches; no `ALTER INDEX … RENAME` in migrations (the only rename in the repo is `scripts/jobs/reindex_embedding.sh:29`, for the *embedding* index).

Impact: a rebuild from migrations produces a differently-named index than production. Any future `DROP INDEX idx_memories_search_vector` is a silent no-op on a fresh database — the same failure mode `migrations/009_hnsw_recall.sql:19-20` already had to defend against for the HNSW index.

Fix: name the index explicitly in 001 and add an idempotent migration 015: `ALTER INDEX IF EXISTS memories_search_vector_idx RENAME TO idx_memories_search_vector;`.

### 1.2 Migration 009 hardcodes the database name and mutates production from the test suite — HIGH
`migrations/009_hnsw_recall.sql:27`: `ALTER DATABASE memory_bank SET hnsw.ef_search = 200;`

`tests/conftest.py:84-101` (`_apply_migrations`) applies **every** `migrations/[0-9]*.sql` to the test database unconditionally. Statement 009:27 names `memory_bank` explicitly, so running it while connected to `memory_bank_test` alters **production** configuration.

Evidence: `pg_db_role_setting` shows `hnsw.ef_search=200` on `memory_bank` and `second_brain_codex_test` but **not** on `memory_bank_test`, even though `memory_bank_test` has `idx_memories_embedding` with `{m=32,ef_construction=200}` — i.e. the rest of 009 did run there.

Two consequences: the test suite issues an `ALTER DATABASE` against production, and recall-sensitive tests run at the default `ef_search=40` while production runs at 200, so `tests/test_rerank_drift.py` measures a different retrieval regime than it claims to guard.

Fix: `DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search = 200', current_database()); END $$;`.

### 1.3 Ledger state
`memory_bank.schema_migrations` — 14 rows, `001_…` through `014_…`, exactly matching the files on disk. `000_migrations_table.sql` is absent by design (`migrations/migrate.sh:19` skips it). **No drift.**

`memory_bank_test.schema_migrations` — **0 rows**, while all 11 tables and 13 `memories` indexes exist. MEDIUM. Cause: `tests/conftest.py:84-101` applies the files directly and never writes to `schema_migrations`. Nothing can determine which migrations the test database has. Fix: reuse `migrate.sh` semantics, or record versions in `_apply_migrations`.

### 1.4 conftest's migration runner half-applies new migrations — MEDIUM
`tests/conftest.py:91-101` executes each file as a single `cur.execute(sql)` and swallows only `DuplicateTable` and `DuplicateObject`. On a pre-existing test DB, 001 aborts at its first `CREATE TABLE` and the entire rest of the file — 7 indexes, the trigger function, the trigger — is skipped. Any future migration mixing idempotent and non-idempotent statements will half-apply and then raise an uncaught error.

### 1.5 Columns the code uses — all present, no phantoms
`_LIST_COLUMNS` (`src/db.py:163-168`) and `_CANDIDATE_COLUMNS` (`src/search.py:22-27`) each enumerate the same 19 scalar columns; every one exists in live `memories`. `rrf_raw` / `rrf_score` (`src/search.py:158-160`) are computed in Python, not columns. **No column or table is read that a migration does not create.**

### 1.6 Migration artifacts the code never uses
| Artifact | Migration | Live state | Reader in `src/` |
|---|---|---|---|
| `entities` | 008:10 | 29,642 rows | **none** |
| `entity_edges` | 008:30 | 45,776 rows | **none** |
| `memory_entities` | 008:50 | 1,995 rows | **none** |
| `express_feedback` | 010:8 | **0 rows** | `src/express.py:75,92,106` |
| `memories.confidence` | 001:15 | all default 1.0 | selected, never written by MCP, never read by rerank |
| `memories.summary` | 001:8 | — | selected everywhere; only `memory_read` surfaces it |

The knowledge graph (008) is written only by `scripts/migrate/migrate_quick_desktop.py:230,245`. Nothing in `db.py`, `search.py`, `express.py`, or the MCP surface joins it. MEDIUM — ~2.7 MB of `entities` indexes plus a 45k-edge graph with zero readers.

### 1.7 `memory_relationships.expired_at` is honoured in exactly one place — HIGH (correctness)
`migrations/003_dream_cycle.sql:61` adds it; `src/dream_cycle_db.py:849-850` sets it; **22,461 of 23,216 rows (97%) are expired**. Only `src/express.py:242` filters `expired_at IS NULL`. `src/db.py:289-296` (`get_relationships`, the data source for `memory_graph`) and `src/db.py:359/378/400` (schema lookups) do not. So `memory_graph` hands agents overwhelmingly expired edges with no indication they were retired.

Fix: filter `expired_at IS NULL` by default in `get_relationships`, with an `include_expired=False` parameter for the dream cycle.

### 1.8 Unused indexes (live `idx_scan = 0`) — MEDIUM
- `memories_metadata_idx` (001:41) — **13 MB**, 0 scans. Express's `metadata->>'depth_score'` / `metadata->>'express_last_surfaced'` predicates (`src/express.py:282,290-291`) are expression predicates a plain `gin(metadata)` cannot serve.
- `idx_memories_encoding_context` (006:21) — 504 kB, 0 scans. Nothing full-text searches `encoding_context`; `src/search.py:239` reads it off the already-fetched row.
- `idx_relationships_derived_from` (007:23) — 16 kB, 0 scans, despite `migrations/007_schema_type.sql:30`'s comment claiming it serves the schema lookups. The planner uses `memory_relationships_pkey` for `src/db.py:378,400`.
- `entities`/`entity_edges` index set: `idx_entities_embedding` (808 kB) and `idx_entities_name` (1,872 kB) both at 0.

~16 MB of index maintained on the hot write path for nothing.

### 1.9 Missing index — LOW-MEDIUM
No index on `memories.parent_id`, despite the self-FK and two hot predicates: `WHERE parent_id IS NULL` (`src/db.py:231`, `get_arrivals_summary`, driving `memory_recent_activity`) and the per-parent dedup key (`src/search.py:151`).

---

## 2. DB ACCESS STYLES — five distinct ways

**Style 1 — pooled context manager, function-owns-connection (canonical).** `src/db.py`, `src/search.py`, `src/skim/store.py`, `src/dream_cycle_db.py`. `with get_connection() as conn: with conn.cursor(…) as cur:`, `%s` params, explicit `conn.commit()` on writes, reads never commit.

**Style 2 — cursor injection.** `src/express.py` `_gather_*(cur, …)` at lines 165, 229, 272, 314, 340, 571 receive a cursor from `compose_briefing`'s single connection (375-382). Different ownership model. Worse: `load_prefs()` is called at `express.py:385`, **after** that `with` block closes, opening a second connection for the same logical operation.

**Style 3 — injected connection factory with row locking and manual rollback.** `src/capture/codex.py:218` takes `connect=db.get_connection`; uses `SELECT … FOR UPDATE` (526/531, 761, 922) and explicit `connection.rollback()` (591, 649, 781, 1067). The only place in the codebase with real row locks.

**Style 4 — raw `psycopg2.connect` with `autocommit=True`, bypassing the pool.** `tests/conftest.py:63-71`.

**Style 5 — shell/psql.** `migrations/migrate.sh`, `scripts/jobs/reindex_embedding.sh`.

**Cursor factories:** default tuple cursor for writes and scalar reads (`db.py:96,156,245,307`; `search.py:342`) vs `RealDictCursor` for row reads (`db.py:112,122,191,…`). Two row shapes inside one module — `db.py:100` indexes `cur.fetchone()[0]`, `db.py:139` indexes `r["id"]`.

**Connection leaks: none found.** `src/db.py:62-63`'s `finally` always calls `putconn`. `_pool.getconn()` at `db.py:45` is outside the `try`, so a pool exhaustion raises `psycopg2.pool.PoolError` straight out of `get_connection` — I confirmed `PoolError` is a `psycopg2.Error` subclass, so it is *not* swallowed by the loop's handler (correct), but it also is not translated at the MCP boundary (see §5).

**Pooling is defeated by its own defaults — MEDIUM.** `DB_POOL_MIN` defaults to `1` (`src/db.py:37`). psycopg2's `_putconn` only returns a connection to the pool while `len(self._pool) < minconn`; every connection past the first is **closed** on release. Combined with the unconditional `SELECT 1` probe on every checkout (`db.py:46-48`), each concurrent call pays a fresh TCP connect plus an extra round trip. The docstring at `db.py:31-35` ("Check out a live connection from the pool") describes a 1-connection cache.

**Transaction discipline — fragile but correct.** Read paths never commit or rollback; correctness depends on psycopg2's `putconn` resetting the connection. `SET LOCAL hnsw.iterative_scan = relaxed_order` (`src/search.py:79`) silently relies on the implicit transaction psycopg2 opens — it becomes a no-op the moment anyone sets `autocommit=True`. LOW, worth an explicit transaction block or a comment.

**String-built SQL:**
- `src/db.py:155,159` — `set_clause` is f-string-interpolated into `UPDATE memories SET {set_clause}`. Not injectable, because keys are filtered through `ALLOWED_UPDATE_FIELDS` (`db.py:141-146`) and values are parameters. But the safety rests entirely on that allowlist with no identifier quoting. `src/search.py:22-27` already uses `psycopg2.sql.Identifier` correctly — `update_memory` should too.
- `src/db.py:188/194` and `db.py:261/266` join literal `"col = %s"` fragments; values parameterised. Safe.
- `src/search.py:76-134` uses `psycopg2.sql.SQL` / `Identifier` throughout. **This is the good pattern to standardise on.**
- `migrations/migrate.sh:21,29` interpolate `$VERSION` into SQL string literals. Input is a repo-controlled glob, so not attacker-controlled, but a filename containing an apostrophe breaks the runner. LOW.
- Out-of-scope but same class: `scripts/cleanup_duplicate_sources.py:114,119` (table name into DDL), `scripts/eval/recall_check.py:36-37`.

**No SQL injection vector found in the assigned modules.** Every caller-supplied value reaches Postgres as a bound parameter, including the `'; DROP TABLE memories; --` case that `tests/test_mcp_server.py:696` already covers.

---

## 3. DUPLICATED SQL

**(a) Three declarations of "all scalar columns of `memories`" — HIGH (maintenance)**
- `src/db.py:163-168` `_LIST_COLUMNS` (string)
- `src/search.py:22-27` `_CANDIDATE_COLUMNS` (`sql.Identifier` composition)
- `src/db.py:127-133` inline literal inside `get_memories_by_ids`

All three enumerate the identical 19 columns. `src/search.py:20` even says "Keep in sync with migrations/" — an explicit admission. Migration 015 adding a column means editing three places.
**Canonical:** one `MEMORY_SCALAR_COLUMNS` tuple rendered via `sql.SQL(", ").join(sql.Identifier(...))`, imported by all three.

**(b) `SELECT *` contradicting the layer's own stated rule — MEDIUM**
`src/db.py:113` (`get_memory`), `db.py:266-270` (`search_similar`), `db.py:359` / `db.py:378` / `db.py:400` (`m.*` in the three schema queries) all pull `embedding` (1024 floats) and `search_vector` back — directly contradicting the rationale written at `db.py:119-121` and `search.py:19-21`. The `m.*` variant runs on **every** MCP search via `find_schemas_for_memories` (`src/mcp_server.py:196`).
**Canonical:** the shared column list from (a); add `embedding` only where genuinely needed.

**(c) Two schema lookups, one query — MEDIUM**
`src/db.py:369-385` `find_schemas_for_memory` and `db.py:388-412` `find_schemas_for_memories` are the same SELECT, singular vs `= ANY`. The singular has **zero callers**.
**Canonical:** `find_schemas_for_memories`; delete the singular.

**(d) `get_memory_provenance` defined twice — LOW**
`src/db.py:415` and a pass-through shim at `src/dream_cycle_db.py:968-969`.
**Canonical:** `src/db.py`; `src/dream_cycle/ledger.py:194` should import it directly.

**(e) Two contradictory UUID-list coercions — MEDIUM**
`src/db.py:421-426` silently drops invalid ids; `src/validation.py:57-70` `uuid_or_error` raises. Same job, opposite contracts.
**Canonical:** `validation.uuid_or_error`, plus an explicit `filter_valid_uuids` if silent-drop is genuinely wanted.

**(f) Row→object mapping repeated six times — MEDIUM**
The `{"id": str(...), "title", "type", "created_at": <iso>}` projection appears at `src/db.py:236-238`, `db.py:329-334`, `src/mcp_server.py:158-164`, `mcp_server.py:180-186`, `mcp_server.py:200-206`, `mcp_server.py:349-355`. Three different date idioms coexist: the `_iso()` helper (`mcp_server.py:380-383`), inline `hasattr(v, "isoformat")` (`mcp_server.py:184`, `db.py:238`), and raw datetime passthrough (`db.py:332`).
**Canonical:** one `_iso` and one `memory_summary(row)` serialiser in the data-access layer.

**(g) Four "recent memories by filter" queries — LOW**
`src/db.py:191-196` `list_memories` and `src/express.py:279-297 / 316-324 / 342-351` all reduce to "memories WHERE status/type/source_type/date ORDER BY … LIMIT". Express's predicates are specialised enough that collapsing is optional.

---

## 4. CONTRADICTIONS AND INACCURACIES

### 4.1 Vocabularies declared in five places
1. `src/validation.py:29-32` `MEMORY_TYPES` (10), `:38` `FILTERABLE_TYPES` (+`schema`), `:40` `MEMORY_STATUSES`, `:42-45` `RELATION_TYPES`
2. `src/classify.py:10-13` `SEMANTIC_TYPES` = {idea, synthesis, insight, decision, connection, priority, project, question} — **a fifth set that omits `research` and `source`**
3. `src/rerank_weights.py:33` `TYPE_BOOST_TYPES` (4) — a deliberate subset
4. `tests/test_mcp_server.py:577-582` re-declares all four as literals (intentional oracle)
5. `src/models.py:39` `CandidateInsight.type` carries a comment-only vocabulary `# insight, connection, question, synthesis`

**SQL CHECK constraints / enums: none.** Confirmed by `\d memories` and `\d memory_relationships` — only FKs and the pkey. `src/validation.py:12-15`'s claim is **accurate**. The consequence is that `validation.py` is the sole gate and is bypassed by every non-MCP writer: `src/ingest.py`, `src/capture/*`, `src/dream_cycle_db.py`, `scripts/migrate/*` all call `db.create_memory` directly.

### 4.2 Live data vs the declared vocabularies
| Value | Live count | In `MEMORY_TYPES`? | Consequence |
|---|---|---|---|
| `source` | 118,783 | yes | — |
| `insight` | 2,754 | yes | — |
| `decision` | 2,324 | yes | — |
| `research` | 139 | yes | — |
| `synthesis` | 27 | yes | — |
| `connection` | 20 | yes | — |
| **`correction_episode`** | **10** | **NO** | **unreachable via MCP** |
| `question` | 1 | yes | — |
| `idea`, `priority`, `project` | **0** | yes | declared, never used |
| `schema` | **0** | filter-only | feature can never fire |

- **`correction_episode` — HIGH.** Not in `MEMORY_TYPES` and not in `FILTERABLE_TYPES` (`validation.py:29-38`), so `memory_search(type='correction_episode')` and `memory_list(type='correction_episode')` are both rejected with "type must be one of: …". Those 10 rows are unreachable by type filter through the MCP surface. Written by a non-MCP path.
- **`schema` has 0 rows — MEDIUM.** Yet `FILTERABLE_TYPES` carries it, `migrations/007_schema_type.sql:17` built a partial index for it (586 scans, 8 kB), and the entire `schema_context` block at `src/mcp_server.py:193-208` always returns `[]`. The `memory_search` docstring at `mcp_server.py:126-127` advertises `schema_context` ("higher-level notes the top hits belong to") as a live feature that cannot fire.
- **`status`:** only `active` (124,051) and `superseded` (7). `archived` is declared (`validation.py:40`) and unused.
- **`relation_type`:** all 6 live values are in `RELATION_TYPES`; `inspires` declared with 0 rows. **Full agreement.**
- **`mem_class`:** live values are `episodic` (43,989), `procedural` (38,966), NULL (35,131), `semantic` (5,855) and **`source` (117)**. `classify_memory` (`src/classify.py:27-44`) can only return semantic/episodic/procedural, so `source` came from another writer. `MEM_CLASS_BOOST` (`rerank_weights.py:32`) has no `source` key, so `.get(…, 0.0)` at `search.py:264` silently treats it as episodic. LOW.

### 4.3 Reranker weights — single source of truth is intact, its documentation is not
`src/rerank_weights.py` is genuinely canonical: consumed by `src/search.py:14-25` and `scripts/eval/eval_common.py:25,43,46` (which splats `**COEFFICIENTS`). `tests/test_rerank.py:335-341` re-declares the literals as a deliberate independent oracle, exactly as `rerank_weights.py:9-11` describes. **Good.**

Two problems:
- **`tests/test_rerank.py:214-219` documents the wrong formula — MEDIUM.** The class docstring lists `0.30·rrf + 0.18·overlap + 0.18·title + 0.12·recency + 0.08·length + 0.05·depth + type_boost + mem_class_boost + 0.03·log1p(access)·spacing + project_penalty` and **omits `0.10 · context_overlap`** (`rerank_weights.py:25`, asserted 20 lines below at `test_rerank.py:338`), **`superseded_penalty`** (`rerank_weights.py:35`) and **`staleness_penalty`** (`rerank_weights.py:36`). The documented contract and the asserted contract disagree inside one file.
- **`tests/test_rerank_drift.py:29` imports `TYPE_BOOST_TYPES` and `MEM_CLASS_BOOST` from the canonical module** — so it is not an independent oracle for those two, despite the file's name.

### 4.4 Docstrings that misstate behaviour
- **`src/mcp_server.py:387-391` `memory_graph` — HIGH.** "its linked relationships (target_id, relation_type, note) … then `memory_read` those target_ids". `src/db.py:289-296` `get_relationships` returns edges in **both** directions (`WHERE r.source_id = %s OR r.target_id = %s`); `mcp_server.py:402-405` emits `r["target_id"]` unconditionally. **For an inbound edge the queried memory *is* the target, so the tool reports a self-link and drops the actual neighbour.** The JOIN at `db.py:294-295` already computes the other memory's `m.title`/`m.type` — and `mcp_server` throws both away. I confirmed an inbound-only edge exists live: `7ce8fcad-525b-4a8a-ae21-37e96e243bef → bc7cdf28-9908-4ca4-98ea-83ce00ef142e`, type `extends`. Compounded by §1.7 (no `expired_at` filter). Fix: select `CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END AS neighbor_id` — the JOIN already computes exactly this expression.
- **`src/db.py:31-35`** — "Check out a live connection from the pool … the long-running pool self-heals". Self-healing is real; "pool" is not, given `DB_POOL_MIN=1` (see §2).
- **`src/db.py:117-121` `get_memories_by_ids`** — "preserving the requested order". Order preservation works **only** if the caller's ids are already canonical lowercase strings: the index is keyed on `str(r["id"])` (`db.py:139`) and matched with `if i in by_id` (`db.py:141`). Pass `uuid.UUID` objects or an uppercase UUID and the function silently returns `[]` with no error. MEDIUM — and note `db.py:421-426` *does* canonicalise, so the two functions disagree.
- **`src/db.py:245`** `search_similar` says nothing about returning the 1024-dim embedding and tsvector, which its `SELECT *` (`db.py:266-270`) does.
- **`src/mcp_server.py:12-17`** module docstring enumerates the error shape per return type, but **two of the ten tools are missing from that contract**: `memory_recent_activity` (`:360`, no validation at all) and `memory_brief` (`:481`, no try/except → raises instead of returning `"Error: …"`).
- **`src/validation.py:6-8`** — "never SQL text, table or constraint names". True of `validation.py`. But `_guarded` at `mcp_server.py:53-54` forwards Postgres' `e.diag.message_primary` verbatim into `f"Invalid value: {detail}"`; for `InvalidTextRepresentation` that is `invalid input syntax for type uuid: "…"`, which names the Postgres type and echoes the offending literal **untruncated**, unlike `validation.py:51-54`'s 60-char cap. No table or constraint name leaks, so `tests/test_mcp_server.py:869-877` passes, but the guarantee is weaker than the docstring implies.
- **`src/search.py:5-7`** — "`search_similar()` stays in db.py — it's a data-access primitive (single SQL query)". Accurate as far as it goes, but `search_similar` also builds a dynamic `type`/`status` WHERE (`db.py:251-261`), duplicating `hybrid_search`'s filter logic in a second style.
- **`src/express.py:341`** — "(currently none in the store; future-proofed)". Live: `type='question'` has exactly 1 row.
- **`migrations/007_schema_type.sql:30`** — the `COMMENT ON INDEX` narrative is not borne out for the sibling `idx_relationships_derived_from` (007:23), which has 0 scans.
- **`migrations/migrate.sh:7`** — `CONTAINER="${DB_CONTAINER:-second-brain-db}"` is assigned and never used (`run_sql` at `:12` is a native psql call). Dead variable that misleadingly implies migrations run in Docker.

---

## 5. VALIDATION COVERAGE — all 10 MCP tools

| # | Tool | id | limit / days | enum | tags | `_guarded` | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | `memory_create` :60 | n/a | n/a | ✅ type | ✅ | ❌ | gaps |
| 2 | `memory_search` :121 | n/a | ✅ | ✅ type, status | n/a | ❌ | gaps |
| 3 | `memory_read` :214 | ✅ | n/a | n/a | n/a | ✅ | **complete** |
| 4 | `memory_update` :240 | ✅ | n/a | ✅ status, type | ✅ | ✅ | near-complete |
| 5 | `memory_relate` :298 | ✅✅ | n/a | ✅ relation | n/a | ✅ | near-complete |
| 6 | `memory_list` :325 | n/a | ✅ | ✅ type, status | n/a | ❌ | gaps |
| 7 | `memory_recent_activity` :360 | n/a | ❌ **hand-rolled** | n/a | n/a | ❌ | **bypasses validation entirely** |
| 8 | `memory_graph` :387 | ✅ | n/a | n/a | n/a | ✅ | **complete** (output bug, §4.4) |
| 9 | `memory_learn` :411 | n/a | ❌ none | n/a | n/a | ❌ | gaps |
| 10 | `memory_brief` :481 | n/a | ✅ range | n/a | n/a | ❌ | no error handling |

### Unguarded paths, by severity

**HIGH — `memory_recent_activity` (`mcp_server.py:360-378`) does not import or use `validation.py` at all.** `days = max(1, min(int(days), 90))` (`:369`) and `max(0, min(int(samples_per_source), 20))` (`:371`) are hand-rolled clamps with bounds that differ from `clamp_days` (0–365) and `clamp_limit` (1–100). `int("abc")` raises a bare `ValueError: invalid literal for int() with base 10: 'abc'` — not a `ValidationError`, not the documented error shape, and the message is a Python-internals string. Fix: `clamp_days(days, "days", 1, 90)` and `clamp_limit(samples_per_source, 0, 20)` — the `lo`/`hi` override parameters already exist on both helpers and are currently used by no caller, which is precisely why this tool re-invented them.

**MEDIUM — `source_type` is never validated, in three tools.** `memory_create:66`, `memory_search:122`, `memory_list:327`. It reaches SQL as a bound parameter (`db.py:184-186`, `search.py:71-73`), so there is no injection risk, but there is no vocabulary despite `memory_create`'s docstring at `:75` listing eight values and `memory_recent_activity`'s at `:365` listing nine. A typo silently returns zero hits with no feedback. Live `source_type` cardinality is real and enumerable — this should be an `optional_one_of` against a `SOURCE_TYPES` frozenset in `validation.py`.

**MEDIUM — `memory_learn` (`:411-478`) has unbounded paid fan-out.** `topics` passes `require_text` but the resulting `topic_list` (`:436`) has no length cap; `:440-442` runs one Bedrock `generate_embedding` **plus** one `hybrid_search` **per topic**. `topics="a,b,c,…"` × 500 is 500 embedding calls from a single tool invocation. Fix: cap `len(topic_list)` (the docstring at `:417` already says "2-4 key topics").

**MEDIUM — `memory_brief` (`:481-507`) has no `try/except ValidationError`.** `clamp_days` at `:504` raises for a non-numeric `window_days`, so the tool raises instead of returning `"Error: …"`, contradicting the module docstring at `mcp_server.py:14-15`. `use_llm` is unvalidated. Also `:504` reads `14 if window_days is None else window_days`, silently substituting the default where every other tool would pass `None` through.

**LOW-MEDIUM — `memory_update`'s `summary` (`:246`, written at `:268`) is accepted with no validation at all**, unlike `title` and `content` which both go through `require_text`. `""` or pure whitespace is stored. The asymmetry is invisible in the docstring at `:248-250`.

**LOW — unvalidated free text:** `memory_create`'s `source_url` (no scheme or length check) and `encoding_context` (unbounded string straight into the row); `memory_relate`'s `note` (`:299`, unbounded); `memory_learn`'s `source` (`:414`, interpolated into the returned Markdown at `:477` unescaped — agent-facing text, not HTML, so low risk).

**LOW — missing `_guarded`:** `memory_create:100-105`, `memory_search:150-196`, `memory_list:352-353`, `memory_learn:441`, `memory_brief:505`. A surprise constraint violation on these paths surfaces raw.

### Error messages that could leak internals
1. **`mcp_server.py:53-54`** — `_guarded` forwards Postgres' `message_primary` verbatim. Leaks the Postgres type name and echoes the raw offending literal untruncated. MEDIUM-LOW.
2. **`mcp_server.py:369,371`** — raw Python `int()` `ValueError` text. LOW.
3. **`src/db.py:54`** `psycopg2.OperationalError`, and a `PoolError` from `db.py:45`, are documented as propagating (`mcp_server.py:19`, asserted at `tests/test_mcp_server.py:888`). psycopg2's connection-failure message embeds the connection target: `connection to server at "localhost", port 5432 failed…`. That reaches the MCP client and leaks local infrastructure detail. **MEDIUM.** Fix: catch `OperationalError` / `PoolError` at the tool boundary and return a generic "memory store unavailable".
4. **No filesystem paths leak.** `DreamCycleResult.digest_path` (`models.py:26`) is not surfaced by `memory_brief`.

---

## 6. DEAD CODE AND REFACTOR SEAMS

### Dead code
- **`src/db.py:345-366` `get_schema_with_constituents`** — imported at `src/mcp_server.py:28` and **never called** anywhere in `src/` or `scripts/`. Dead function *and* dead import.
- **`src/db.py:369-385` `find_schemas_for_memory`** — zero callers; superseded by `find_schemas_for_memories` (`:388`).
- **`src/dream_cycle_db.py:968-969` `get_memory_provenance`** — pure pass-through shim to `src/db.py:415`.
- **`src/db.py:294-295`** — `get_relationships` selects `m.title, m.type` through the JOIN; `mcp_server.py:402-405` discards both. Dead columns in a live query — and exactly the data needed to fix §4.4.
- **`src/validation.py:118,124`** — `clamp_limit`'s and `clamp_days`' `lo`/`hi` override parameters are never exercised by any caller, which is why `memory_recent_activity` hand-rolled its own clamps.
- **`migrations/migrate.sh:7`** — `CONTAINER`, assigned and never used.
- **`src/models.py`** — nothing in the storage/search/interface layer imports it. It belongs to the dream cycle, not to this layer's scope.

### Unused indexes
See §1.8 — `memories_metadata_idx` (13 MB), `idx_memories_encoding_context` (504 kB), `idx_relationships_derived_from` (16 kB), and the `entities` index set, all at 0 scans. ~16 MB maintained for nothing.

### Where the repository / data-access boundary should sit
`src/search.py` already extracted the *algorithm* but left its *data access* behind in `db.py` (its own docstring at `:5-7` justifies this). The result is that `db.py` mixes three responsibilities. The seams the current code actually offers:

1. **`src/db/pool.py`** — `get_connection`, `close_pool`, `is_reachable`, `DB_CONFIG` (`db.py:16-80`). The **only** module that should import `psycopg2.pool`. Fix `DB_POOL_MIN` while here.
2. **`src/db/columns.py`** — the single `MEMORY_SCALAR_COLUMNS` tuple replacing `db.py:163-168`, `search.py:22-27` and `db.py:127-133`; plus one `memory_summary(row)` serialiser and one `_iso`, replacing the six hand-rolled projections in §3(f).
3. **`src/db/memories.py`** — a repository owning every `memories` statement: create / get / get_many / update / list / arrivals / similar / temporal_neighbors / provenance. `search_similar` belongs here (it is the only vector query outside `search.py`), and the five `SELECT *` sites collapse onto the shared column list.
4. **`src/db/relationships.py`** — `create_relationship`, `get_relationships` (returning the **neighbour** id and filtering `expired_at IS NULL` by default), and one batched schema lookup.
5. **`src/search.py`** keeps the algorithm but takes candidate rows from the repository instead of issuing its own SELECT at `:130-134`. It remains the only place that legitimately needs `psycopg2.sql` composition, for the vector and FTS legs.
6. **Vocabularies** — `src/validation.py` becomes the only declaration; `src/classify.py:10-13` imports `MEMORY_TYPES` and derives `SEMANTIC_TYPES` as a subset. Enforcement should then move *down* into the database (a CHECK constraint in migration 015, after reconciling `correction_episode`), because every non-MCP writer bypasses `validation.py` today.
7. **`src/express.py`** — the `_gather_*(cur, …)` style (`:165-360`) becomes repository methods so the layer has one connection-ownership model; `load_prefs()` at `:385` moves inside the existing `with` block to stop the second connection.

---

## What I could NOT verify, and why

1. **Who wrote the 10 `correction_episode` rows and the 117 `mem_class='source'` rows.** Both are outside `validation.py`'s reach, but the writer lives in `src/capture/*` or `scripts/`, outside the assigned scope. I did not sample their `created_at` to date them against `validation.py`'s introduction.
2. **Whether the test suite re-issues `ALTER DATABASE memory_bank` on every run.** Inferred from `tests/conftest.py:84-101` applying `[0-9]*.sql` unconditionally plus `memory_bank_test` lacking the setting while having the rest of 009. Confirming would require running pytest, which writes.
3. **`pg_stat_user_indexes.idx_scan` counts are cumulative since the last stats reset.** I did not read `pg_stat_database.stats_reset`, so "0 scans" means "0 since reset", not "never used in history".
4. **`memory_graph`'s inbound-edge bug was confirmed by code reading plus live data** (the inbound-only `extends` edge exists), **not by invoking the tool** — calling it would `UPDATE` `access_count` / `last_accessed_at`.
5. **`src/rerank_weights.py:40`'s claim that chunks are "up to 4,000 chars (`src/ingest.py chunk_by_section`)".** `ingest.py` is outside the assigned scope; I did not read the chunker.
6. **`docs/` and `.kiro/specs/` were not opened** per instruction (uncommitted user edits), so I could not cross-check code against design intent — notably `.kiro/specs/data-layer-decomposition/` and `.kiro/specs/db-layer-hardening/`, whose names suggest they already cover part of §6, and `.kiro/specs/rerank-weights-single-source-of-truth/`, which §4.3 suggests was completed successfully.
7. **A third database exists: `second_brain_codex_test`**, carrying `hnsw.ef_search=200`. It is not mentioned in the task and I did not determine its provenance (likely a template copy or a prior 009 run against it).
8. **`express_feedback` has 0 rows**, so the `src/express.py:66-120` feedback path is code-complete but never exercised in production; I could not assess its real behaviour from data.

RELEVANT FILES (ranked by importance):
  1. src/db.py — Epicenter of the duplication and dead code: three column-list declarations, five SELECT * sites contradicting its own docstrings, two dead schema helpers, the get_relationships direction/expired_at defect, and the pool whose DB_POOL_MIN=1 default defeats pooling.
  2. src/mcp_server.py — The 10-tool validation boundary: memory_recent_activity bypasses validation.py entirely, memory_brief has no error handling, source_type is unvalidated in three tools, _guarded leaks Postgres message_primary, and memory_graph emits target_id for inbound edges.
  3. src/validation.py — Sole enforcement point for type/status/relation vocabularies (no SQL CHECK constraints exist, confirmed live); its MEMORY_TYPES omits the live correction_episode value and its lo/hi override parameters go unused, which is why callers hand-rolled clamps.
  4. tests/conftest.py — Its _apply_migrations runner leaves memory_bank_test's ledger empty, half-applies non-idempotent migrations, and executes migration 009's hardcoded ALTER DATABASE memory_bank against production.
  5. src/search.py — Holds the correct psycopg2.sql composition pattern the rest of the layer should adopt, the duplicated _CANDIDATE_COLUMNS list, the rrf_score min-max normalisation, and the SET LOCAL that silently depends on psycopg2's implicit transaction.

NOTE: Line ranges have been extended by ~50 lines to provide additional surrounding context.

---

