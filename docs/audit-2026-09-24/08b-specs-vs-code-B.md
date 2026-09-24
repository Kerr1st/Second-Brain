# 08b — Specs vs Code (Track B)

Repo: /Users/kerrfir/second-brain @ HEAD 6b8c2d2
Scope: db-layer-hardening, retrieval-quality, question-aware-search,
rerank-weights-single-source-of-truth, capture-api, project-auto-tagging,
evaluation-framework; plus Cross-check 1 (requirement citations in code) and
Cross-check 2 (docs/SPEC-INDEX.md vs `ls .kiro/specs`).

Method: read-only. Each spec's requirements.md / design.md / tasks.md compared
against src/, scripts/, tests/ at HEAD. Sections are appended as each family
finishes; a missing section means the track ran out of budget before reaching it.

Severity: S1 = spec and code disagree in a way that would mislead an implementer
or a refactor; S2 = stale/inaccurate but low blast radius; S3 = cosmetic / drift.

---


## 1. db-layer-hardening

Spec state: all 13 tasks checked; landed in `e07e294` ("DB layer hardening:
connection pooling + sql.SQL composable queries"). Header banner (added 2026-09-10,
uncommitted) marks it historical.

### (a) Task checkboxes vs code

| Task | Claimed | Verified | Note |
|---|---|---|---|
| 1.1 pool + `@contextmanager` | done | **done+** | `src/db.py:27-65` — but the implementation grew a SELECT 1 liveness probe loop that no requirement or task describes (see F-1.3). |
| 1.2 `is_reachable` widened to `except Exception` | done | done | `src/db.py:76-83`. |
| 2.1/2.2 conftest fixtures | done | done | `tests/conftest.py:124-135, 142-148` — matches design verbatim. |
| 4.1 "all 10 functions in db.py" | done | done, list stale | db.py now has **16** `get_connection()` callers (adds `get_memories_by_ids`, `get_arrivals_summary`, `get_schema_with_constituents`, `find_schemas_for_memory`, `find_schemas_for_memories`, `get_memory_provenance`). All use the `with` form. |
| 5.2 composable SQL in `hybrid_search` | done | done, design superseded | `src/search.py:54-71, 83-102`. Design's FTS clause was `AND search_vector IS NOT NULL`; code is `AND search_vector @@ to_tsquery(...)` with an AND-then-OR two-pass leg (`search.py:96-112`). Semantics moved past Req 7.3's "identical to the f-string approach". |
| 6.1 "all 16 functions in dream_cycle_db.py" | done | done, list stale | dream_cycle_db.py now has **25** direct `get_connection()` callers (`grep -n "with get_connection" src/dream_cycle_db.py`). |
| 8.1/9.1 mocks return context managers | done | done | `tests/test_dream_cycle_db.py` 80 `__enter__` sites, `tests/test_dream_cycle.py` 4; zero `close.assert_called` remain. The suggested `_mock_context_manager` helper was never added (grep empty) — boilerplate is repeated 80×. S3. |
| 12.1–12.5 property tests | done | done | `tests/test_pool_properties.py` has all five, each tagged `**Validates: Requirements …**`. |

No unchecked task is implemented (all are checked). No checked task is missing.

### (b) Requirements the code violates or has moved past

**F-1.1 (S1) — Design rationale for `SimpleConnectionPool` is now false; three callers are multi-threaded.**
`design.md:41-42`: "SimpleConnectionPool over ThreadedConnectionPool: The MCP server runs on a single-threaded stdio transport. The dream cycle scripts are also single-threaded. No thread safety needed." psycopg2 documents `SimpleConnectionPool` as usable only by a single thread. Callers that break the premise today:
- `src/capture_api.py:120,133,146,164,183` — all endpoints are plain `def` under FastAPI, so Starlette runs each request on its anyio worker threadpool (default 40 threads); concurrent `/capture/*` calls enter `get_connection()` from different threads.
- `scripts/batch_ingest_parallel.py:91` and `scripts/migrate/ingest_doc_chunks.py:156` — `ThreadPoolExecutor(max_workers=…)` calling `create_memory`/`ingest_content`. Both files `os.environ.setdefault("DB_POOL_MAX", "20")` (lines 22 / 26), i.e. the authors already treat the pool as shared across threads and sized it up, without changing the class.
Fix: one-word change `SimpleConnectionPool → ThreadedConnectionPool` in `src/db.py:14,35` (same API), and strike the rationale from the design or mark it superseded. Evidence of an actual corruption event was not sought (read-only track).

**F-1.2 (S2) — Req 1.2 / design: pool size read from env "consistent with DB_CONFIG".** `DB_CONFIG` is frozen at import (`db.py:16-22`), but `DB_POOL_MAX` is re-read on *every* checkout at `db.py:42` to size the probe loop, independent of the `maxconn` the pool was built with (`db.py:37`). If the env var changes after pool creation (tests in `test_pool_properties.py:106` do exactly this via `monkeypatch`), the loop bound and the pool's real capacity diverge. Fix: use `_pool.maxconn`.

