# 09a — Search & Ranking (hybrid_search / rerank) — second-pass narrow review

Repo: /Users/kerrfir/second-brain @ HEAD 6b8c2d2
Scope: src/search.py, src/rerank_weights.py, tests/test_rerank.py, tests/test_rerank_drift.py, tests/test_search.py, tests/test_question_search.py
Mode: read-only; this file is the only write. No pytest, no scripts, no MCP calls; DB via read-only SELECT/EXPLAIN only.

Status: COMPLETE (appended incrementally; see Findings index and "Could not verify" at the end)



## Prior-track context (read briefly)

- `docs/audit-2026-09-23/04-data-layer-search-mcp.md:103` (SET LOCAL relies on implicit txn, LOW) and `:186` (test_rerank docstring omits three terms, MEDIUM); `docs/audit-2026-09-24/02-verify-prior-audit-B.md:164` re-confirmed the docstring omission by reading only.
- `docs/audit-2026-09-24/06-live-data-quality.md:178-205`: 661 chunks > 25k (embedding truncated), 14,338 chunks > 4k, parents "not embedded by design".
- `docs/audit-2026-09-24/08b-specs-vs-code-B.md:149`: `encoding_context` is not in `search_vector`; `:273`: UUID-shaped `project` values make `project_penalty` a noise floor.

Line numbers below are from `src/search.py` @ 6b8c2d2 (read in full: 356 lines). `hybrid_search` = :45-163, `compute_spacing_bonus` = :166-185, `token_overlap` = :190-213, `rerank` = :216-343, `increment_access_count` = :346-356.

---

## 1. The rerank formula — one reconciled table

Computed in `rerank()` (`search.py:322-336`), compared with the canonical constants (`src/rerank_weights.py`), the independent oracle in `tests/test_rerank.py` (docstring :214-219 vs. asserted formula :338-352), and what `tests/test_rerank_drift.py` pins.

| # | Term (as summed at search.py:322-336) | Constant used in code | rerank_weights.py | test_rerank.py docstring (:214-219) | test_rerank.py asserted (:338-352) | test_rerank_drift.py |
|---|---|---|---|---|---|---|
| 1 | `COEFFICIENTS["rrf"] * r.get("rrf_score", 0)` | 0.30 | `"rrf": 0.30` (:20) | `0.30 * rrf_score` ✓ | `0.30 * rrf_score` ✓ | not pinned (imports nothing about it; agreement test only) |
| 2 | `COEFFICIENTS["overlap"] * overlap` (content∪title tokens, content window 20k) | 0.18 | `"overlap": 0.18` (:21) | `0.18 * token_overlap` ✓ | `0.18 * expected_overlap` ✓ (content[:20000] literal) | — |
| 3 | `COEFFICIENTS["title_overlap"] * title_overlap` | 0.18 | `"title_overlap": 0.18` (:22) | ✓ | ✓ | — |
| 4 | `COEFFICIENTS["context_overlap"] * context_overlap` (encoding_context tokens) | 0.10 | `"context_overlap": 0.10` (:23) | **ABSENT** | `0.10 * expected_context_overlap` — but expected is hard-coded `0.0` (:290) and `encoding_context=None` (:277), so the coefficient is **never exercised** by the oracle | branch presence only (`_context_overlap > 0` for one row, :226, :312); value not pinned |
| 5 | `COEFFICIENTS["recency"] * recency`, recency = `(1 + days/S)^-0.8`, `S = 30 + 10·log1p(access)`; `0.35` if no created_at | 0.12 (+ literals 30.0, 10.0, 0.8, 0.35 inline at :262-267) | `"recency": 0.12` (:24). **30/10/0.8/0.35 are not in rerank_weights.py** | `0.12 * recency` ✓ (shape of recency undocumented) | `0.12 * expected_recency` with 30/10/0.8 re-declared (:294-296) ✓; the 0.35 no-date fallback is **untested** | — |
| 6 | `COEFFICIENTS["length"] * min(1, len(content_tokens)/80)` | 0.08 (+ literal 80 at :283) | `"length": 0.08` (:25); **80 not in rerank_weights.py** | `0.08 * length_score` ✓ | ✓ (80 re-declared :299) | — |
| 7 | `COEFFICIENTS["depth"] * metadata.depth_score` | 0.05 | `"depth": 0.05` (:26) | `0.05 * depth_score` ✓ | ✓ | — |
| 8 | `type_boost` | 0.06 if type ∈ {idea, synthesis, insight, decision} | `TYPE_BOOST=0.06`, `TYPE_BOOST_TYPES` (:31-32) | `+ type_boost` (magnitude undocumented) | 0.06 + tuple re-declared (:302) ✓ | imports `TYPE_BOOST_TYPES` (:29) — **not independent** |
| 9 | `mem_class_boost` | semantic 0.04, procedural 0.02 | `MEM_CLASS_BOOST` (:33) | `+ mem_class_boost` | dict re-declared (:305) ✓ | imports `MEM_CLASS_BOOST` (:29) — **not independent** |
| 10 | `reinforcement = 0.03 · log1p(access) · spacing_bonus` | 0.03 | `REINFORCEMENT_COEFF=0.03` (:29) | `0.03 * log1p(access_count) * spacing_bonus` ✓ | ✓ (:311) | — |
| 11 | `project_penalty` | −0.15 when query_project and mem_project differ (both non-empty) | `PROJECT_PENALTY=-0.15` (:34) | `+ project_penalty` | −0.15 re-declared (:314) ✓ | branch presence only |
| 12 | `superseded_penalty` | −0.20 when status == "superseded" | `SUPERSEDED_PENALTY=-0.20` (:35) | **ABSENT** | `expected_superseded_penalty = 0.0` hard-coded (:317), status fixed "active" (:271) → coefficient **never exercised** | branch presence only (`< 0`) |
| 13 | `staleness_penalty` | −0.05 · min(1, (days−90)/180) when access == 0 and days > 90 | `STALENESS_PENALTY_MAX=-0.05` (:36); **90 and 180 are inline literals at :275-276** | **ABSENT** | −0.05, 90, 180 re-declared (:322-323) ✓ — *is* exercised (days_old up to 365, access_count may be 0) | branch presence only |

