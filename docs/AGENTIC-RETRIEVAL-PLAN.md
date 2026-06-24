# Agentic-Retrieval Polish — Plan

Next phase after the retrieval-value refactor. This doc is the resume anchor.
**Status: DONE** (implemented 2026-06-04 — `source_type`/`since_days`/`status` filters on
`memory_search`+`hybrid_search`, `iterative_scan` guard on the filtered path, iteration-oriented
tool docstrings; full suite 553 passed, benchmark unchanged 9/12·3/4·0/12). Branch `main`
(the QD-integration branch was fast-forward-merged into main on 2026-06-04), both remotes (`mini` + `origin`).

## Why this — and why NOT a knowledge graph (P4)
Deep research (2024-2025 sources) + our own data ruled out P4 (wiring the KG into retrieval):
- Our KG is **99.5% disconnected** — only 563 of ~120k memories have any entity link, almost
  all from the Quick Desktop import; the distilled layer, chats, articles, and videos have none.
- GraphRAG **underperforms plain RAG on local/factual queries** (the personal-memory norm) and
  is expensive to build; its global-synthesis strength is **already covered** by our distillation
  + weekly digest.
- Verdict: **defer the KG.** If ever needed, scope it to entity-extracting the ~3,500 distilled
  memories only — never the full corpus.

Highest-ROI next step instead: make the retrieval tools good for **iterative agent use**. A
grounding check showed the MCP already exposes `memory_search`/`memory_read`/`memory_graph`/
`memory_list` plus auto temporal & schema context — so this is a **small polish, not a new pillar**.

## Current system state (2026-06-04)
- pgvector, ~120k memories. Hybrid search (HNSW cosine + Postgres full-text) + RRF + tuned reranker.
- **HNSW recall fixed:** index `m=32, ef_construction=200`, `ef_search=200`; recall@10 ~0.93–0.96.
- **Distillation layer:** ~3,500 `decision`/`insight` memories (self-contained WHAT/WHY).
- **Weekly digest** (`synthesis`) scheduled Mondays (`com.second-brain.weekly-digest`).
- Benchmark `scripts/eval/refactor_probe.py` (distilled 9–10/12, decision 4/4, dup 0/12);
  recall monitor `scripts/eval/recall_check.py` (~0.93); maintenance `scripts/jobs/reindex_embedding.sh`.

## Exact current signatures (verified)
- `mcp_server.py`: `memory_search(query, type=None, limit=10, project=None)`
- `search.py`: `hybrid_search(query_text, query_embedding, limit=10, type=None, status=None, project=None)`

## The plan
### 1. Add filters (core — highest value)
An agent can't currently filter by recency or channel. Add:
- `memory_search`: new params `source_type: str | None`, `since_days: int | None` (and optionally
  `status` to exclude superseded). Compute `created_after = now - since_days`; pass through.
- `hybrid_search`: add `source_type` and `created_after` to the shared `WHERE` (one place — both
  the vector and full-text subqueries reuse it). Defaults `None` → existing behavior unchanged.
- Unlocks "decisions about X in the last month", "only `cli_chat`", etc.

### 2. Sharpen MCP tool descriptions for iteration
- `memory_search`: document the new filters + the temporal/schema context it already returns + a
  one-line hint to refine/iterate or follow up with `memory_read`/`memory_graph` on returned ids.
- Make `memory_read` / `memory_graph` / `memory_list` descriptions consistent so the chaining
  path (search → read → traverse → re-search) is obvious to the agent.

### 3. Technical guard
- A `WHERE` filter on the vector search introduces the **post-filter** case where HNSW can
  under-return. Enable pgvector 0.8 `hnsw.iterative_scan=relaxed_order` on the filtered path so
  recall holds. (This is where `iterative_scan` *does* help — unlike the earlier unfiltered case.)

### Tests + verification
- Extend `tests/test_search.py`: `hybrid_search` with `source_type` + `created_after` narrows correctly.
- Full suite + `refactor_probe.py`: confirm zero regression (new params default off).
- Manual agentic check: `type='decision', since_days=30` returns a sensible narrowed set.

### Out of scope (deferred)
KG/GraphRAG (P4), contextual retrieval, re-embedding, query-routing infra, cross-encoder reranker.
Scoped-KG (entity-extract the distilled layer) stays parked until entity-relationship queries fail.

### Design choice (settled)
Recency = `since_days` (relative; covers "recent / last month"). Add `date_from`/`date_to` only if
explicit ranges prove needed.

## How to resume
1. Read this doc. Run `scripts/eval/refactor_probe.py` (expect ~9–10/12, 4/4, 0/12) and
   `recall_check.py` (~0.93) to confirm the baseline holds.
2. Implement changes 1–3; add the test; run the full suite + benchmark (no regression).
3. Commit + push **both** remotes.
Standing constraints: minimal code; commit + push both remotes; dry-run + approval before
destructive ops; this is additive/safe (no migration, no re-embedding).