**F-1.3 (S2) — Undocumented behaviour: liveness probe on every checkout.** `db.py:41-52` issues `SELECT 1` + `rollback()` per checkout and drops dead connections with `putconn(close=True)`. Not in any requirement, design, or task; the docstring is the only record. Consequences the spec never considered: (i) every `get_connection()` costs a round-trip (the spec's stated goal was to *remove* per-call overhead — Req 1 user story); (ii) Property 1 test (`test_pool_properties.py:43`) asserts the connection is returned but cannot distinguish "returned" from "closed-and-replaced". Fix: either add a requirement (Req 1.5 "self-healing") or move the probe behind a cheaper check (`conn.closed`, or probe only after an `OperationalError`).

**F-1.4 (S3) — Req 2.3 wording.** Requires rollback on exception; code additionally swallows a `psycopg2.Error` raised *by the rollback itself* (`db.py:58-61`). Sensible, but silent — a dead connection is then `putconn`'d back without `close=True`, so the *next* checkout pays the probe to discover it. Fix: `putconn(conn, close=True)` in that branch.

**F-1.5 (S3) — Req 8 "all 242+ existing tests".** Test count is a snapshot; suite is now ~85 files. Historical, banner covers it.

### (c) design.md names that no longer exist / have moved

- Caller inventory table (`design.md:97-103`) lists 28 call sites; live count is 16 + 2 + 25 = 43 in the three named files, plus `src/express.py:28`, `src/capture/codex.py:218` (`connect=db.get_connection`), `src/skim/store.py` (patched in conftest:41) and 14 scripts. The "No external callers exist" mitigation (`design.md:243`) is false today.
- `design.md:150-158` FTS composable — the string literal shown does not exist in code (superseded, see 5.2 above).
- `_mock_context_manager` helper (`design.md:222-226`, `tasks.md:82`) — never created.
- All other names (`_pool`, `close_pool`, `get_connection`, `is_reachable`, `test_db`, `clean_tables`) exist.

### (d) Contradiction with data-layer-decomposition on the repository boundary

`data-layer-decomposition/design.md:12` declares the target: "Data Access (db.py — pure CRUD + connection management)" and `:146` "search.py … imports `get_connection` from `db.py` for raw database access but contains no CRUD operations." So the *service* layer is explicitly licensed to write raw SQL. db-layer-hardening then hardened SQL construction **only in that service-layer module** (`hybrid_search`, Req 7) and declared the data layer's own f-string WHERE clauses (`list_memories`, `search_similar`, `update_memory`'s `set_clause` at `db.py:151-156`) out of scope (`design.md:161-163, 261`). Net effect: the module the decomposition calls the data-access layer has the *least* hardened SQL, and "raw SQL against `memories`" is now spread over `db.py`, `search.py`, `dream_cycle_db.py`, `express.py`, `skim/store.py` and ≥10 scripts (grep above). Neither spec defines a boundary that forbids this, and the two together normalise it. S2 as a refactor seam: the refactor needs a spec that says *who may write SQL* — neither of these does.

Also: `data-layer-decomposition/design.md:611` states the rerank formula as `0.35×rrf + 0.20×overlap + 0.20×title + 0.15×recency + 0.10×length + type_boost + reinforcement` — the pre-retrieval-quality formula. See §2–4 for the live formula; this spec's Property 2 is now a false statement about `src/search.py`.

### Verdict — db-layer-hardening: **update** (not archive)
It is the only spec that describes `get_connection()`'s contract, and that contract is live and load-bearing. Fix F-1.1's rationale, record the probe (F-1.3), refresh the caller inventory, then freeze.

---


## 2. retrieval-quality

Spec state: 13 top-level tasks, all checked. This is the spec that introduced
`mem_class`, `project`, `last_accessed_at`, depth scoring, spacing bonus,
relationship discovery and `temporal_context`.

### (a) Task checkboxes vs code

Every checked task has a corresponding artefact: `migrations/002_v2_columns.sql`,
`src/classify.py`, `src/depth.py`, `find_temporal_neighbors` (`db.py:293`),
`_discover_relationships` (`ingest.py:105`), `temporal_context` (`mcp_server.py:172-189`),
`tests/conftest.py` fixtures (`mock_embedding:177`, `sample_memory_factory:191`),
`tests/test_rerank.py` Property 8 (`:208`). No unchecked tasks exist. Two checked tasks
are implemented *differently* from their text:

- **Task 5.1 / design.md:151 / Property 4** — `classify_memory(type, source_type, content)`.
  Code: `classify_memory(type: str, content: str)` (`src/classify.py:27`); every caller
  passes two args (`capture_api.py:105`, `ingest.py:172,195`, `mcp_server.py:96,287`,
  `dream_cycle/storage.py:22,69`). The module docstring (`classify.py:4`) and the spec
  glossary still say classification uses `source_type`. It never has. S3.
- **Task 7.2** "Move `_DEPTH_RE` from mcp_server.py to depth.py" — `_DEPTH_RE` no longer
  exists anywhere (`grep -rn _DEPTH_RE src` empty); `depth.py` has `_CAUSAL_RE` etc. Fine,
  but design.md:188 still describes the moved regex. S3.

### (b) Requirements the code violates or has moved past

**F-2.1 (S1) — Req 7.7 formula is not the live formula, and no spec defines the live one.**
Req 7.7 / design.md:251-262 / Property 8: `0.30·rrf + 0.18·overlap + 0.18·title + 0.12·recency + 0.08·length + 0.05·depth + type_boost + mem_class_boost + reinforcement + project_penalty`. Live `src/search.py:319-333` adds three terms and changes one:
- `+ 0.10 · context_overlap` (encoding-context reinstatement, `search.py:241-247`)
- `+ superseded_penalty` (−0.20, `search.py:300-303`)
- `+ staleness_penalty` (−0.05·ramp after 90 days for never-retrieved rows, `search.py:271-279`)
- recency is power-law `(1 + days/(30 + 10·log1p(access)))^−0.8` (`search.py:258-266`); design.md:384 says `exp(-days_old / 60)`.
All four arrived in commit `1acf9fb` ("Encoding context, schema type, depth experiments, and search/rerank enhancements") with no spec. rerank-weights-single-source-of-truth *inventories* them (its Glossary) but explicitly refuses to define them ("Out of Scope: … the shape of the rerank formula"). Consequence: the only requirement-level statement of the formula in the repo (Req 7.7) is false, and `tests/test_rerank.py:208-220` still carries the Req 7.7 docstring while its oracle body (`:295-330`) computes the live formula — the test validates something its own docstring does not say. Fix: a `rerank-formula-v3` requirement (or an addendum to this spec) that states the full live formula; correct the Property 8 docstring.