**Confirmed:** the class docstring at `tests/test_rerank.py:214-219` omits terms 4, 12 and 13 (`context_overlap`, `superseded_penalty`, `staleness_penalty`), all three of which are summed at `:338-352` of the same file. The prior claim stands.

**New, beyond the prior claim:**

- **F1.1 (S2) — the "complete formula" oracle cannot detect a wrong `context_overlap` or `superseded_penalty` coefficient.** `test_rerank.py:277` fixes `encoding_context=None` and `:271` fixes `status="active"`, so `expected_context_overlap = 0.0` (:290) and `expected_superseded_penalty = 0.0` (:317). If `rerank_weights.py:23` were changed to `"context_overlap": 1.0` or `:35` to `SUPERSEDED_PENALTY = +5.0`, every test in the repo that pins values would still pass: `TestRerankEncodingContextOverlap` (:389-399) asserts only `> 0`, the drift test asserts only production == eval (both read the same constant). The module docstring of `rerank_weights.py:7-10` promises exactly this detection ("a wrong value in this canonical source is detected rather than verified against itself") — untrue for two of the thirteen terms. *Fix:* draw `encoding_context ∈ {None, "alpha zeta"}` and `status ∈ {"active","superseded"}` in the Hypothesis strategy at :222-237 and compute the expected values with literals.
- **F1.2 (S3) — the drift guard imports the constants it claims to pin.** `test_rerank_drift.py:29` `from src.rerank_weights import TYPE_BOOST_TYPES, MEM_CLASS_BOOST` and uses them at `:229-233, :293-297`. Since it compares production against eval and both import the same module, the only thing it can catch is *structural* drift between `rerank()` and `rerank_with_overrides()`, not a wrong value. Its module docstring (:3-6) is accurate about that; the prior-audit phrasing "constants it is supposed to independently pin" is slightly unfair — the file never promised independence. Downgrade to S3; no fix needed beyond a one-line docstring clarification.
- **F1.3 (S3) — five magic numbers live in `rerank()` instead of `rerank_weights.py`.** Recency stability base `30.0`, growth `10.0`, exponent `0.8`, no-date fallback `0.35` (`search.py:265-269`); length cap `80` (`:283`); staleness knee `90` and ramp `180` (`:275-276`). `rerank_weights.py:1` calls itself "the single source of truth"; the eval scorer (`scripts/eval/eval_common.py`, out of scope, not read) cannot override them through `PRODUCTION_WEIGHTS` if it reads only that module. *Fix:* `RECENCY = {"stability_days": 30.0, "stability_per_log_access": 10.0, "exponent": 0.8, "no_date": 0.35}`, `LENGTH_CAP_TOKENS = 80`, `STALENESS_KNEE_DAYS = 90`, `STALENESS_RAMP_DAYS = 180`.
- **F1.4 (S3) — `rerank()` docstring (`search.py:217`) lists four signals** ("recency, type boost, token overlap, access reinforcement") for a thirteen-term sum. Same family as the test docstring defect.
- **F1.5 (S3) — `rrf_score` default.** `r.get("rrf_score", 0)` (`:323`) means a row that bypassed `hybrid_search` (e.g. `search_similar` output fed to `rerank`) scores as if it were the *worst* fused candidate, while a `hybrid_search` singleton scores 1.0 (`:160`). Callers outside scope not audited; note the asymmetry.

Formula-magnitude sanity (for §2 reasoning): maximum achievable positive sum = 0.30+0.18+0.18+0.10+0.12+0.08+0.05+0.06+0.04+0.03·log1p(access) ≈ 1.11 + reinforcement (unbounded: access=1000 → +0.207). Penalties sum to at most −0.40. `rrf_score` (the only relevance signal from retrieval) is 0.30 of ~1.1 — the two overlap terms together (0.36) outweigh it, so an FTS/vector rank-1 result with low lexical overlap (a paraphrase) loses to a rank-40 result that repeats the query words in its title.



---

## 2. `hybrid_search` step by step (`search.py:45-163`)

Live facts used below (read-only SELECTs, 2026-09-24): `memories` = 124,280 rows; 115,403 with `embedding`; chunks (`parent_id IS NOT NULL`) = 75,370, all embedded; parents-with-children = 7,790, **none embedded**, all with `search_vector`; top-level rows without children = 41,120 of which 40,033 embedded (1,087 not). pgvector 0.8.2, PG 17.10, `hnsw.ef_search=200` (database-level `ALTER DATABASE`), `hnsw.iterative_scan` default `off`, `hnsw.max_scan_tuples=20000`. Embedded rows by status: active 115,396 / superseded 7. Embedded rows by type: source 110,501 (96 %), insight 2,636, decision 2,201, synthesis 27, connection 20, correction_episode 16.

