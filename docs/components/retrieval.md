# Retrieval Component

> **Status: canonical component contract.** Last reviewed: 2026-07-23.

Retrieval converts a query and optional filters into a ranked, diverse set of
relevant memories.

## Boundary

Retrieval owns:

- vector and PostgreSQL full-text candidate search;
- Reciprocal Rank Fusion (RRF);
- duplicate and per-parent result suppression;
- cognitive and utility reranking;
- project, type, source, status, and time filters; and
- retrieval reinforcement through access counts and timestamps.

It does not create source content, judge new insights, deliver briefings, or
choose the model backend that generates a query embedding.

## Contract

The internal retrieval contract is:

```python
hybrid_search(
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

`rerank(results, query_text, query_project=None)` then applies the utility
model and returns the final ordering. The MCP Interface composes these calls
behind `memory_search`.

## Runtime flow

```text
query
  → generate query embedding
  → vector candidate search
  → PostgreSQL full-text candidate search
  → RRF fusion
  → near-duplicate and parent-cap filtering
  → cognitive and utility reranking
  → increment retrieval reinforcement
  → return ranked memories and selected context
```

The reranker uses lexical overlap, title overlap, recency, memory type,
project alignment, retrieval reinforcement, memory status, and available
encoding context. The configured coefficients live in
`src/rerank_weights.py`.

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
- `tests/test_project_properties.py`
- `scripts/eval/recall_check.py`

## Related

- [Architecture Component Index](index.md)
- [How memory works](../user-guide/how-memory-works.md)
- [Search architecture](../ARCHITECTURE.md#search-architecture)
- [Agentic retrieval plan](../AGENTIC-RETRIEVAL-PLAN.md)
- [Quality baseline](../QUALITY-BASELINE-2026-06-06.md)