**F-2.2 (S2) — Req 8.1/8.2 semantic-neighbour discovery cannot link ingested documents to each other.** `ingest.py:171-186`: the parent record is stored *without* an embedding ("intentional"). `_discover_relationships` (`ingest.py:114-119`) then calls `search_similar(embedding, limit=3)` — which has `WHERE embedding IS NOT NULL` (`db.py:245`) — and keeps only hits with `parent_id IS NULL`. Ingested parents fail the first filter; chunks fail the second. So the only possible Semantic_Neighbors are memories created *with* an embedding and no parent: MCP `memory_create`, dream-cycle outputs, and capture-API rows. For the dominant channels (kiro_ide_chat 5,493 docs, quick_desktop_doc 32,624, article 867, youtube 548 — live counts) Req 8.1 is structurally unsatisfiable. The `limit=3` is also applied *before* the filters, so even eligible neighbours are lost when a chunk occupies a top-3 slot. Fix: search `limit=prefetch` and filter to 3, or search against a chunk's embedding and map to its parent.

**F-2.3 (S3) — Req 8.6 guard is implicit.** Spec: "WHEN a chunk Memory is stored … SHALL skip Relationship_Discovery". Code never checks `parent_id`; discovery is simply not called in the chunk loop (`ingest.py:189-206`). Property 13 test therefore tests call-site structure, not a guard. Acceptable; note only.

**F-2.4 (S3) — Req 1 test-count claims** ("174 tests across 8 files", "243+") — snapshot; banner covers it.

**F-2.5 (S3) — Req 9 satisfied, then extended without spec.** `memory_search` also returns `schema_context` (`mcp_server.py:192-210`, `find_schemas_for_memories`). No spec family describes `schema` type, `derived_from` semantics, or migration `007_schema_type.sql`.

### (c) design.md names that no longer exist / drifted

| design.md | Status |
|---|---|
| `classify_memory(type, source_type, content)` (:101, :151, :285) | signature drifted (2 params) |
| `_DEPTH_RE` moves to depth.py (:188) | deleted, not moved |
| `hybrid_search(query_text, query_embedding, limit, type, status, project)` (:235) | now also `source_type`, `created_after` (`search.py:45-46`) |
| `memory_search(query, type=None, limit=10, project=None)` (:296) | now also `source_type`, `since_days`, `status` (`mcp_server.py:121-122`) |
| recency `exp(-days_old / 60)` (:384) | replaced by power law |
| `migrations/002_v2_columns.sql`, `find_temporal_neighbors`, `compute_spacing_bonus`, `mock_embedding`, `sample_memory_factory`, `clean_tables` | exist |

### Verdict — retrieval-quality: **supersede**
Keep as the record of *why* mem_class/depth/spacing/project exist (the research citations are only here), but the formula section must be superseded by a live-formula spec (F-2.1). Do not "update" in place — the historical banner would then be lying in the other direction.

---

## 3. question-aware-search

Spec state: 5 tasks, all checked. Migration `005_question_weighted_search.sql`,
`depth.extract_questions` (`src/depth.py:67`), `tests/test_question_search.py` with
Properties 1–4 all present and tagged.

### (a) Task checkboxes vs code — all verified

- 1.1–1.3: `migrations/005_question_weighted_search.sql:6` (`extract_questions_text`), `:85` (trigger replacement), `:104-106` (backfill). The backfill uses correlated sub-selects, not the `FROM LATERAL` the task text describes — equivalent, S3.
- 2.1–2.3: `depth.py:67`; `test_question_search.py:115-180`.
- 4.1–4.7: `test_question_search.py:310-709` — every listed test present, including idempotency (`:545`) and the two DB-backed properties.

### (b) Requirements the code violates or has moved past

**F-3.1 (S3) — Req 4.2 / 7.3 "hybrid_search SHALL require no code changes" is a point-in-time negative.** `hybrid_search` has since changed its FTS leg substantially (`@@` pre-filter, AND-then-OR two pass, `search.py:96-112`); the weight semantics still hold because `ts_rank` is unchanged, so the *intent* survives. Historical banner covers it.

**F-3.2 (S3) — Req 2.1 says title gets weight A; `encoding_context` (added by migration 006) is not in the tsvector at all.** Not a violation of this spec, but the "context reinstatement" term in rerank (`_context_overlap`) is therefore a pure-Python token overlap with no FTS support, while the spec that introduced weighted FTS knows nothing about the column. A refactor of search needs to decide whether `encoding_context` belongs in `search_vector` (weight C?) — no spec owns that decision.

**F-3.3 (S3) — Terminology.** Spec calls `ts_rank` "BM25" throughout (Introduction, Glossary, Req 4.3). Prior audit already flagged; noted for completeness.

### (c) design.md names — all exist (`extract_questions_text`, `memories_search_vector_update`, `trg_memories_search_vector`, `extract_questions`, `_QUESTIONS_RE`).

### Verdict — question-aware-search: **keep-as-record**
The most accurate of the seven. Nothing to update; the one live seam (F-3.2) belongs in a new search spec, not here.

---

## 4. rerank-weights-single-source-of-truth

Spec state: 8 tasks, all checked; landed in `f84179f`.

### (a) Task checkboxes vs code — all verified

- Task 2: `src/rerank_weights.py` defines exactly the named constants (`:19-36`); imports nothing from `src` ✓; docstring says test_rerank.py does not import it ✓ (`tests/test_rerank.py:6-11` confirms).
- Task 3: `search.py:19-29` imports all constants; inline literals for the seven coefficients and five additive magnitudes gone; signal-shaping constants `30/10/0.8/80/90/180` still inline (`search.py:262-278, 283`) as the task requires.
- Task 4: `eval_common.py:25,42-49` builds `PRODUCTION_WEIGHTS` from `COEFFICIENTS` + `REINFORCEMENT_COEFF`; multipliers retained ✓; comment block updated ✓.
- Task 6: `tests/test_rerank_drift.py` seeds every branch (`:59-99`), asserts branch presence (`:235-245`), asserts per-id agreement at `1e-9` (`:288`).
- Task 8: `docs/ARCHITECTURE.md:79` has the pointer.
- Task 7 (transient negative check) is unverifiable from artefacts by construction.

### (b) Requirements — none violated

**F-4.1 (S3) — Req 4.4 satisfied only by the weaker arm.** ARCHITECTURE.md:79 names the signals but no longer states the formula; the requirement allowed "OR a follow-up note". Acceptable, but it means *no document in the repo states the formula with numbers* except the source file and the oracle test — see F-2.1.

