# Retrieval regression and ranking ownership: acceptance criteria

Reviewed against refactor source on 2026-09-26. These requirements describe behavior, not deployed status.

1. Exercise actual hybrid retrieval using committed synthetic rows/embeddings and 36 independent expected query rankings.
2. Require hit@3=1.0, mean reciprocal rank at least 0.98 and minimum Kendall agreement at least 0.8.
3. Return distinct IDs and preserve full-text reachability of an unembedded parent.
4. Keep filters, text passes, reciprocal-rank fusion, normalization and parent dedup in explicit retrieval helpers.
5. Centralize production reranking coefficients; test score behavior with independently expressed expected values.
6. Treat synthetic stability as a regression gate, not proof that discoveries improve the user's work.

Qualification evidence and unresolved release gates belong in the [completion ledger](../../../docs/REFACTOR-COMPLETION-2026-09-26.md).
