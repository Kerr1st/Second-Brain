# Retrieval Component

> **Status: canonical component contract.** Last reviewed: 2026-09-01.

Retrieval converts a query and optional filters into a ranked, diverse set of
relevant memories.

## Boundary

Retrieval owns:

- vector and PostgreSQL full-text candidate search;
- Reciprocal Rank Fusion (RRF);
- exact-content duplicate suppression and source-lineage diversity;
- cognitive and utility reranking;
- project, type, source, status, and time filters; and
- retrieval reinforcement through access counts and timestamps.

It does not create source content, judge new insights, deliver briefings, or
choose the model backend that generates a query embedding.

## Contract

The agent-facing contract remains `memory_search(...)`. Its primary internal
retrieval contract is:

```python
retrieve_memories(
    query_text,
    query_embedding,
    limit=10,
    type=None,
    status=None,
    project=None,
    source_type=None,
    created_after=None,
)
```

`hybrid_search(...)` remains a compatibility interface for evaluation scripts
and older internal callers. It does not define the agent-facing retrieval
policy.

## Runtime flow

```text
query
  → generate query embedding
  → 100 vector candidates
  → 100 PostgreSQL full-text candidates
  → RRF fusion
  → cognitive and utility reranking
  → exact-content deduplication
  → source-lineage diversity
  → apply the requested output limit
  → reinforce only returned memories
  → return ranked memories and selected context
```

The candidate population is independent of ordinary requested result limits.
For an unchanged corpus and reinforcement state, a smaller result set is a
prefix of a larger one. RRF and utility ties use stable native IDs as their
final tie-breaker.

Source-lineage diversity resolves Codex-derived root memories through
`metadata.task_source_url`, then falls back to `parent_id`, `source_url`, and
the memory ID. The first pass prefers at most two results from one lineage so a
single Agent Task cannot crowd out related evidence. If that would leave the
response underfilled, a second pass adds the highest-ranked suppressed results.
Two results are retained because one task may contain independently useful
decisions and Correction Episodes.

The reranker uses lexical overlap, title overlap, recency, memory type,
project alignment, retrieval reinforcement, memory status, and available
encoding context. The configured coefficients live in
`src/rerank_weights.py`.

Vector candidates come only from the active `ollama:bge-m3:1024` space. Full-text candidates cover
all memories, including rows not yet locally re-embedded, so the gradual migration degrades to
lexical retrieval rather than mixing BGE-M3 and Titan vectors. Titan vectors remain preserved in
`legacy_embedding` and are not queried by the active path.

## Failure behavior

Retrieval does not fabricate an empty success when database or embedding
infrastructure fails. Callers receive the underlying failure. Legitimate
queries with no matching candidates return an empty result set.

Filtering an approximate HNSW search can under-return. The implementation uses
pgvector iterative scanning for filtered searches and tests the behavior
separately.

## Entry points

| Purpose | Entry point |
|---|---|
| Hybrid retrieval and reranking | `src/search.py` |
| Vector query primitive and CRUD reads | `src/db.py` |
| Query embedding | `src/embeddings.py` |
| Rerank coefficients | `src/rerank_weights.py` |
| Agent-facing search | `src/mcp_server.py::memory_search` |
| Recall health check | `scripts/eval/recall_check.py` |

## Tests and health checks

- `tests/test_search.py`
- `tests/test_search_properties.py`
- `tests/test_rerank.py`
- `tests/test_rerank_drift.py`
- `tests/test_question_search.py`
- `tests/test_mcp_server.py`
- `tests/test_project_properties.py`
- `scripts/eval/recall_check.py`

## Related

- [Architecture Component Index](index.md)
- [How memory works](../user-guide/how-memory-works.md)
- [Search architecture](../ARCHITECTURE.md#search-architecture)
- [Agentic retrieval plan](../AGENTIC-RETRIEVAL-PLAN.md)
- [Quality baseline](../QUALITY-BASELINE-2026-06-06.md)
- [ADR 0012: Use local BGE-M3](../adr/0012-use-local-bge-m3-embedding-space.md)