**F-4.2 (S3) — `OVERLAP_WINDOW_CHARS = 20000` (`rerank_weights.py:44`) is in the "single source of truth" module but is not a weight.** Neither the spec's Glossary nor Task 2's constant list includes it; the spec's Out-of-Scope explicitly excludes "signal-shaping internals", which is what this is. Post-spec addition (`git log -S OVERLAP_WINDOW_CHARS`) that blurs the module's stated boundary. Harmless, but the drift guard does not exercise it (eval scorer reads `_overlap` pre-computed, so a window change cannot drift). Fix: either move it next to the other shaping constants in `search.py` or widen the module's docstring.

### (c) design.md names — all exist.

### (d) Contradictions between retrieval-quality, question-aware-search and rerank-weights on the formula

| Statement | Source | True at HEAD? |
|---|---|---|
| `0.35 rrf + 0.20 + 0.20 + 0.15 + 0.10 + type_boost + reinforcement` | data-layer-decomposition/design.md:611 (Property 2) | No (V1) |
| `0.30/0.18/0.18/0.12/0.08 + 0.05 depth + type + mem_class + reinf·spacing + project` | retrieval-quality Req 7.7, design.md:251, Property 8 | No (V2) |
| "+0.10 context_overlap, superseded −0.20, staleness −0.05" | rerank-weights Glossary (inventory only, "not defining") | Yes (V3), but not normative |
| No formula; "ts_rank weights A/B" only | question-aware-search | Orthogonal — affects the *input* `rrf_score`, not the formula. No contradiction, but note `rrf_score` is now min-max normalised over the candidate set (`search.py:141-151`) — none of the three specs says so, and retrieval-quality's Property 8 samples `rrf_score ∈ [0,1]` as if that were a spec fact. |

Three specs, three formulas, zero that match the code as a normative statement. The
refactor needs one.

### Verdict — rerank-weights-single-source-of-truth: **keep-as-record** (tighten F-4.2 if touched)

---


## 5. capture-api

Spec state: **all 9 tasks unchecked**, overridden by a 2026-06-04 banner in tasks.md
("BUILT & DEPLOYED … treat all tasks as done"). This is the inverse of every other
family — the only spec where the checkboxes under-report.

### (a) Task checkboxes vs code

Every unchecked task is implemented: `src/capture_api.py` (209 lines), 45 tests in
`tests/test_capture_api.py`, `requirements.txt:8-9`, plist
`scheduling/com.second-brain.capture-api.plist`. Two task texts are wrong about *where*:

- Task 8 / design.md:152-156 / Req 7.5: `scripts/capture_api.sh` with "hardcoded absolute path, `python3`, no venv activation". Actual: `scripts/jobs/capture_api.sh`, which `set -a; . ./.env` for the token and `exec .venv/bin/python` (lines 16-22). `scripts/capture_api.sh` does not exist. The plist points at the `jobs/` path (plist line 10). S3, but the design's stated convention ("matching `mcp_serve.sh`") is the opposite of what shipped — the shipped version is the better one, and nothing records why it changed.
- Task 2 says the token is read "at call time"; Req 5.2 says "at startup". Code reads at call time (`capture_api.py:38`). Internal spec inconsistency; S3.

### (b) Requirements the code violates or has moved past

**F-5.1 (S1) — The API has never stored a memory; the spec's own premise is obsolete.**
Read-only evidence:
- launchd: `com.second-brain.capture-api` is running (pid 42500).
- `~/Library/Logs/second-brain/capture-api-{stdout,stderr}.log`: 10 lines total since the log began; 3 POSTs ever (2026-09-14 18:49) — 422, 401, 422. **Zero 201s.**
- Live DB: `source_type IN ('browser_extension','email')` → 0 rows. `source_type='slack'` → 856 rows, of which **0** carry the API's `slack_user`/`slack_channel`/`thread_ts` metadata; all 856 come from `src/capture/slack.py` (`SOURCE_TYPE = "slack"`, line 41).
- The Introduction's premise — "There is no way to capture knowledge from Slack, mobile devices, web browsers, or email" — is false at HEAD: `src/capture/slack.py`, `src/capture/browser.py` (`SOURCE_TYPE="browser_history"`), `src/capture/outlook.py` (`SOURCE_TYPE_MAIL="outlook_email"`) are scheduled batch pullers that have captured 856 + 36 + 221 + 110 rows.
docs/CAPTURE-COMPONENTS.md:21 already hedges ("not a working inbox/browser connector") and SPEC-INDEX.md:14 says "endpoint availability does not establish successful capture". Both are correct and neither says the number, which is zero. Fix: either retire the service (plist + wrapper + module + 45 tests, or keep `/capture` alone for iOS Shortcuts) or record it as dormant. A KeepAlive process holding a pool connection for a service with no clients is a cost with no return.

**F-5.2 (S1) — `source_type` vocabulary: the spec's three values are the only ones ever specified, and two are dead, one collides.**
| Spec value (Req 2.2 / 3.3 / 4.2) | Live writer with same channel | Live value | Rows |
|---|---|---|---|
| `slack` | `src/capture/slack.py:41` | `slack` | 856 — **same label, different shape** (batch rows have `source_url`, API rows would have `None`; `get_processed_source_urls("slack")` dedup would be blind to API rows) |
| `browser_extension` | `src/capture/browser.py:39` | `browser_history` | 36 vs 0 |
| `email` | `src/capture/outlook.py:43` | `outlook_email` | 221 vs 0 |
Meanwhile `mcp_server.py:74` documents `source_type: youtube, article, pdf, course, kiro_cli_chat, kiro_ide_chat, notes, manual` — none of the API's three, and `pdf`/`course`/`notes`/`manual` have 0 live rows. The live DB has **27 distinct `source_type` values** (incl. 14 `quick_desktop_*`, `distilled_chat`, `codex_task`, `weekly_digest`, and 88 rows with NULL). No spec, doc, or code constant enumerates them; `get_arrivals_summary` (`db.py:189`) invents `generated:<type>` labels for NULLs on the fly. `memory_search(source_type=…)` and `memory_list(source_type=…)` filter on this column with no validation. Fix: a `source-type-vocabulary` spec + a single `SOURCE_TYPES` constant (or a CHECK constraint / lookup table) that every writer imports. This is the highest-leverage missing spec in this track.