### 2.1 Constants and WHERE (`:50-73`)
- `k = 60`, `prefetch = limit * 4` (default 40). Both legs `LIMIT prefetch`; `absent_rank = prefetch + 1`.
- One shared `where` (`:53-68`) for **both** legs, always starting with `embedding IS NOT NULL`. Consequence (**F2.1, S2**): the full-text leg can never return a row that has no embedding, even though `search_vector` is populated for every row (`search_vector IS NULL` count = 0). This silently excludes (a) all 7,790 chunked parents — arguably intended, see §2.5 — and (b) the 1,087 never-embedded top-level rows (the QD leaves the live-data track flagged) and (c) any row whose Titan call failed. The FTS leg is the one place those rows *could* be found; the shared predicate removes it. *Fix:* build the FTS `where` without the `embedding IS NOT NULL` term, or make the exclusion of chunked parents explicit (`parent_id IS NOT NULL OR NOT EXISTS(children)`) so that the reason is visible.
- `filtered` (`:73`) is true for any filter → `SET LOCAL hnsw.iterative_scan = relaxed_order` (`:78`). See §3.

### 2.2 Vector leg (`:80-87`)
`SELECT id, 1 - (embedding <=> q) AS similarity … ORDER BY embedding <=> q LIMIT prefetch`. Only the *rank* is kept (`vec_results = {id: i+1}`, `:87`); `similarity` is computed and discarded (**F2.2, S3** — dead column, one extra distance evaluation per row; drop it or keep it as `_vec_sim` for eval).

EXPLAIN (plain) on the live DB:
- unfiltered, `status='active'`, `project=…`: `Index Scan using idx_memories_embedding` with the filter applied post-scan → HNSW path, iterative scan relevant.
- `type='decision'`, `source_type='slack'`, `created_after=now()-30d`: planner picks the **btree** (`memories_type_status_idx`, `memories_source_type_idx`, `memories_created_at_idx`) + exact Sort — the HNSW index is not used at all, so recall is exact and `hnsw.iterative_scan` is irrelevant. Selective filters are therefore *safer* than the comment at `:70-72` implies; the comment is right only for non-selective filters (`status`, `project`, `type='source'`).

### 2.3 Full-text leg (`:89-112`)
Tokenisation: `re.sub(r"[^\w\s]", " ", query_text).split()` — Python `\w` is Unicode-aware. Verified by SELECT that every resulting token is tsquery-safe (the only tsquery metacharacters `& | ! ( ) : ' < > *` are all non-`\w`).

Passes: for ≥2 terms, `" & ".join(terms)` first, then `" | ".join(terms)` only if the AND pass returned fewer than `prefetch` ids (`:99-112`). Ranks are 1-based positions in the union list `ordered`; OR hits already present keep their AND rank. Both passes use `ts_rank(search_vector, q)` with **no normalisation argument** (`:93`).

Edge cases (each checked with a pure SELECT unless marked "by reading"):
| Case | Behaviour | Note |
|---|---|---|
| empty query `""` / only punctuation | `terms == []` → FTS leg skipped entirely (`:91`); vector-only. | matches `TestHybridSearchEmptyFtsQuery`. |
| one token | single `" | "` pass (`:99`). | fine. |
| all stopwords (`"the and of"`) | `to_tsquery('english','the & and & of')` → empty tsquery + NOTICE; `search_vector @@ ''` is false for every row → 0 FTS hits, no error. | psycopg2 accumulates NOTICEs in `conn.notices` (capped at 50) — harmless. |
| stopword + real word | `'the & skim'` → `'skim'` (stopword dropped). | fine. |
| `_` alone / `foo_bar` | `'_'` → empty query; `'foo_bar & baz'` → `'foo' <-> 'bar' & 'baz'` (parser splits on underscore into a **phrase**). | identifiers like `hnsw_ef_search` become phrase queries — stricter than the OR-fallback intends. |
| non-ASCII (`café naïve 東京`) | `'café' | 'naïv' | '東京'`; tsvector side indexes the same lexemes. | OK. Full-width `ａｂｃ` kept as one lexeme (not NFKC-folded) — a row containing ASCII `abc` will not match. |
| digits / decimals (`"3.14"`) | regex turns `.` into space → `'3' & '14'`. | AND of two number tokens; unlikely to match what the user meant. |
| hyphenated (`write-through`) | → `write & through` (AND). tsvector of `write-through` contains `write-through`, `write`, `through` → still matches. | OK. |
| > 20 tokens (25-word natural-language query) | AND pass almost surely empty; OR pass = 25-way OR ranked by un-normalised `ts_rank`. EXPLAIN of a 5-way OR on common terms already shows **Parallel Seq Scan over ~51k rows** with `ts_rank` computed per row (planner abandons the GIN index when the OR is unselective). | **F2.3, S2**: long queries degrade to "any word" ranking that favours the longest documents (see next row) and cost a full-table `ts_rank`. *Fix:* cap terms (e.g. 12), drop very common tokens, or use `websearch_to_tsquery`/`plainto_tsquery` phrase semantics for the AND pass. |
| `ts_rank` without normalisation | `ts_rank(sv, q)` defaults to normalisation 0: raw weighted term-frequency, no document-length correction. A 360k-char chunk (live max) with the word 50 times outranks a 400-char insight with it once. | **F2.4, S2**: the FTS leg is structurally biased toward the oversize chunks the live-data track flagged (14,311 > 4k). *Fix:* `ts_rank_cd(sv, q, 32)` or `ts_rank(sv, q, 1|32)` (log-length normalisation), and measure with the eval set. |
| chunk vs parent | see §2.5 | |

