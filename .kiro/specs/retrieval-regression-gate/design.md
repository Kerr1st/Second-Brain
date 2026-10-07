# Retrieval regression and ranking ownership: design

search composes vector/full-text candidates and delegates pure filter/fusion/dedup calculations to retrieval. Reranking reads the centralized rerank_weights coefficients with project and lifecycle context.

The gate loads synthetic_36.json into disposable PostgreSQL and calls hybrid_search for each query. It compares IDs/order with frozen independent fixtures; missing targets and duplicates fail rather than disappearing from metrics.

Code authority: [src/search.py](../../../src/search.py), [src/retrieval.py](../../../src/retrieval.py), [src/rerank_weights.py](../../../src/rerank_weights.py), [tests/fixtures/retrieval/synthetic_36.json](../../../tests/fixtures/retrieval/synthetic_36.json).

Regression authority: [tests/test_retrieval_quality_gate.py](../../../tests/test_retrieval_quality_gate.py), [tests/test_rerank.py](../../../tests/test_rerank.py), [tests/test_rerank_drift.py](../../../tests/test_rerank_drift.py). Test presence is not a passing result.