**F-5.3 (S2) — Req 7.3 "SHALL use the existing connection pool"** — it does, and thereby inherits F-1.1: FastAPI runs these sync handlers on a threadpool against a `SimpleConnectionPool`. The capture-api spec is the second spec (after db-layer-hardening) whose assumptions jointly create the race.

**F-5.4 (S3) — Req 1.1 field list.** `CaptureRequest` also accepts `encoding_context` (`capture_api.py:65`) and `_store_memory` threads it through (`:98,:113`). Unspecified extension; harmless.

**F-5.5 (S3) — Introduction cites `docs/AGENTS.md`** ("architecture diagram … identifies a Capture API"). File does not exist (`ls docs/AGENTS.md` fails; not in `docs/archive/2026-09-10/` either).

### (c) design.md names

| design.md | Status |
|---|---|
| `scripts/capture_api.sh` (:152) | moved to `scripts/jobs/capture_api.sh` |
| `_store_memory(title, content, source_type, source_url, tags, metadata, project, type)` (:43 prose) | prose order ≠ code order; the code block at :46-47 is right |
| `docs/AGENTS.md` (:10 region, requirements Intro) | does not exist |
| `verify_token`, `CaptureRequest`, `SlackCaptureRequest`, `BrowserCaptureRequest`, `EmailCaptureRequest`, `/health` | exist |

### Verdict — capture-api: **archive** (after a decision on the service)
The banner already declares it a "design reference for the planned Slack/Outlook capture extension" — that extension shipped as `src/capture/*` without this API. The spec now documents a running process with no callers. Archive; if `/capture` is kept for iOS Shortcuts, write a one-page replacement that also fixes F-5.2.

---


## 6. project-auto-tagging

Spec state: 9 tasks, all checked. Structurally the most faithfully implemented spec in
the track — and the one whose *outcome* is furthest from its intent.

### (a) Task checkboxes vs code — all verified

`src/project.py` (rules 1-7 in docstring match Req 8.1-8.6 exactly);
`ide_chat.py:19,55`; `cli_chat.py:20,133,178`; `chat_extract.py:93` unpacks 3-tuples;
`ingest.py:166-168` explicit-over-header; `mcp_server.py:98,152`;
`scripts/backfill_projects.py` with `BATCH_SIZE = 500`, `--dry-run`, `conversations_v2`
fallback; `tests/test_project_properties.py` Properties 1-8 all present and tagged.

### (b) Requirements the code violates or has moved past

**F-6.1 (S1) — Both extraction premises are false on live data; the `project` column is 99.5 % NULL and the remainder is UUID noise.**
Live (read-only, parent rows only): 48,910 parents; **266** have a project; **263 distinct values**; 262 of them are UUIDs, all `kiro_cli_chat`, dated 2026-05-17 → 2026-09-24. `kiro_ide_chat`: **0 of 5,493** tagged. The one non-UUID tag (`isengard-second-account`, 3 rows) came via MCP.

- *CLI premise* (Req 2.1: "`conversation_id` … is the workspace directory path", design.md:103): the parser no longer reads SQLite. `cli_chat.py:22` reads `~/.kiro/sessions/cli/*.jsonl`; `conv_id = basename(path)` (`:85`) is a session UUID. `normalize_project_tag("3f202db6-…")` returns the UUID unchanged (no `/`, not dot-prefixed), so **Req 2.3** ("WHEN `conversation_id` … is a non-path identifier, SHALL set the Project_Tag to NULL") is violated on every CLI session since the JSONL migration. The `Project:` header then carries the UUID into ingestion (Req 3.2, 4.1 — working as specified, propagating the error).
- *IDE premise* (Req 1.1: "extract the top-level directory name from the first expanded path"): measured across all 12,398 live `.chat` files with the parser's own `extract_project_context()` — `project_hint` is `None` for **12,398 / 12,398**. Every `expandedPaths[0]` top-level component is `.kiro` (4,865 of 4,865 fileTree entries with paths). `expandedPaths` are workspace-*relative*; the workspace name is not in them. design.md:58 already knew this ("34,145 entries with `project: ".kiro"` (useless)") and assumed the on-disk files were richer. They are not.

Downstream: `rerank`'s `project_penalty` (−0.15) fires whenever a query passes `project=` and a hit has a UUID tag — always a mismatch. `hybrid_search(project=…)` keeps NULL-project rows, so 99.5 % of the corpus is unaffected and the feature is effectively a no-op with a small noise floor. Fix: (1) in `parse_conversation`, derive the workspace from the session's first `Prompt` metadata/cwd if present, else `None` — do not pass a UUID to the normaliser; (2) add a UUID/hex guard to `normalize_project_tag` (Req 2.3 gives the mandate); (3) for IDE chats, read the workspace-hash directory name (the parent dir of the `.chat` file, `ide_chat.py:4-5`) and map it, or accept NULL and drop the fileTree path; (4) one-shot `UPDATE memories SET project=NULL WHERE project ~ '^[0-9a-f]{8}-'`.

**F-6.2 (S2) — Req 6 backfill is now unrunnable as specified.** `backfill_projects.py:108-115` reads `conversations_v2` from a SQLite DB the CLI no longer writes; Phase A re-parses `.chat` files that yield nothing (above). The script is listed as live maintenance in `docs/OPERATIONS.md:94`. Fix: retire or repoint.

**F-6.3 (S3) — Req 5 "MCP passthrough (Existing — No New Work)"** — then Task 6 adds normalisation in MCP (Req 8.8/8.9). Internal ordering quirk; code is right (`mcp_server.py:98,152`).

**F-6.4 (S3) — Introduction counts** ("73,257 IDE chat memories", "752 CLI") — snapshot; banner covers it.

### (c) design.md names

| design.md | Status |
|---|---|
| "CLI SQLite DB", `conversations_v2` (:11, :23, :37, :103, :149, :260) | source replaced by `~/.kiro/sessions/cli/*.jsonl`; only the backfill fallback still references it |
| `extract_project_context`, `normalize_project_tag`, `parse_conversation`, `format_as_markdown(…, project=None)`, `backfill_projects.py` | exist |