### 2.4 RRF fusion and min-max normalisation (`:114-123`, `:143-161`)
- `rrf = 1/(60+vec_rank) + 1/(60+fts_rank)` with absent rank `prefetch+1`. For `prefetch=40`: max `2/61 = 0.0328` (rank 1 in both), min `1/100 + 1/101 = 0.0199` (rank 40 in one leg, absent in the other). The comment at `:146` says raw RRF "lives in [0, 2/(k+1)]" — the lower bound is `1/(k+prefetch)+1/(k+prefetch+1)`, never 0 (**F2.5, S3**, comment accuracy).
- Sort is by `rrf` only; ties (same rank pair) keep `set` iteration order of `all_ids` — **non-deterministic across processes** (Python hash randomisation of str keys) (**F2.6, S3**; `tests/test_search_properties.py` claims determinism for `rerank`, not for this step — not read, out of scope). *Fix:* `scored.sort(key=lambda x: (-x[1], x[0]))`.
- `lo, hi = scored[-1][1], scored[0][1]` (`:150`) are taken over **all** fused ids (up to `2·prefetch`), but only `scored[:prefetch]` is fetched and eligible (`:127`). So the fetched pool's minimum `rrf_score` is > 0 whenever the legs are disjoint; only rows *never returned* would have scored 0. Harmless, but the comment "min-max normalised over the fused candidate set" is loose.
- Singleton fused set → `span == 0` → `rrf_score = 1.0` (`:160`). Two candidates → exactly {1.0, 0.0}: the second-best result is scored as if it had zero retrieval relevance and its order then depends entirely on priors (`rerank` term 1 = 0). (**F2.7, S3**) *Fix:* rank-based normalisation `1 - (i/(n-1))·0.5` or an absolute scale `rrf / (2/(k+1))` — the latter is what the comment at `:143-149` actually argues for.