### Verdict — project-auto-tagging: **update** (or supersede with `project-tagging-v2`)
The normaliser and plumbing are sound and tested; the two *sources* died. The spec should
say so in a status banner and the two premise requirements (1.1, 2.1) need replacing
before anyone tunes `PROJECT_PENALTY` against a column that carries no signal.

---


## 7. evaluation-framework

Spec state: 8 tasks; all checked except **2.2 (MANUAL: populate seed.json with real IDs)**,
which is correctly unchecked — and still undone.

### (a) Task checkboxes vs code

| Task | Claimed | Verified |
|---|---|---|
| 1.1 underscore signals in `rerank()` | done | done, and extended: `_context_overlap`, `_superseded_penalty`, `_staleness_penalty` added later (`search.py:306-318`) |
| 1.2 `scripts/eval_common.py` | done | exists as **`scripts/eval/eval_common.py`** (package, not flat). All 8 listed functions present. Every task in this file names `scripts/eval_*.py`; every file lives in `scripts/eval/`. `docs/OPERATIONS.md:93` has the right path. S3 |
| 1.3 `evaluations/{query_sets,results}` + `.gitignore` | done | `query_sets/` exists; `.gitignore:27` ignores `results/`; **`evaluations/results/` does not exist on disk** — see F-7.1 |
| 2.1 `seed.json` template | done | 31 entries, 6 categories (6/5/5/5/5/5), `_instructions` key ✓ |
| 2.2 populate real IDs | **not done** | **31 / 31 still placeholder `00000000-…`** |
| 2.3 / 3.1 / 4.1 / 6.1 / 7.1 / 8.1 scripts | done | all present under `scripts/eval/`; `eval_common.load_query_sets` skips placeholder IDs (`:118-119`) |
| 4.1 "baseline + 6 ablation conditions" | done | now **9** conditions (`eval_common.py:65-75`: + `minus_superseded`, `minus_context`, `minus_staleness`) |
| 5.1 `tests/test_eval_properties.py` | done | exists |

### (b) Requirements the code violates or has moved past

**F-7.1 (S1) — The evaluation harness has never produced a result from this checkout, and its curated tier is empty by construction.**
- `evaluations/results/` is absent. Every tier writes there (`write_results`); the directory is created on first write. Either no evaluation has ever run here, or results were removed. Nothing is scheduled (`grep -rl eval scheduling/` empty).
- Req 1.6 ("seed curated query set … authored by examining actual memories") is unmet: 31/31 placeholders. `load_query_sets` drops them, so `eval_curated` evaluates **0 queries**, and `cold_warm`/`ablation` ("golden + curated") silently run on golden queries alone.
- Golden supply: `get_golden_queries()` (`dream_cycle_db.py:439`) reads `dream_cycle_candidates WHERE final_verdict='ACCEPTED'` — 114 rows live, all with `source_memories` — then extracts "Questions this answers" from the created memory; ~75 active insight-class memories carry that section. So the whole harness rests on ≤114 machine-authored questions about machine-authored insights. `docs/VALIDATION.md:23` says exactly this ("Golden queries can be derived from memories, so keep independent held-out questions") — the spec's mechanism for held-out questions is Task 2.2, which is the one task nobody did.
Fix: either do 2.2 (≥30 human-authored queries with verified IDs) or mark the curated/cold-warm/ablation tiers as "not yet evidence" in the spec banner. Until then, no MRR/Hit@k number from this harness should be cited.

**F-7.2 (S2) — Req 2.2 cold-mode definition is stale.** Spec: cold zeroes `mem_class_boost`, `depth`, forces `spacing_bonus=1.0`, zeroes `project_penalty`, "keeping base weights … unchanged". `COLD_OVERRIDES` (`eval_common.py:54-59`) does exactly that — and therefore *keeps* `context_overlap` (0.10), `superseded_penalty`, `staleness_penalty`, all of which post-date the spec. "Cold" no longer means "V1 formula"; it means "V3 minus four V2 signals". Any cold-vs-warm delta measures something the spec does not describe. Fix: redefine `COLD_OVERRIDES` (add `context_overlap: 0.0`, `superseded_penalty: 0.0`, `staleness_penalty: 0.0`) or rename.

**F-7.3 (S3) — Req 3.1 lists 6 ablation conditions; code has 9.** Extension without spec; the summary table in `eval_ablation.py` will show rows the spec does not name. Fine, record it.

**F-7.4 (S3) — Req 5.5 `script_version` in metadata.** `get_eval_metadata()` — not verified whether `script_version` is emitted (function read only by name). Listed under Could-not-verify.

### (c) design.md names

Every `scripts/eval_*.py` path in design.md and tasks.md is now `scripts/eval/*.py`. Function names all exist. `evaluations/results/` — specified, gitignored, never created.

### Verdict — evaluation-framework: **update**
The code is complete; the *data* the spec depends on was never authored, and the cold-mode
definition has drifted. Update: (i) status banner stating "no results yet; curated tier empty";
(ii) fix paths; (iii) redefine cold; (iv) keep 2.2 unchecked until it is done.

---


## 8. Cross-check 1 — requirement citations in code

`grep -rn 'Req \|Requirement\|\.kiro/specs\|Validates:' src scripts tests --include='*.py'`
→ 479 hits in 41 files. (Without `--include` the grep also returns
`scripts/backups/kb-sources/*.md` — Kiro blog/doc mirrors — which are not code.)