### 2.5 Dedup and per-parent cap (`:150-163`)
- `ckey = normalised content[:300]`; `pkey = parent_id or own id`; at most 2 rows per `pkey`; stop at `limit`.
- **Chunk and its parent both present?** Structurally impossible today: parents-with-children have `embedding IS NULL` (7,790 / 7,790 live) and both legs require `embedding IS NOT NULL`, so a parent never enters `scored`. The per-parent cap therefore only ever collapses *sibling chunks*. If a parent were ever embedded (e.g. a future backfill), parent and chunk would share `pkey` and count toward the same cap of 2, but nothing would prevent both appearing; the chunk could outrank the parent since RRF treats them as independent rows. Not a defect now; a latent assumption with no test (`TestHybridSearchDedup.test_per_parent_cap` embeds the parent, so the test corpus does **not** reflect production, where parents are unembedded).
- **F2.8 (S1) — dedup after a `4×limit` prefetch under-returns catastrophically on the live corpus.** Measured with the exact nearest-neighbour SQL equivalent of the vector leg (query = a live row's own embedding, `LIMIT 40`):
  - `kiro_ide_chat` chunk (offset 500): 40 neighbours → **1** distinct `content[:300]` key → `hybrid_search(limit=10)` would return **1 row**.
  - `kiro_cli_chat` chunk (offset 300): 40 neighbours → 7 distinct keys / 6 parents → ≤ 7 rows.
  - `insight` (offset 100): 40 → 20 distinct keys / 40 parents → 10 rows, OK.
  The live-data track reports 92 % of `kiro_ide_chat` chunks (62,384) are byte-identical to another chunk, so for any query whose neighbourhood is chat-heavy the candidate pool is spent on copies before `limit` is reached. `hybrid_search` has no mechanism to refill. The docstring promise "still returning `limit` distinct rows" (`:126`) does not hold. *Fix (cheap):* dedup inside the SQL legs (`DISTINCT ON (md5(left(content,300)))` or a stored `content_key` column with an index) so the `LIMIT prefetch` counts distinct rows; *fix (cheaper still):* loop — if `len(out) < limit` and `len(scored) > prefetch`, continue into `scored[prefetch:]`; *fix (root):* the ingest dedup the live-data track proposes.
- `ckey` empty (content NULL/whitespace) → not deduped (`if ckey and …`, `:154`) but still counted in `per_parent`. Fine.

### 2.6 Filters (`:55-68`)
- `project` → `(project = %s OR project IS NULL)` — inclusive by design; with 99.5 % NULL (08b-track) it filters almost nothing.
- `status` is *not* defaulted to `'active'` — superseded rows (7 live) are retrievable and only penalised −0.20 in `rerank`. Intentional per interference-theory comment (`:296-299`); note only.
- No `parent_id IS NULL`/`IS NOT NULL` filter exists, so callers cannot ask for "documents, not chunks".



---

## 3. `SET LOCAL hnsw.iterative_scan` and the implicit transaction (`search.py:77-78`)

What actually bounds the setting today (all read from code, psycopg2 2.9.11 in `.venv`):
1. `db.get_connection()` (`src/db.py:28-64`) checks a connection out of a `SimpleConnectionPool(minconn=DB_POOL_MIN=1, maxconn=5)`, probes `SELECT 1`, then **`rollback()`** (`db.py:48`) → the connection is yielded in `IDLE` state.
2. psycopg2 default `autocommit=False` → the `SET LOCAL` at `:78` implicitly opens a transaction; the vector, FTS and candidate-fetch statements all run inside it.
3. `hybrid_search` never commits or rolls back on the success path. `get_connection`'s `finally: _pool.putconn(conn)` → `AbstractConnectionPool._putconn` (psycopg2 2.9.11, read from source): if `len(self._pool) < minconn` it inspects `transaction_status` and **rolls back** anything not IDLE; otherwise it **closes** the connection outright. Either way the `SET LOCAL` scope ends.

So the setting is correctly scoped **by accident of three other components** (probe-rollback, autocommit default, pool return policy). Findings:

- **F3.1 (S3, confirms prior LOW)** — one `conn.autocommit = True` anywhere on this pool turns `:78` into a no-op: PostgreSQL emits `WARNING: SET LOCAL can only be used in transaction blocks` and continues; psycopg2 does not raise. No test would notice (the tests only check result sets). *Fix:* wrap `:77-141` in an explicit `with conn:` block or `cur.execute("BEGIN")`; or assert `not conn.autocommit`.
- **F3.2 (S3)** — with `minconn=1`, any concurrent second connection is **closed on return**, so under any concurrency most `hybrid_search` calls run on a **fresh backend** in which `vector.so` is not yet loaded (the `SELECT 1` probe does not load it; verified live: `SHOW hnsw.iterative_scan` errors "unrecognized configuration parameter" in a fresh session and works after one `<=>` evaluation). `SET LOCAL hnsw.iterative_scan` in that state creates a *placeholder* GUC that PostgreSQL validates and applies when the module loads on the next statement. That is documented behaviour and should work, but it is the *common* path, not the corner case, and I could not exercise it under the read-only rule (see "Could not verify"). *Fix:* `shared_preload_libraries = 'vector'` or `ALTER DATABASE memory_bank SET hnsw.iterative_scan = relaxed_order` next to the existing `hnsw.ef_search=200` database setting — which also removes the code path entirely.
- **F3.3 (S3)** — `relaxed_order` returns rows **not strictly in distance order** (pgvector README: wrap in a materialised CTE and re-`ORDER BY` to restore order). The query at `:82-86` has `ORDER BY embedding <=> q` but that ORDER BY is satisfied by the index scan itself, so no re-sort happens; `vec_rank` (`:87`) is then the scan order, not the true distance order. The code computes `similarity` and throws it away (F2.2) — sorting `cur.fetchall()` by `similarity DESC` before ranking would fix this at zero cost.
- Observation — `:78` executes for every filter, but EXPLAIN shows the planner only uses the HNSW index for the non-selective filters (`status`, `project`, and by inference `type='source'`); for `type`, `source_type`, `created_after` it uses a btree and an exact sort, where the GUC does nothing. Not a defect.

---

## 4. Embedding truncation vs. FTS reach (precise statement)

Pipeline facts (by reading; `src/embeddings.py:24` per prior audit — `text[:25000]`; `src/ingest.py` not re-read here): the vector for a row is computed from `content[:25000]`; `search_vector` is computed by trigger from the **full** `title || questions` (A) and `remaining_content` (B) — `tests/test_question_search.py:503-513` shows the backfill SQL, which has no length cut. tsvector limits (PG docs): positions > 16,383 are clamped to 16,383, ≤ 256 positions per lexeme, ≤ 1 MB per tsvector — lexeme *presence* is preserved for any content < ~1 MB, so `@@` matching is unaffected; only `ts_rank`'s positional weighting saturates.

Live counts (this run): chunks > 25,000 chars = **661** (all embedded); chunks > 20,000 = 911; chunks > 4,000 = 14,311 (of 75,370); top-level embedded rows > 25,000 = **6**; > 20,000 = 7.

Precise statements:

| Row class | Vector leg sees | FTS leg sees | `token_overlap` (rerank) sees |
|---|---|---|---|
| **Chunked parent** (7,790 rows, `embedding IS NULL` by design) | nothing — never a candidate | **nothing** — excluded by the shared `embedding IS NOT NULL` (F2.1), despite a complete `search_vector` | n/a (never in results) |
| **Chunk ≤ 25,000** (74,709) | full text (one vector; for the 13,650 chunks in 4k–25k the vector is diluted over ≥ 4k chars — precision, not reach) | full text | first 20,000 chars (`OVERLAP_WINDOW_CHARS`); **250 chunks in 20k–25k** lose their tail here even though the embedding covered it |
| **Chunk > 25,000** (661, max 360,823) | `content[:25000]` only — text past 25k is **vector-unreachable** | full text — **FTS-reachable** via `@@`; `ts_rank` positions clamp at 16,383 tokens (~100k chars) | first 20,000 chars — **rerank-unreachable** past 20k |
| **Top-level embedded > 25,000** (6) | `[:25000]` | full | `[:20000]` |
| **Top-level never embedded** (1,087) | nothing | **nothing** (F2.1) | n/a |

So the statement "text past char 25,000 of an embedded row is FTS-reachable but vector-unreachable" is exact for the 661 + 6 rows, **and** there is a second, narrower window nobody has stated: text in 20,000–25,000 is embedded and FTS-indexed but invisible to the two 0.18-weight overlap terms (F4.1, S3; 250 chunks + 1 top-level). For parents the correct statement is *not* "FTS-reachable": they are unreachable through `hybrid_search` on both legs; their text is reachable only through their chunks, whose FTS coverage is complete. Net: the FTS leg is the only leg with full-text reach, and it is (a) length-biased (F2.4) and (b) gated by the vector-oriented `embedding IS NOT NULL` predicate.

*Fix:* record `embedded_chars` (or `embedding_truncated bool`) on the row at embed time so the retrieval layer can expose it (`_truncated` signal) and the eval set can measure the effect; re-chunk the 661 (live-data track).

---

## 5. `token_overlap` (`search.py:187-213`) and its callers (`:225-238`)

Definition: hit if `q in doc_tokens` **or** (`len(q) ≥ 4` and ∃ t: `t.startswith(q)` or (`len(t) ≥ 4` and `q.startswith(t)`)); score = hits / |query_tokens|. Tokens = `re.sub(r"[^\w\s]", " ", s).lower().split()` on both sides; content side truncated to `[:OVERLAP_WINDOW_CHARS]=20000` **before** the regex (`:226`).

Failure cases (by reading; each is a concrete input):

| # | Input | Result | Why it is wrong |
|---|---|---|---|
| F5.1 (S2) | query `"skim"` vs doc token `"skimmed"` (the documented motivating example) | hit ✓ | works. But query `"stage"` also hits `"stagecoach"`, `"staged"`, `"stagehand"`; query `"data"` (4 chars) hits `"database"`, `"datasets"`, `"datastore"`, `"datagram"` — every 4-letter query token becomes a prefix wildcard. Because `token_overlap` is 0.36 of the score (two terms), a query `"data model"` credits full overlap to a doc containing `"database"` and `"modeling"`. The FTS leg does *not* do this (stemmer only). *Fix:* raise `OVERLAP_PREFIX_MIN` for the *query* side to 5, cap suffix length (`len(t) - len(q) ≤ 3`), or reuse Postgres' stem via a tiny `SELECT to_tsvector(...)` once per query. |
| F5.2 (S3) | query token `"test"` vs doc token `"testing"` and `"tests"`; doc token `"retest"` | first two hit; `"retest"` no | asymmetric: prefix only, no suffix/infix; documented, acceptable. |
| F5.3 (S2) | doc token `"the"` (3 chars) vs query `"theory"` | `len(t) ≥ 4` guard → no hit ✓ — but doc token `"them"`/`"then"`/`"they"`/`"this"`/`"that"`/`"with"`/`"from"`/`"have"` (4 chars, all stopwords) vs query `"thematic"`, `"thenceforth"`, `"thistle"`, `"withdraw"`, `"fromage"`, `"haven"` → **hit**. There is no stopword list on either side; virtually every English document contains `this/that/with/from/have`, so any query token starting with one of them scores an overlap hit against **every** document. Same for `"time"`→`"timeline"`, `"user"`→`"username"`. *Fix:* apply a small stopword set (reuse PG's english list or a 50-word inline set) before the prefix scan; require `len(t) ≥ OVERLAP_PREFIX_MIN + 1` on the doc-prefix branch. |
| F5.4 (S3) | punctuation: `"write-through"` → `write`, `through`; `"O'Reilly"` → `o`, `reilly`; `"C++"` → `c`; `"node.js"` → `node`, `js`; `"e.g."` → `e`, `g`; URLs `https://x.y/z` → `https x y z`; `"rrf_score"` → kept whole (`_` is `\w`) | tokens ≤ 3 chars can only match exactly, so `c`, `o`, `e`, `g`, `js` inflate the denominator and are easy exact hits on any doc containing the letter `c`… Query `"C++ vs Go"` → tokens `{c, vs, go}`: any doc with a stand-alone `c` token (from `C#`, `c)`, `(c)`) scores 1/3, and `go` exact-matches the verb in most prose. | denominator counts junk; numerator hits on noise. *Fix:* drop tokens of length < 2 (or < 3 when not numeric) on the query side. |
| F5.5 (S3) | identifiers with underscores: query `"hybrid_search"` vs doc `"hybrid_search("` → `hybrid_search` both sides ✓; vs doc prose `"hybrid search"` → tokens `hybrid`, `search` → `"hybrid_search".startswith("hybrid")` with `len(t)=6 ≥ 4` → **hit** (score 1.0) | accidental but arguably good; FTS side meanwhile turns `hybrid_search` into a **phrase** `'hybrid' <-> 'search'` (§2.3). The two legs treat the same token differently; neither is documented. |
| F5.6 (S3) | 20k window (`:226`): a 25k-char chunk with the query terms in the last 5k → `overlap = 0`, `length_score` still 1.0 | 250 live chunks in the 20k–25k band and 661 above; `rerank_weights.py:39-44` says "20k covers every chunk" — **false on live data** (911 chunks > 20k). *Fix:* correct the comment; consider `content[:W]` + `content[-W/4:]` or the stored `depth`/summary. |
| F5.7 (S3) | unicode case: `"İstanbul".lower()` → `"i̇stanbul"` (combining dot) ≠ `"istanbul"`; full-width `ＡＰＩ`.lower() ≠ `api`; `"ß"` vs `"ss"` | no hit | `str.lower()` without `casefold()`/NFKC; the FTS side has the same limitation (verified: `'ａｂｃ'` lexeme kept as is). Low frequency in this corpus. |
| F5.8 (S3) | `title_overlap` uses `title_tokens` only, `overlap` uses `content ∪ title` (`:227-229`) — so title words are counted **twice** (0.18 + 0.18) while body words count once. | a 3-word title that repeats the query scores 0.36 before any content is read | by design? undocumented; the test docstring calls them "token_overlap" and "title_overlap" as if independent. Note only. |
| F5.9 (S3) | `context_overlap` (`:232-238`) runs the same prefix rule over `encoding_context` ("debugging auth flow"). Query `"debug"` ≥ 4 → hits `"debugging"`; query `"auth"` hits `"authentication"`, `"author"`. | with only 0.10 weight and sparse population, low impact | |

Complexity: exact-miss path is O(|Q|·|D|) per row (`:208-210`, the `any(...)` scans all doc tokens per missed query token), with |D| up to ~3,000 distinct tokens for a 20k window and |Q| ≈ 5 → ~15k `startswith` per row × 40 rows; fine (~ms), matches the "~1 ms" claim in `rerank_weights.py:44`.



Side note on `tests/test_question_search.py` (in scope, read to :740): it tests `src/depth.extract_questions` and the migration-005 trigger, not `search.py`. Its relevance here is that the trigger gives `title || questions_text` weight **A** and body weight **B**; `ts_rank`'s default weight vector is `{D 0.1, C 0.2, B 0.4, A 1.0}`, so a title/question hit is worth 2.5× a body hit in the FTS leg — and `rerank` then adds `title_overlap` on top (F5.8). Title matches are therefore triple-counted across the pipeline (FTS A-weight, `overlap`, `title_overlap`). `encoding_context` is in neither `search_vector` nor any leg (08b-track F-3.2 confirmed).

---

## 6. Proposed decomposition of `hybrid_search` (≤ 6 pure functions) and the regression seam

Today `hybrid_search` (119 lines, complexity E-31 per the first review) interleaves: SQL building, two DB round-trips (three with the OR top-up), Python fusion, normalisation and dedup, with one connection open throughout. Everything after the SQL round-trips is pure and testable without a database.

```
def build_filters(type, status, project, source_type, created_after) -> tuple[sql.Composed, list]   # :53-69
def tokenize_query(query_text) -> list[str]                                                         # :95  (shared with rerank :224 — one definition)
def fts_passes(terms) -> list[str]            # [] | ["a | b"] | ["a & b", "a | b"]                  # :99-100
def rrf_fuse(vec_ranks: dict[str,int], fts_ranks: dict[str,int], k, absent_rank) -> list[tuple[str,float]]   # :114-123 (+ deterministic tie-break)
def normalise_rrf(scored) -> dict[str,float]                                                        # :150-160
def dedup_candidates(scored_ids, rows_by_id, limit, per_parent_cap=2, key_chars=300) -> list[dict]  # :151-163
```
Plus one thin impure orchestrator `hybrid_search(...)` that (1) builds filters, (2) runs `_vector_leg(cur, ...)` and `_fts_leg(cur, ...)` — the only two functions touching `cur` — (3) calls the four pure functions in order. Six pure functions; two SQL runners; one orchestrator.

**The regression seam:** `rrf_fuse` + `normalise_rrf` + `dedup_candidates` consume only `dict[str,int]` rank maps and `dict[str,dict]` rows — both trivially serialisable. A cheap regression check is therefore: capture, for ~20 golden queries against the live DB, the two rank maps and the candidate rows *once* (JSON, a few hundred KB), commit them as fixtures, and assert that `dedup_candidates(normalise_rrf(rrf_fuse(vec, fts)))` reproduces the recorded `(id, rrf_score)` list byte-for-byte. That pins the entire post-SQL behaviour without a database, hypothesis, or embeddings, and is the file to diff when F2.6/F2.7/F2.8 are fixed (each will change it deliberately). The same fixtures feed `rerank()` directly, so the `test_rerank_drift.py` agreement test could also run DB-free.

For the SQL side, the seam is `build_filters` (already effectively tested by `tests/test_pool_properties.py` Property 4 per 08b-track) plus one `EXPLAIN`-shaped test per leg asserting the index name in the plan (`idx_memories_embedding` / `idx_memories_search_vector`) so a future predicate change that flips to a Seq Scan (F2.3) is caught.

Ordering suggestion: extract `tokenize_query` first (removes the duplicated regex at `:95`, `:224`, `:226-227`, `:235`), then `rrf_fuse`/`normalise_rrf`/`dedup_candidates` with the fixture test, then the SQL runners. The `SET LOCAL` (F3.1) moves into `_vector_leg`, which is also where the `similarity`-sort for F3.3 belongs.

---

## Findings index

| ID | Sev | Where | One line |
|---|---|---|---|
| F2.8 | **S1** | `search.py:126-163` + live data | Post-fetch dedup over a 4×limit pool under-returns: live kiro_ide_chat neighbourhood → 40 candidates, 1 distinct → `limit=10` yields 1 row. |
| F1.1 | S2 | `tests/test_rerank.py:271,277,290,317` | "Complete formula" oracle fixes `encoding_context=None`, `status="active"` → cannot detect a wrong `context_overlap` or `SUPERSEDED_PENALTY` value; contradicts `rerank_weights.py:7-10`. |
| F2.1 | S2 | `search.py:53,93-97` | FTS leg inherits `embedding IS NOT NULL`; 7,790 parents + 1,087 unembedded rows are unreachable by full text despite complete `search_vector`. |
| F2.3 | S2 | `search.py:99-112` | Long queries: AND empty → N-way OR → Parallel Seq Scan with per-row `ts_rank` (EXPLAIN), "any word" ranking. |
| F2.4 | S2 | `search.py:93` | `ts_rank` with no normalisation → length bias toward the 14,311 oversize chunks. |
| F5.1 | S2 | `search.py:190,208-210` | 4-char prefix rule makes every ≥4-char query token a wildcard (`data`→`database`). |
| F5.3 | S2 | `search.py:208-210` | No stopwords: doc tokens `this/that/with/from/have/time/user` prefix-match many query tokens → overlap credit on every document. |
| F1.2 | S3 | `test_rerank_drift.py:29` | Imports the constants it uses; structural drift guard only (docstring honest; prior audit slightly overstated). |
| F1.3 | S3 | `search.py:265-269,275-276,283` | 30/10/0.8/0.35, 90/180, 80 are inline; "single source of truth" is incomplete. |
| F1.4 | S3 | `search.py:217` | `rerank` docstring lists 4 of 13 terms. |
| F1.5 | S3 | `search.py:323` | `rrf_score` default 0 vs singleton 1.0 asymmetry for non-`hybrid_search` callers. |
| F2.2 | S3 | `search.py:83,87` | `similarity` computed, never used (and it is exactly what F3.3 needs). |
| F2.5 | S3 | `search.py:146` | Comment: raw RRF lower bound is not 0. |
| F2.6 | S3 | `search.py:122` | RRF ties resolved by `set` iteration order → cross-process non-determinism. |
| F2.7 | S3 | `search.py:150-160` | Min-max over the pool: 2nd of 2 candidates gets `rrf_score=0`; relative not absolute scale. |
| F3.1 | S3 | `search.py:78`, `db.py:48,64` | `SET LOCAL` scoped only by probe-rollback + autocommit default + pool rollback/close; `autocommit=True` makes it a silent WARNING. |
| F3.2 | S3 | `db.py:37-41` `minconn=1` | Fresh backends are the common case → placeholder-GUC path every time; prefer `ALTER DATABASE … SET hnsw.iterative_scan`. |
| F3.3 | S3 | `search.py:82-87` | `relaxed_order` returns out-of-order rows; rank taken from scan order. |
| F4.1 | S3 | `rerank_weights.py:39-44`, `search.py:226` | 20k window comment false on live data (911 chunks > 20k); 250 chunks have embedded-but-rerank-invisible text in 20k–25k. |
| F5.4–F5.9 | S3 | `search.py:224-238` | Short-token denominator inflation, `_` phrase-vs-prefix asymmetry, window, `lower()` vs `casefold()`, title double count, context prefix rule. |
| — | note | `tests/test_search.py:379-390` | `test_per_parent_cap` embeds the parent — production parents are never embedded, so the test corpus does not model the live parent/chunk relationship. |

Confirmed prior claims: `test_rerank.py:214-219` docstring omits `context_overlap`, `superseded_penalty`, `staleness_penalty` (§1); `SET LOCAL` relies on the implicit transaction (§3, with the mechanism now spelled out).

## Could not verify

- Placeholder-GUC behaviour of `SET LOCAL hnsw.iterative_scan` on a backend where `vector.so` is not yet loaded (F3.2): requires issuing `SET`, which the read-only rule forbids. Inferred from PostgreSQL's documented custom-placeholder semantics; a one-line `BEGIN; SET LOCAL …; SELECT '[1]'::vector <=> '[1]'::vector; SHOW hnsw.iterative_scan; ROLLBACK;` in a fresh session settles it.
- That `relaxed_order` actually reorders rows for this index/ef_search (F3.3): would need `EXPLAIN ANALYZE` or running the leg under the GUC. Stated from the pgvector README.
- `scripts/eval/eval_common.py` (`rerank_with_overrides`, `PRODUCTION_WEIGHTS`, `COLD_OVERRIDES`) and `src/embeddings.py`, `src/ingest.py`: not read this pass (out of the narrow scope); F1.3's claim that the inline literals are un-overridable in eval is inferred from `rerank_weights.py`'s docstring, not from reading eval code.
- Whether `tests/test_search_properties.py` asserts determinism of `hybrid_search` ordering (F2.6): file not read.
- F2.8 was measured with the *exact* nearest-neighbour SQL (no HNSW) for three sample rows, not by calling `hybrid_search` (forbidden: MCP/pytest). The HNSW result set could differ slightly but not in duplicate density.
- No test run was performed; every statement about what tests assert is from reading.

Status: COMPLETE.