**Ownership split.** 327 of the 479 hits cite the dream-cycle, model-backend-adapters,
direct-api-backends or claude-code-probe specs (`src/backends/*.py` 116, `src/dream_cycle_db.py` 8,
`tests/test_dream_cycle.py` 65, `test_consensus.py` 36, `test_codex.py` 35, `test_claude_code*.py` 55,
`test_integration.py` 17, `test_agent_invoker.py` 14, `test_resolver.py` 10, `test_mcp_probe.py` 7,
`test_orchestrator_properties.py` 9, `test_prompts.py` 12, `test_panel_prompts.py` 4, `test_feedback_properties.py` 5,
`test_digest_properties.py` 5, `scripts/dream_cycle_run.py` 1, `scripts/eval/golden_queries.py` 1 — "Requirements: 18.3" is dream-cycle's Tier-3 metric). Those are Track A's (08a).
**Zero citations exist in `src/` for any of this track's seven families** — `db.py`, `search.py`, `classify.py`,
`depth.py`, `project.py`, `capture_api.py`, `rerank_weights.py`, `ingest.py`, `mcp_server.py`, `scripts/eval/*` carry no `Req` comments. All in-scope citations are in tests.

**In-scope citations, one row per test file** (numbers checked against the cited spec's requirements.md numbering; "still says" = the AC text still describes what the test asserts):

| File | Cites | Spec | Points at a real AC? | Still says what the test assumes? |
|---|---|---|---|---|
| `tests/test_pool_properties.py` | 1.1, 2.2, 2.3, 3.2, 4.3, 5.2, 6.1, 6.2, 6.3, 7.3 | db-layer-hardening | yes, all | yes. Caveat: Property 3 (`:186`) checks 27 hard-coded signatures; `search.hybrid_search` is listed *with* `source_type`, `created_after` (`:145`) — the oracle was updated to post-spec reality, so it no longer tests "pre-migration signature" as Req 3.2/4.3/5.2 state. Property 4 (`:253`) compares against an `_old_where` that never had `source_type`/`created_after` — it tests the spec's snapshot, not `hybrid_search` as it is. Both are tautologies now. |
| `tests/test_question_search.py` | 1.5, 1.8, 2.1, 2.2, 3.4, 4.1, 4.3, 5.2, 5.4, 6.2, 6.3, 6.5, 8.1-8.5 | question-aware-search | yes, all | yes |
| `tests/test_project_properties.py`, `tests/test_project.py` | 1.1-1.4, 2.1-2.6, 4.1-4.4, 6.4, 6.5, 6.9, 6.10, 6.12, 7.1, 7.2, 8.1-8.9 | project-auto-tagging | yes, all | **Req 2.3 is cited (`test_project.py:216,227`) and the test passes while production violates it** — the test feeds "non-path" strings the spec had in mind (empty, `None`); it never feeds a UUID, which is what `conversation_id` now is (F-6.1). Req 1.1-1.3 tests pass on synthetic `fileTree` with real project names; live files never have one. Citations are accurate; the tests are mock-shaped around a dead premise. |
| `tests/test_search_properties.py` | 1.2, 1.4, 2.1, 2.3, 3.1, 3.2, 3.5, 4.1, 4.2, 5.1-5.3, 6.1-6.3, 7.1, 8.1, 10.1, 10.2 | data-layer-decomposition (header `:1`) | yes, all 10 requirements exist | yes for structure. Not cited: that spec's Property 2 formula (`design.md:611`) — good, because it is false (§4d). |
| `tests/test_ingest_v2.py` | 8.5, 8.6, 8.7 | retrieval-quality | yes | 8.5 (caps) yes; 8.6 tested by absence of call (F-2.3); 8.7 yes. **Not cited anywhere: 8.1/8.2** (semantic neighbours) — the requirement the code cannot satisfy (F-2.2) has no test. |
| `tests/test_db.py` | 1.3, 1.5, 2.5 | retrieval-quality | yes | yes |
| `tests/test_rerank.py` | 3.2-3.7, 4.7-4.10, 5.7, 6.5-6.7, 7.1-7.7 | retrieval-quality | yes | **No for 7.1-7.7**: docstring `:213-217` quotes the V2 formula; body `:295-330` computes V3 (context, superseded, staleness, power-law recency). The citation points at a real AC that no longer says what the test computes (F-2.1). |
| `tests/test_search.py` | 1.5, 3.1, 6.4 | retrieval-quality | yes | yes |
| `tests/test_mcp_server.py` | 1.6, 9.2-9.4 | retrieval-quality | yes | yes |
| `tests/test_depth.py` | 5.1, 5.3, 5.4 | retrieval-quality | yes | yes |
| `tests/test_classify.py` | 4.1-4.4 | retrieval-quality | yes | yes for outcomes; the AC text and design say the classifier takes `source_type`; the test calls the 2-arg live signature. |
| `tests/test_rerank_drift.py` | 3.1-3.6 | rerank-weights-single-source-of-truth | yes | yes |
| `tests/test_eval_properties.py`, `tests/test_capture_api.py` | — | evaluation-framework, capture-api | no citations at all | evaluation-framework Task 5.1 named four properties; the file exists but is untagged. capture-api Task 9 lists R1-R8; none referenced. |

**Summary.** Every citation resolves to an existing requirement number in the right spec
(no dangling numbers). Three citation *groups* point at ACs whose text no longer matches
what the code/test does: retrieval-quality 7.1-7.7 (formula), db-layer-hardening 3.2/4.3/5.2/7.3
(signature/SQL "pre-migration" baselines silently moved), project-auto-tagging 2.1-2.3 (premise).
Two requirement families with the largest live defects (retrieval-quality 8.1/8.2; project-auto-tagging
2.3-as-UUID) have **no test citing them**, or a test that cannot fail.

---

## 9. Cross-check 2 — docs/SPEC-INDEX.md vs `ls .kiro/specs`

| Claim (SPEC-INDEX.md) | Line | Verified | Evidence |
|---|---|---|---|
| "36 tracked specification documents across 12 families" | :9 | **partly** | `git ls-files .kiro/specs` = 48 files / 12 families (36 `.md` + 12 `.config.kiro`). "36" is right for `.md`; "12 families" is right for *tracked*. `ls .kiro/specs` = **13** dirs; the 13th (`direct-api-backends`, `?? untracked`) has requirements/design/tasks + a `tasks.meta.json` with 3 recorded task executions — it was *worked*, not just drafted. |
| Table rows | :13-24 | 12 rows, all 12 tracked families, all paths resolve | ✓ |
| "untracked `direct-api-backends` specification" | :28 | ✓ | `git status`: `?? .kiro/specs/direct-api-backends/` |
| "untracked `src/backends/direct_api.py` / `bedrock.py`" | :28 | **inverted** | Both are tracked; last commit `9f15696` ("price and adapt Nova 2 Lite / Gemma 3 12B for the skim stage"). |
| "the active resolver does not register a direct Bedrock adapter" | :28 | **inverted** (re-confirmed) | `src/backends/resolver.py:42-47`: `DEFAULT_ADAPTERS = {"kiro", "claude_code", "codex", "bedrock": BedrockInvoker}`. |
| "A docstring claiming registration is ahead of implementation" | :28 | **inverted** | The docstring is *behind* nothing; the doc is behind the code. |
| model-backend-adapters row: "These three adapters are registered" | :20 | **wrong** | four. |
| capture-api row: "experimental scheduling deprecates this route … Consult a dated installation inventory" | :14 | **understated** | The plist is live and the process is running (pid 42500); the "dated installation inventory" is not in the repo. What is true and unsaid: zero captures ever (F-5.1). |
| project-auto-tagging row: "Newer canonical policy separates workspace provenance from semantic project identity" | :21 | **unsourced** | `grep -rni "semantic project\|workspace provenance" docs src` → only this line. No such policy exists in the repo. What is true and unsaid: the column is 99.5 % NULL (F-6.1). |
| evaluation-framework row: "placeholder curated targets" | :19 | ✓ and understated | 31/31 placeholders; no results dir; nothing ever run (F-7.1). |

**New (S2) — duplicate `specId`s in `.config.kiro`.**
`retrieval-quality` and `byzantine-consensus-panel` both carry `"specId": "10f5a9f3-c806-405e-a539-a5afaaaa5710"`;
`rerank-weights-single-source-of-truth` and `model-backend-adapters` both carry `"5de1edaa-730e-4a17-b343-2aa380364889"`.
`evaluation-framework` has the non-UUID `"eval-framework-001"`. If any tooling keys on `specId`
(Kiro's spec workflow does), two pairs of specs are indistinguishable. Fix: regenerate the four ids.

Verdict on SPEC-INDEX.md: the table is sound; §"Unfinished local work" (:28) should be rewritten
to: *"`direct-api-backends` spec dir is untracked; its code (`direct_api.py`, `bedrock.py`) is
tracked and registered as the `bedrock` adapter; the spec should be committed or removed."*
Add the 13th row or say "12 tracked + 1 untracked".

---

## 10. Per-spec verdicts

| Spec | Verdict | One-line reason |
|---|---|---|
| db-layer-hardening | **update** | Contract is live; fix the `SimpleConnectionPool` rationale (F-1.1), record the probe (F-1.3), refresh caller inventory. |
| retrieval-quality | **supersede** | Formula section is false (F-2.1); Req 8.1 unsatisfiable (F-2.2). Keep for research rationale only. |
| question-aware-search | **keep-as-record** | Accurate. |
| rerank-weights-single-source-of-truth | **keep-as-record** | Accurate; `OVERLAP_WINDOW_CHARS` boundary nit (F-4.2). |
| capture-api | **archive** | Zero captures ever; premise superseded by `src/capture/*` (F-5.1); its source_type values are dead or colliding (F-5.2). |
| project-auto-tagging | **update** | Plumbing correct; both sources died; column carries no signal (F-6.1, F-6.2). |
| evaluation-framework | **update** | Code complete; curated data never authored; cold-mode drifted (F-7.1, F-7.2). |
| data-layer-decomposition (read for §1d only) | — | Its Property 2 formula (`design.md:611`) is V1; flag for Track A. |

## 11. Spec families the refactor needs that do not exist

1. **`rerank-formula` (normative, current)** — the only requirement-level formula in the repo is retrieval-quality Req 7.7 and it is wrong; V3 terms (context overlap, superseded, staleness, power-law recency, min-max-normalised `rrf_score`) have no home. (F-2.1, §4d)
2. **`source-type-vocabulary`** — 27 live values, three specs/docstrings each list a different subset, none enumerates the set, no constant, no constraint. (F-5.2)
3. **`sql-ownership` / data-access boundary** — who may write raw SQL against `memories`; today: `db.py`, `search.py`, `dream_cycle_db.py`, `express.py`, `skim/store.py`, ≥10 scripts. Neither db-layer-hardening nor data-layer-decomposition draws the line. (§1d)
4. **`connection-concurrency`** — thread-safety contract for `get_connection()` given FastAPI threadpool + two `ThreadPoolExecutor` scripts. (F-1.1)
5. **`encoding-context`** — column, migration 006, MCP parameter, rerank term, and whether it belongs in `search_vector`. Appears in code and INTERFACES.md only. (F-3.2)
6. **`schema-type` / knowledge graph** — `type='schema'`, `derived_from`, `find_schemas_for_memories`, `schema_context` in search results, migrations 007/008. No spec. (F-2.5)
7. **`project-tagging-v2`** — replacement sources for the two dead premises, plus the UUID cleanup. (F-6.1)
8. **`capture-channels`** — `src/capture/{slack,browser,outlook,codex,youtube}.py` are the real capture layer and have no spec family; capture-api is the spec for the layer that does not capture. (F-5.1)

## Could not verify

- Whether the `SimpleConnectionPool` race (F-1.1) has ever corrupted a request — no error-log search was done; read-only track, no pytest.
- Whether `evaluations/results/` once existed and was deleted vs. never created (gitignored; no history).
- Whether `get_eval_metadata()` emits `script_version` (Req 5.5) — function located, body not read.
- Task 7 of rerank-weights (transient perturbation check) — unverifiable from artefacts by design.
- IDE `.chat` sample covers `kiro.kiroagent` dir only (12,398 files); the legacy `amazonwebservices.aieditoragent` dir (`ide_chat.py:5`) was not scanned.
- The 88 `source_type IS NULL` rows: which writer produced them was not traced.
- Whether the three 2026-09-14 capture-API POSTs (422/401/422) were a manual smoke test or a misconfigured client.
- `tasks.meta.json` in `direct-api-backends` — 3 executions recorded for task 1; later tasks not inspected (Track A scope).
